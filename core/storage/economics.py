from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from core.domain.economics import (
    EconomicEvent,
    EconomicsMode,
    MacroObservation,
    MacroPeriod,
    MacroProviderHealth,
    UTCDatetime,
)
from core.providers.economic_base import EconomicBatch


class SeriesCheck(BaseModel):
    provider: str
    id: str
    attempted_at: UTCDatetime
    successful: bool
    last_success_at: UTCDatetime | None = None
    latest_verified_period: MacroPeriod | None = None


class EconomicChanges(BaseModel):
    observations: list[MacroObservation] = Field(default_factory=list)
    events: list[EconomicEvent] = Field(default_factory=list)


def _semantic(payload: object) -> object:
    if isinstance(payload, dict):
        return {
            key: _semantic(value)
            for key, value in payload.items()
            if key not in {
                "received_at", "checked_at", "last_checked_at", "revision",
                "occurrence_aliases", "occurrence_sources",
            }
        }
    if isinstance(payload, list):
        return [_semantic(value) for value in payload]
    return payload


class SQLiteEconomicRepository:
    def __init__(self, path: str | Path, mode: EconomicsMode) -> None:
        self.path = Path(path) if str(path) != ":memory:" else ":memory:"
        self.mode = EconomicsMode(mode)
        self._connection: sqlite3.Connection | None = None

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("economic repository is not initialized")
        return self._connection

    def initialize(self) -> None:
        if self._connection is not None:
            return
        if isinstance(self.path, Path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # Commits run on the event loop; see SQLiteEventRepository.initialize.
            # Release checks and actuals are refetched from the issuer next poll.
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS namespace (mode TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS versions (
                    seq INTEGER PRIMARY KEY,
                    kind TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    source_key TEXT NOT NULL,
                    canonical_id TEXT NOT NULL,
                    period TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    semantic TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE(kind, provider, source_key, canonical_id, period, revision)
                );
                CREATE TABLE IF NOT EXISTS checks (
                    seq INTEGER PRIMARY KEY,
                    provider TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS versions_lookup
                    ON versions(kind, canonical_id, seq);
                CREATE INDEX IF NOT EXISTS checks_lookup ON checks(provider, seq);
            """)
            with conn:
                row = conn.execute("SELECT mode FROM namespace").fetchone()
                if row is None:
                    conn.execute("INSERT INTO namespace VALUES (?)", (self.mode.value,))
                elif row[0] != self.mode.value:
                    raise ValueError("economic database mode namespace mismatch")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS series_checks (
                    seq INTEGER PRIMARY KEY,
                    provider TEXT NOT NULL,
                    canonical_id TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS series_checks_lookup
                    ON series_checks(provider, canonical_id, seq);
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(series_checks)")}
            if "latest_verified_period" not in columns:
                conn.execute("ALTER TABLE series_checks ADD COLUMN latest_verified_period TEXT")
            self._connection = conn
        except BaseException:
            conn.close()
            raise

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _store(self, item: MacroObservation | EconomicEvent):
        event = isinstance(item, EconomicEvent)
        kind = "event" if event else "observation"
        source = item.source_event_id if event else item.source_series_id
        period = "" if event else item.period
        key = (kind, item.provider, source, item.id, period)
        semantic = json.dumps(_semantic(item.model_dump(mode="json")), sort_keys=True)
        row = self.connection.execute(
            "SELECT revision, semantic FROM versions WHERE kind=? AND provider=? "
            "AND source_key=? AND canonical_id=? AND period=? ORDER BY seq DESC LIMIT 1",
            key,
        ).fetchone()
        if row and row[1] == semantic:
            return None
        revision = row[0] + 1 if row else 1
        saved = type(item).model_validate({**item.model_dump(), "revision": revision})
        self.connection.execute(
            "INSERT INTO versions(kind,provider,source_key,canonical_id,period,revision,"
            "semantic,payload) VALUES (?,?,?,?,?,?,?,?)",
            (*key, revision, semantic, saved.model_dump_json()),
        )
        return saved

    def store_batch(
        self, batch: EconomicBatch, *, expected_series_ids: tuple[str, ...] = ()
    ) -> EconomicChanges:
        batch = EconomicBatch.model_validate_json(batch.model_dump_json())
        if self.mode == EconomicsMode.DISABLED:
            raise ValueError("disabled repository cannot store provider data")
        if any(event.mode != self.mode for event in batch.events):
            raise ValueError("event mode does not match economic database namespace")
        providers = [batch.provider]
        sources = []
        for observation in batch.observations:
            providers.append(observation.provider)
            sources.append(observation.source_series_id)
            for item in observation.metadata.inputs:
                providers.append(item.provider)
                sources.append(item.source_series_id)
        for event in batch.events:
            providers.append(event.provider)
            sources.append(event.source_event_id)
        demo = self.mode == EconomicsMode.DEMO
        if any((name.strip().lower() == "demo") != demo for name in providers):
            raise ValueError("provider does not match economic database namespace")
        if not demo and any(source.lower().startswith("demo:") for source in sources):
            raise ValueError("source does not match economic database namespace")
        changes = EconomicChanges()
        with self.connection:
            for item in batch.observations:
                saved = self._store(item)
                if saved is not None:
                    changes.observations.append(saved)
            for item in batch.events:
                saved = self._store(item)
                if saved is not None:
                    changes.events.append(saved)
            self._insert_check(
                MacroProviderHealth(
                    name=batch.provider,
                    status=batch.status,
                    checked_at=batch.checked_at,
                    detail="; ".join(batch.issues) or None,
                )
            )
            inferred = {item.id for item in batch.observations}
            successful = (
                set(batch.checked_series_ids)
                if batch.checked_series_ids is not None
                else (inferred if batch.status in {"healthy", "degraded"} else set())
            )
            known = {item.id for item in self.series_checks() if item.provider == batch.provider}
            for series_id in sorted(known | inferred | successful | set(expected_series_ids)):
                self._insert_series_check(
                    batch.provider,
                    series_id,
                    batch.checked_at,
                    series_id in successful,
                    max(
                        (item.period for item in batch.observations if item.id == series_id),
                        default=None,
                    ),
                )
        return changes

    def _insert_series_check(
        self,
        provider: str,
        series_id: str,
        attempted_at: datetime,
        successful: bool,
        latest_verified_period: str | None = None,
    ) -> None:
        row = self.connection.execute(
            "SELECT payload FROM series_checks WHERE provider=? AND canonical_id=? "
            "ORDER BY seq DESC LIMIT 1",
            (provider, series_id),
        ).fetchone()
        previous = SeriesCheck.model_validate_json(row[0]) if row else None
        check = SeriesCheck(
            provider=provider,
            id=series_id,
            attempted_at=attempted_at,
            successful=successful,
            latest_verified_period=latest_verified_period
            if successful and latest_verified_period is not None
            else (previous.latest_verified_period if previous else None),
            last_success_at=attempted_at
            if successful
            else (previous.last_success_at if previous else None),
        )
        self.connection.execute(
            "INSERT INTO series_checks(provider,canonical_id,payload,latest_verified_period) "
            "VALUES (?,?,?,?)",
            (provider, series_id, check.model_dump_json(), check.latest_verified_period),
        )

    def series_checks(self) -> list[SeriesCheck]:
        rows = self.connection.execute(
            "SELECT payload FROM series_checks WHERE seq IN "
            "(SELECT MAX(seq) FROM series_checks GROUP BY provider,canonical_id) ORDER BY seq"
        )
        return [SeriesCheck.model_validate_json(row[0]) for row in rows]

    def _insert_check(self, check: MacroProviderHealth) -> None:
        if self.mode != EconomicsMode.DISABLED and (
            (check.name.strip().lower() == "demo") != (self.mode == EconomicsMode.DEMO)
        ):
            raise ValueError("provider check does not match economic database namespace")
        self.connection.execute(
            "INSERT INTO checks(provider,payload) VALUES (?,?)",
            (check.name, check.model_dump_json()),
        )

    def store_check(
        self, check: MacroProviderHealth, *, expected_series_ids: tuple[str, ...] = ()
    ) -> None:
        with self.connection:
            self._insert_check(check)
            if check.checked_at is not None:
                known = {item.id for item in self.series_checks() if item.provider == check.name}
                for series_id in sorted(known | set(expected_series_ids)):
                    self._insert_series_check(check.name, series_id, check.checked_at, False)

    def checks(self) -> list[MacroProviderHealth]:
        rows = self.connection.execute(
            "SELECT payload FROM checks WHERE seq IN "
            "(SELECT MAX(seq) FROM checks GROUP BY provider) ORDER BY provider"
        )
        return [MacroProviderHealth.model_validate_json(row[0]) for row in rows]

    def observations(
        self, series_id: str | None = None, *, all_versions: bool = False, limit: int | None = None
    ) -> list[MacroObservation]:
        return self._read("observation", MacroObservation, series_id, all_versions, limit)

    def events(
        self, event_id: str | None = None, *, all_versions: bool = False, limit: int | None = None
    ) -> list[EconomicEvent]:
        return self._read("event", EconomicEvent, event_id, all_versions, limit)

    def _read(self, kind, model, canonical_id, all_versions, limit):
        query = "SELECT payload FROM versions WHERE kind=?"
        params: list[object] = [kind]
        if canonical_id is not None:
            query += " AND canonical_id=?"
            params.append(canonical_id)
        if not all_versions:
            query += (
                " AND seq IN (SELECT MAX(seq) FROM versions "
                "GROUP BY kind,provider,source_key,canonical_id,period)"
            )
        query += " ORDER BY seq DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        result = [
            model.model_validate_json(row[0]) for row in self.connection.execute(query, params)
        ]
        return list(reversed(result))
