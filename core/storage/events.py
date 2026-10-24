"""SQLite-backed repository for the Phase 5 event pipeline.

Storage keeps three distinct things, never collapsing them:

* **raw** — the verbatim fetched payload per source item,
* **canonical events** — one deduplicated :class:`MarketEvent` per real event,
* **source occurrences** — one row per source report of a canonical event.

Instrument and country link tables make the common API filters indexed rather
than JSON scans. All timestamps are stored as ISO-8601 UTC strings and all SQL is
parameterized. Operations are short synchronous transactions; the caller runs
them on the event-loop thread, matching the economics repository convention.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from core.domain.news import (
    MarketEvent,
    RawEvent,
    SourceHealth,
    SourceOccurrence,
    SourceType,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS namespace (mode TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS raw_events (
    seq          INTEGER PRIMARY KEY,
    source       TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    source_url   TEXT,
    fetched_at   TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    payload      TEXT NOT NULL,
    UNIQUE(source, source_id, content_hash)
);

CREATE TABLE IF NOT EXISTS events (
    event_id         TEXT PRIMARY KEY,
    deduplication_key TEXT NOT NULL,
    source           TEXT NOT NULL,
    source_type      TEXT NOT NULL,
    source_id        TEXT NOT NULL,
    source_url       TEXT,
    headline         TEXT NOT NULL,
    published_at     TEXT,
    received_at      TEXT NOT NULL,
    first_seen_at    TEXT,
    last_seen_at     TEXT,
    occurrence_count INTEGER NOT NULL,
    revision         INTEGER NOT NULL,
    event_type       TEXT,
    payload          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_published ON events(published_at, received_at);
CREATE INDEX IF NOT EXISTS events_source ON events(source, received_at);
CREATE INDEX IF NOT EXISTS events_type ON events(event_type, received_at);
CREATE INDEX IF NOT EXISTS events_received ON events(received_at);
CREATE INDEX IF NOT EXISTS events_dedup ON events(deduplication_key);

CREATE TABLE IF NOT EXISTS occurrences (
    occurrence_key TEXT PRIMARY KEY,
    event_id       TEXT NOT NULL,
    source         TEXT NOT NULL,
    source_type    TEXT NOT NULL,
    source_id      TEXT NOT NULL,
    source_url     TEXT,
    headline       TEXT NOT NULL,
    published_at   TEXT,
    received_at    TEXT NOT NULL,
    fingerprint    TEXT NOT NULL,
    raw_seq        INTEGER,
    payload        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS occurrences_event ON occurrences(event_id);
CREATE INDEX IF NOT EXISTS occurrences_source ON occurrences(source, received_at);

CREATE TABLE IF NOT EXISTS event_instruments (
    event_id   TEXT NOT NULL,
    instrument TEXT NOT NULL,
    PRIMARY KEY (event_id, instrument)
);
CREATE INDEX IF NOT EXISTS event_instruments_symbol ON event_instruments(instrument, event_id);

CREATE TABLE IF NOT EXISTS event_countries (
    event_id TEXT NOT NULL,
    country  TEXT NOT NULL,
    PRIMARY KEY (event_id, country)
);
CREATE INDEX IF NOT EXISTS event_countries_code ON event_countries(country, event_id);

CREATE TABLE IF NOT EXISTS source_checks (
    seq     INTEGER PRIMARY KEY,
    name    TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS source_checks_lookup ON source_checks(name, seq);
"""


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _parse(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:  # pragma: no cover - we only write aware values
        raise ValueError("stored event timestamp is naive")
    return parsed


class EventRepositoryError(RuntimeError):
    """Raised when the event store cannot serve a request."""


class SQLiteEventRepository:
    """Durable event store shared by all news/geo sources."""

    def __init__(self, path: str | Path, namespace: str = "live") -> None:
        self.path = Path(path) if str(path) != ":memory:" else ":memory:"
        self.namespace = namespace
        self._connection: sqlite3.Connection | None = None

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise EventRepositoryError("event repository is not initialized")
        return self._connection

    def initialize(self) -> None:
        if self._connection is not None:
            return
        if isinstance(self.path, Path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # Commits run on the event loop. With WAL, NORMAL skips the per-commit
            # fsync, which occasionally took seconds on this disk and stalled the
            # loop (Oct 21, 2026). A power cut can lose the last commits, never
            # corrupt the file; news and source health are refetched next poll.
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA)
            with conn:
                row = conn.execute("SELECT mode FROM namespace").fetchone()
                if row is None:
                    conn.execute("INSERT INTO namespace VALUES (?)", (self.namespace,))
                elif row[0] != self.namespace:
                    raise ValueError("event database namespace mismatch")
            conn.execute("PRAGMA foreign_keys=ON")
            self._connection = conn
        except BaseException:
            conn.close()
            raise

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    # --- raw payloads -------------------------------------------------

    def store_raw(self, raw: RawEvent) -> int:
        """Persist a verbatim payload, idempotent on (source, id, content hash)."""
        payload = json.dumps(raw.payload, sort_keys=True, separators=(",", ":"))
        self.connection.execute(
            "INSERT OR IGNORE INTO raw_events(source,source_id,source_url,fetched_at,"
            "content_hash,payload) VALUES (?,?,?,?,?,?)",
            (
                raw.source,
                raw.source_id,
                raw.source_url,
                _iso(raw.fetched_at),
                raw.content_hash,
                payload,
            ),
        )
        row = self.connection.execute(
            "SELECT seq FROM raw_events WHERE source=? AND source_id=? AND content_hash=?",
            (raw.source, raw.source_id, raw.content_hash),
        ).fetchone()
        return int(row[0]) if row else 0

    # --- canonical events ---------------------------------------------

    def event_by_key(self, key: str) -> MarketEvent | None:
        row = self.connection.execute(
            "SELECT payload FROM events WHERE deduplication_key=? LIMIT 1",
            (key,),
        ).fetchone()
        return MarketEvent.model_validate_json(row[0]) if row else None

    def event_by_id(self, event_id: str) -> MarketEvent | None:
        row = self.connection.execute(
            "SELECT payload FROM events WHERE event_id=?", (event_id,)
        ).fetchone()
        return MarketEvent.model_validate_json(row[0]) if row else None

    def upsert_event(self, event: MarketEvent) -> MarketEvent:
        """Insert or replace a canonical event.

        The caller has already deduplicated; this writes the canonical row plus
        its indexed instrument/country links. Re-inserting the same id is
        idempotent rather than an integrity error.
        """
        self.connection.execute(
            "INSERT OR REPLACE INTO events(event_id,deduplication_key,source,source_type,"
            "source_id,source_url,headline,published_at,received_at,first_seen_at,last_seen_at,"
            "occurrence_count,revision,event_type,payload) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event.id,
                event.deduplication_key,
                event.source,
                event.source_type.value,
                event.source_id,
                event.source_url,
                event.headline,
                _iso(event.published_at),
                _iso(event.received_at),
                _iso(event.first_seen_at),
                _iso(event.last_seen_at),
                event.occurrence_count,
                event.revision,
                event.event_type,
                event.model_dump_json(),
            ),
        )
        self._write_links(event)
        return event

    def merge_event(self, canonical: MarketEvent) -> MarketEvent:
        """Update an existing canonical event after another source reported it."""
        self.connection.execute(
            "UPDATE events SET source=?, source_type=?, headline=?, published_at=?, "
            "received_at=?, first_seen_at=?, last_seen_at=?, occurrence_count=?, "
            "revision=?, event_type=?, payload=? WHERE event_id=?",
            (
                canonical.source,
                canonical.source_type.value,
                canonical.headline,
                _iso(canonical.published_at),
                _iso(canonical.received_at),
                _iso(canonical.first_seen_at),
                _iso(canonical.last_seen_at),
                canonical.occurrence_count,
                canonical.revision,
                canonical.event_type,
                canonical.model_dump_json(),
                canonical.id,
            ),
        )
        self._write_links(canonical)
        return canonical

    def _write_links(self, event: MarketEvent) -> None:
        for instrument in event.instruments:
            self.connection.execute(
                "INSERT OR IGNORE INTO event_instruments(event_id,instrument) VALUES (?,?)",
                (event.id, instrument),
            )
        for country in event.countries:
            self.connection.execute(
                "INSERT OR IGNORE INTO event_countries(event_id,country) VALUES (?,?)",
                (event.id, country),
            )

    # --- occurrences ---------------------------------------------------

    def store_occurrence(self, occurrence: SourceOccurrence) -> bool:
        """Insert an occurrence. Returns True when it was new."""
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO occurrences(occurrence_key,event_id,source,source_type,"
            "source_id,source_url,headline,published_at,received_at,fingerprint,raw_seq,payload)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                occurrence.occurrence_key,
                occurrence.event_id,
                occurrence.source,
                occurrence.source_type.value,
                occurrence.source_id,
                occurrence.source_url,
                occurrence.headline,
                _iso(occurrence.published_at),
                _iso(occurrence.received_at),
                occurrence.fingerprint,
                occurrence.raw_seq,
                occurrence.model_dump_json(),
            ),
        )
        return cursor.rowcount > 0

    def occurrences_for(self, event_id: str) -> list[SourceOccurrence]:
        rows = self.connection.execute(
            "SELECT payload FROM occurrences WHERE event_id=? ORDER BY received_at",
            (event_id,),
        )
        return [SourceOccurrence.model_validate_json(row[0]) for row in rows]

    def recent_events(self, *, limit: int = 500) -> list[MarketEvent]:
        rows = self.connection.execute(
            "SELECT payload FROM events ORDER BY COALESCE(published_at, received_at) DESC, "
            "event_id DESC LIMIT ?",
            (limit,),
        )
        return [MarketEvent.model_validate_json(row[0]) for row in rows]

    def query_events(
        self,
        *,
        source: str | None = None,
        source_type: SourceType | None = None,
        event_type: str | None = None,
        instrument: str | None = None,
        country: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 200,
    ) -> list[MarketEvent]:
        clauses: list[str] = []
        params: list[object] = []
        joins = ""
        if instrument is not None:
            joins += " JOIN event_instruments ei ON ei.event_id = e.event_id"
            clauses.append("ei.instrument = ?")
            params.append(instrument)
        if country is not None:
            joins += " JOIN event_countries ec ON ec.event_id = e.event_id"
            clauses.append("ec.country = ?")
            params.append(country)
        if source is not None:
            # A source filter matches the canonical event's primary source or any
            # source that reported it, so a deduplicated event is not lost when
            # filtered by a secondary reporter.
            clauses.append(
                "(e.source = ? OR EXISTS "
                "(SELECT 1 FROM occurrences o WHERE o.event_id = e.event_id AND o.source = ?))"
            )
            params.extend((source, source))
        if source_type is not None:
            clauses.append("e.source_type = ?")
            params.append(source_type.value)
        if event_type is not None:
            clauses.append("e.event_type = ?")
            params.append(event_type)
        moment = "COALESCE(e.published_at, e.received_at)"
        if start is not None:
            clauses.append(f"{moment} >= ?")
            params.append(_iso(start))
        if end is not None:
            clauses.append(f"{moment} < ?")
            params.append(_iso(end))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        sql = (
            f"SELECT e.payload FROM events e{joins}{where} "
            f"ORDER BY {moment} DESC, e.event_id DESC LIMIT ?"
        )
        params.append(limit)
        rows = self.connection.execute(sql, params)
        return [MarketEvent.model_validate_json(row[0]) for row in rows]

    # --- source health -------------------------------------------------

    def store_source_health(self, health: SourceHealth) -> None:
        self.connection.execute(
            "INSERT INTO source_checks(name,payload) VALUES (?,?)",
            (health.name, health.model_dump_json()),
        )

    def source_health(self) -> list[SourceHealth]:
        rows = self.connection.execute(
            "SELECT payload FROM source_checks WHERE seq IN "
            "(SELECT MAX(seq) FROM source_checks GROUP BY name) ORDER BY name"
        )
        return [SourceHealth.model_validate_json(row[0]) for row in rows]

    def count(self) -> int:
        row = self.connection.execute("SELECT COUNT(*) FROM events").fetchone()
        return int(row[0]) if row else 0


__all__ = ["EventRepositoryError", "SQLiteEventRepository"]
