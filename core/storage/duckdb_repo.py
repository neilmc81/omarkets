"""DuckDB-backed observation repository.

Design notes:

* One connection per repository instance, guarded by a lock, for writes and
  maintenance. DuckDB connections are not safe for concurrent cross-thread use,
  so each read runs on its own cursor (a second connection to the same
  database): reads never wait behind a write, and DuckDB's MVCC gives them a
  consistent snapshot.
* A batch is one ``INSERT`` (one commit) whose rows travel as a single JSON
  parameter. ``executemany`` committed every row separately (~5x slower on
  disk, Oct 20, 2026). A multi-row ``VALUES`` list with seven parameters per
  row was fast alone (~25 ms for 17 rows) but took 0.2-0.9 s whenever another
  thread was running Python code: DuckDB re-takes the GIL many times while
  binding Python values, and each re-take can wait a full GIL switch interval.
  One string parameter is bound once (~5 ms alone, ~20 ms under load,
  Oct 21, 2026). JSON round-trips the values exactly (``repr`` floats, ISO
  timestamps with microseconds and offset).
* A re-polled quote identical to the last one stored for that symbol (same
  exchange time, value, unit, source, previous close, feed status and contract)
  is not stored again; the first receipt is kept.
* Observations are kept in full for :data:`FINE_RETENTION`; older ones are
  thinned to the last observation per symbol per minute by :meth:`compact`.
* Timestamps are stored as ``TIMESTAMPTZ``. DuckDB returns them rendered in the
  process-local timezone, so reads are explicitly converted back to UTC to
  uphold the "UTC in storage" rule.
* All SQL is parameterized; no string interpolation of values.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb

from core.domain.models import MarketObservation
from core.storage.base import ObservationRepository, OhlcBucket, StorageError

logger = logging.getLogger(__name__)

#: How long every observation is kept before thinning to one per minute.
FINE_RETENTION = timedelta(days=30)
#: Metadata fields that make an otherwise identical re-poll a new observation.
_KEY_METADATA = ("previous_close", "feed_status", "contract_code")
_COLUMNS = "symbol, ts, value, unit, source, received_at, metadata"
#: One batch is a JSON array of row objects, typed on the way in by DuckDB.
_ROW_TYPES = json.dumps([{
    "symbol": "VARCHAR",
    "ts": "TIMESTAMPTZ",
    "value": "DOUBLE",
    "unit": "VARCHAR",
    "source": "VARCHAR",
    "received_at": "TIMESTAMPTZ",
    "metadata": "VARCHAR",
}])
_INSERT_BATCH = (
    f"INSERT INTO observations ({_COLUMNS}) "
    f"SELECT {', '.join('r.' + name for name in _COLUMNS.split(', '))} "
    f"FROM (SELECT unnest(from_json(?, '{_ROW_TYPES}')) AS r)"
)
#: OHLC buckets place a quote at its exchange time, so they only use quotes
#: that reached us at most an hour after it (the feed delay is ~10 minutes) and
#: not before it (beyond a minute of clock skew). Seen in live data: CNBC
#: re-sends the settle at each Globex open stamped 01:00 ET, ~17 hours before
#: it arrives, and anchors date-only quotes to 16:00 ET. Neither is a trade at
#: that time.
_BAR_MAX_LATE_SECONDS = 3600
_BAR_MAX_EARLY_SECONDS = 60
#: The latest observation is looked for in this recent window first.
_LATEST_WINDOW = timedelta(days=3)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    symbol      VARCHAR   NOT NULL,
    ts          TIMESTAMPTZ NOT NULL,
    value       DOUBLE    NOT NULL,
    unit        VARCHAR   NOT NULL,
    source      VARCHAR   NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    metadata    VARCHAR   NOT NULL
);
"""

_SCHEMA_VERSION = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   VARCHAR PRIMARY KEY,
    value VARCHAR NOT NULL
);
"""


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise StorageError("refusing to read a naive timestamp from storage")
    return value.astimezone(UTC)


def _store_key(obs: MarketObservation) -> tuple[object, ...]:
    return (
        obs.timestamp,
        obs.value,
        obs.unit,
        obs.source,
        *(
            json.dumps(obs.metadata.get(name), sort_keys=True, default=str)
            for name in _KEY_METADATA
        ),
    )


def _floor_minute(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(second=0, microsecond=0)


class DuckDBObservationRepository(ObservationRepository):
    """DuckDB implementation of :class:`ObservationRepository`."""

    #: 2 = consecutive duplicate re-polls removed (October 20, 2026).
    SCHEMA_VERSION = "2"
    #: Observation sources that are synthetic and must never seed live state.
    SYNTHETIC_SOURCES = frozenset({"fake", "demo"})

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._lock = threading.Lock()
        self._conn: duckdb.DuckDBPyConnection | None = None
        # A second connection to the same database, used only to open read
        # cursors, so a read never waits for the write lock.
        self._read_lock = threading.Lock()
        self._reader: duckdb.DuckDBPyConnection | None = None
        self._last_health_ok = False
        self._last_key: dict[str, tuple[object, ...]] = {}

    @classmethod
    def migrate_observations(
        cls,
        *,
        source_path: str | Path,
        target_path: str | Path,
        exclude_sources: frozenset[str] = SYNTHETIC_SOURCES,
    ) -> int:
        """Copy real observations from a legacy database into a new one.

        Idempotent and non-destructive: the source database is only read. Rows
        whose ``source`` is synthetic are excluded so a demo run can never seed
        live state. Returns the number of rows copied.
        """
        if str(source_path) == ":memory:" or str(target_path) == ":memory:":
            return 0
        if not Path(source_path).exists():
            return 0
        target = cls(target_path)
        target.initialize()
        try:
            with target._lock:
                conn = target._connection()
                try:
                    # Bulk copy through DuckDB itself; a row-by-row insert of a
                    # large history is prohibitively slow. ATTACH requires a
                    # literal path, so it is escaped (single quotes doubled); the
                    # excluded sources remain parameterized.
                    literal = str(source_path).replace("'", "''")
                    conn.execute(f"ATTACH '{literal}' AS legacy (READ_ONLY)")
                    placeholder = ", ".join("?" for _ in exclude_sources)
                    before = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
                    conn.execute(
                        "INSERT INTO observations "
                        "(symbol, ts, value, unit, source, received_at, metadata) "
                        "SELECT symbol, ts, value, unit, source, received_at, metadata "
                        f"FROM legacy.observations WHERE source NOT IN ({placeholder})",
                        list(exclude_sources),
                    )
                    after = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
                except Exception as exc:  # pragma: no cover - defensive
                    raise StorageError(f"cannot migrate observations: {exc}") from exc
                finally:
                    try:
                        conn.execute("DETACH legacy")
                    except Exception:  # pragma: no cover - best effort
                        pass
        finally:
            # Close outside the lock: ``close`` acquires it and the lock is not
            # reentrant.
            target.close()
        return int(after) - int(before)

    @property
    def path(self) -> str:
        return self._path

    def _connection(self) -> duckdb.DuckDBPyConnection:
        if self._conn is None:
            try:
                if self._path != ":memory:":
                    Path(self._path).parent.mkdir(parents=True, exist_ok=True)
                self._conn = duckdb.connect(self._path)
                with self._read_lock:
                    self._reader = self._conn.cursor()
            except Exception as exc:  # pragma: no cover - environment failure
                raise StorageError(f"cannot open DuckDB database: {exc}") from exc
        return self._conn

    def initialize(self) -> None:
        with self._lock:
            conn = self._connection()
            try:
                conn.execute(_SCHEMA)
                conn.execute(_SCHEMA_VERSION)
                if self._meta(conn, "version") == "1":
                    removed = self._remove_repeated_polls(conn)
                    logger.info("observation store migrated to v2: %d repeated polls removed",
                                removed)
                self._set_meta(conn, "version", self.SCHEMA_VERSION)
                self._last_health_ok = True
            except StorageError:
                raise
            except Exception as exc:
                raise StorageError(f"cannot initialize schema: {exc}") from exc

    @staticmethod
    def _meta(conn: duckdb.DuckDBPyConnection, key: str) -> str | None:
        row = conn.execute("SELECT value FROM schema_meta WHERE key = ?", [key]).fetchone()
        return None if row is None else str(row[0])

    @staticmethod
    def _set_meta(conn: duckdb.DuckDBPyConnection, key: str, value: str) -> None:
        conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            [key, value],
        )

    @staticmethod
    def _remove_repeated_polls(conn: duckdb.DuckDBPyConnection) -> int:
        """Delete rows identical to the previous row stored for the same symbol.

        "Previous" is receipt order, matching the write-time rule, so a value
        that changes and later changes back is never touched.
        """
        key = ", ".join(
            ["ts", "value", "unit", "source"]
            + [f"json_extract(metadata, '$.{name}')" for name in _KEY_METADATA]
        )
        row = conn.execute(
            f"""
            DELETE FROM observations WHERE rowid IN (
                SELECT rid FROM (
                    SELECT rowid AS rid, row({key}) AS k,
                           lag(row({key})) OVER (
                               PARTITION BY symbol ORDER BY received_at, rowid
                           ) AS previous
                    FROM observations
                ) WHERE k IS NOT DISTINCT FROM previous
            )
            """
        ).fetchone()
        conn.execute("CHECKPOINT")
        return int(row[0]) if row else 0

    def store(self, observations: Iterable[MarketObservation]) -> int:
        with self._lock:
            keys = dict(self._last_key)
            rows: list[dict[str, object]] = []
            try:
                for obs in observations:
                    key = _store_key(obs)
                    if keys.get(obs.symbol) == key:
                        continue
                    keys[obs.symbol] = key
                    rows.append({
                        "symbol": obs.symbol,
                        "ts": _to_utc(obs.timestamp).isoformat(),
                        "value": obs.value,
                        "unit": obs.unit,
                        "source": obs.source,
                        "received_at": _to_utc(obs.received_at).isoformat(),
                        "metadata": json.dumps(obs.metadata, sort_keys=True, default=str),
                    })
                batch = json.dumps(rows, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise StorageError(f"cannot serialize observations: {exc}") from exc
            if not rows:
                return 0
            conn = self._connection()
            try:
                conn.execute(_INSERT_BATCH, [batch])
            except Exception as exc:
                raise StorageError(f"cannot store observations: {exc}") from exc
            self._last_key = keys
        return len(rows)

    def _read(self, query: str, params: Sequence[object] = ()) -> list[tuple[object, ...]]:
        """Run a read on its own cursor, never waiting for the write lock."""
        if self._reader is None:
            with self._lock:  # first use: open the database (lock order: write, read)
                self._connection()
        with self._read_lock:
            if self._reader is None:
                raise StorageError("observation store is not open")
            cursor = self._reader.cursor()
        try:
            return cursor.execute(query, list(params)).fetchall()
        finally:
            cursor.close()

    def latest_per_instrument(self) -> dict[str, MarketObservation]:
        latest = f"""
            SELECT {_COLUMNS} FROM observations WHERE {{where}}
            QUALIFY row_number() OVER (
                PARTITION BY symbol
                ORDER BY ts DESC, received_at DESC, value DESC, metadata DESC
            ) = 1
        """
        try:
            newest = self._read("SELECT max(ts), list(DISTINCT symbol) FROM observations")[0]
            if newest[0] is None:
                return {}
            # Recent rows answer for nearly every symbol and avoid a full scan;
            # only symbols silent for longer are looked up across all history.
            rows = self._read(latest.format(where="ts >= ?"), [newest[0] - _LATEST_WINDOW])
            missing = sorted(set(newest[1]) - {row[0] for row in rows})
            if missing:
                rows += self._read(
                    latest.format(where=f"symbol IN ({', '.join('?' for _ in missing)})"),
                    missing,
                )
        except Exception as exc:
            raise StorageError(f"cannot read latest observations: {exc}") from exc
        return {row[0]: self._row_to_observation(row) for row in rows}

    def history(
        self,
        symbol: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
    ) -> Sequence[MarketObservation]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        clauses = ["symbol = ?"]
        params: list[object] = [symbol.strip().upper()]
        if start is not None:
            clauses.append("ts >= ?")
            params.append(_to_utc(start))
        if end is not None:
            clauses.append("ts <= ?")
            params.append(_to_utc(end))
        # Select the most recent `limit` rows, then return oldest-first.
        query = (
            "SELECT symbol, ts, value, unit, source, received_at, metadata "
            f"FROM observations WHERE {' AND '.join(clauses)} "
            "ORDER BY ts DESC, received_at DESC, value DESC, metadata DESC LIMIT ?"
        )
        params.append(limit)
        try:
            rows = self._read(query, params)
        except Exception as exc:
            raise StorageError(f"cannot read history: {exc}") from exc
        observations = [self._row_to_observation(row) for row in rows]
        return tuple(reversed(observations))

    def ohlc(
        self,
        symbol: str,
        *,
        start: datetime,
        end: datetime,
        bucket_seconds: int,
        within: Sequence[tuple[datetime, datetime]] = (),
    ) -> Sequence[OhlcBucket]:
        if bucket_seconds <= 0:
            raise ValueError("bucket_seconds must be positive")
        # One query for every sub-window: the cost is the scan, not the ranges.
        ranges = ""
        bounds: list[object] = []
        if within:
            ranges = " AND (" + " OR ".join("(ts >= ? AND ts < ?)" for _ in within) + ")"
            bounds = [_to_utc(moment) for pair in within for moment in pair]
        # One row per exchange time first (a re-sent time keeps its latest
        # receipt, ties broken by value), so open/close by exchange time are
        # unambiguous. GROUP BY is several times faster here than a window.
        latest = "row(received_at, value)"
        # Feed status and contract come from each bucket's first and last quote:
        # parsing every row's metadata would dominate the query.
        query = f"""
            WITH ticks AS (
                SELECT ts,
                       arg_max(value, {latest}) AS v,
                       arg_max(source, {latest}) AS source,
                       arg_max(metadata, {latest}) AS meta
                FROM observations
                WHERE symbol = ? AND ts >= ? AND ts < ?{ranges}
                  AND ts <= received_at + INTERVAL {_BAR_MAX_EARLY_SECONDS} SECOND
                  AND received_at <= ts + INTERVAL {_BAR_MAX_LATE_SECONDS} SECOND
                GROUP BY ts
            ), buckets AS (
                SELECT CAST(floor(epoch(ts) / ?) AS BIGINT) * ? AS bucket,
                       arg_min(v, ts) AS o, max(v) AS h, min(v) AS l, arg_max(v, ts) AS c,
                       count(*) AS n, min(ts) AS first_at, max(ts) AS last_at,
                       list_sort(list(DISTINCT source)) AS sources,
                       arg_min(meta, ts) AS first_meta, arg_max(meta, ts) AS last_meta
                FROM ticks GROUP BY bucket
            )
            SELECT bucket, o, h, l, c, n, first_at, last_at, sources,
                   list_sort(list_distinct(list_filter([
                       json_extract_string(first_meta, '$.contract_code'),
                       json_extract_string(last_meta, '$.contract_code')],
                       x -> x IS NOT NULL))),
                   coalesce(json_extract_string(first_meta, '$.feed_status') = 'delayed'
                            OR json_extract_string(last_meta, '$.feed_status') = 'delayed',
                            false)
            FROM buckets ORDER BY bucket
        """
        params = [symbol.strip().upper(), _to_utc(start), _to_utc(end), *bounds,
                  bucket_seconds, bucket_seconds]
        try:
            rows = self._read(query, params)
        except Exception as exc:
            raise StorageError(f"cannot build OHLC buckets: {exc}") from exc
        return [
            OhlcBucket(
                start=datetime.fromtimestamp(int(bucket), tz=UTC),
                open=float(o), high=float(h), low=float(lo), close=float(c),
                count=int(n), first_at=_to_utc(first), last_at=_to_utc(last),
                sources=tuple(str(x) for x in sources or ()),
                contracts=tuple(str(x) for x in contracts or ()),
                delayed=bool(delayed),
            )
            for bucket, o, h, lo, c, n, first, last, sources, contracts, delayed in rows
        ]

    def count(self) -> int:
        try:
            rows = self._read("SELECT count(*) FROM observations")
        except Exception as exc:
            raise StorageError(f"cannot count observations: {exc}") from exc
        return int(rows[0][0]) if rows else 0

    def compact(self, *, now: datetime, keep: timedelta = FINE_RETENTION) -> int:
        """Thin observations older than ``keep`` to the last one per symbol per minute.

        Incremental: only rows between the previous cutoff and the new one are
        examined. Cutoffs are whole UTC minutes, so no minute is split between
        two runs. Returns the number of rows removed.
        """
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        cutoff = _floor_minute(now - keep)
        with self._lock:
            conn = self._connection()
            try:
                previous = self._meta(conn, "thinned_before")
                since = datetime.fromisoformat(previous) if previous else None
                if since is not None and since >= cutoff:
                    return 0
                where = "ts < ?" + (" AND ts >= ?" if since is not None else "")
                params: list[object] = [cutoff] + ([since] if since is not None else [])
                row = conn.execute(
                    f"""
                    DELETE FROM observations WHERE rowid IN (
                        SELECT rid FROM (
                            SELECT rowid AS rid, row_number() OVER (
                                PARTITION BY symbol, floor(epoch(ts) / 60)
                                ORDER BY ts DESC, received_at DESC, value DESC, metadata DESC
                            ) AS rn
                            FROM observations WHERE {where}
                        ) WHERE rn > 1
                    )
                    """,
                    params,
                ).fetchone()
                self._set_meta(conn, "thinned_before", cutoff.isoformat())
                removed = int(row[0]) if row else 0
                if removed:
                    conn.execute("CHECKPOINT")
            except Exception as exc:
                raise StorageError(f"cannot compact observations: {exc}") from exc
        return removed

    def health(self) -> bool:
        # A worker may hold the connection lock during a multi-second write.
        # Health is called on the API loop, so never wait on that lock here.
        # Report the last verified result until the next unlocked probe.
        if not self._lock.acquire(blocking=False):
            return self._last_health_ok
        try:
            if self._conn is None:
                self._last_health_ok = False
            else:
                self._conn.execute("SELECT 1 FROM observations LIMIT 1")
                self._last_health_ok = True
        except Exception:
            self._last_health_ok = False
        finally:
            self._lock.release()
        return self._last_health_ok

    def close(self) -> None:
        with self._lock:
            with self._read_lock:
                if self._reader is not None:
                    try:
                        self._reader.close()
                    finally:
                        self._reader = None
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None
                    self._last_health_ok = False

    @staticmethod
    def _row_to_observation(row: Sequence[object]) -> MarketObservation:
        symbol, ts, value, unit, source, received_at, metadata = row
        parsed_metadata = json.loads(metadata) if metadata else {}
        return MarketObservation(
            symbol=str(symbol),
            timestamp=_to_utc(ts),  # type: ignore[arg-type]
            value=float(value),  # type: ignore[arg-type]
            unit=str(unit),
            source=str(source),
            received_at=_to_utc(received_at),  # type: ignore[arg-type]
            metadata=parsed_metadata,
        )


__all__ = ["DuckDBObservationRepository"]
