"""Tests for the DuckDB observation repository."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from core.domain.models import MarketObservation
from core.storage.base import StorageError
from core.storage.duckdb_repo import DuckDBObservationRepository


def _obs(
    symbol: str = "NQ",
    *,
    value: float = 24800.0,
    ts: datetime | None = None,
    received_at: datetime | None = None,
    unit: str = "index points",
    metadata: dict | None = None,
) -> MarketObservation:
    ts = ts or datetime(2026, 1, 2, 15, 30, tzinfo=UTC)
    return MarketObservation(
        symbol=symbol,
        timestamp=ts,
        value=value,
        unit=unit,
        source="test",
        received_at=received_at or ts + timedelta(milliseconds=5),
        metadata=metadata or {},
    )


def test_initialize_is_idempotent(tmp_path):
    path = tmp_path / "test.duckdb"
    repo = DuckDBObservationRepository(path)
    repo.initialize()
    repo.initialize()
    assert repo.health() is True
    assert repo.count() == 0
    repo.close()
    assert path.exists()


def test_store_and_latest(repo):
    obs = _obs(value=24800.0)
    assert repo.store([obs]) == 1
    latest = repo.latest_per_instrument()
    assert set(latest) == {"NQ"}
    assert latest["NQ"].value == 24800.0
    assert latest["NQ"].timestamp == obs.timestamp


def test_store_empty_is_noop(repo):
    assert repo.store([]) == 0
    assert repo.count() == 0


def test_latest_is_by_event_time_not_arrival_time(repo):
    """An out-of-order arrival must not become "latest" by arrival order."""
    older_event = datetime(2026, 1, 2, 15, 30, tzinfo=UTC)
    newer_event = datetime(2026, 1, 2, 15, 31, tzinfo=UTC)
    # Store the newer event first, then the older event much later.
    repo.store([_obs(value=100.0, ts=newer_event)])
    repo.store([_obs(value=999.0, ts=older_event)])
    assert repo.latest_per_instrument()["NQ"].value == 100.0


def test_latest_tie_break_on_received_at(repo):
    ts = datetime(2026, 1, 2, 15, 30, tzinfo=UTC)
    repo.store([_obs(value=1.0, ts=ts, received_at=ts + timedelta(seconds=1))])
    repo.store([_obs(value=2.0, ts=ts, received_at=ts + timedelta(seconds=2))])
    assert repo.latest_per_instrument()["NQ"].value == 2.0


def test_latest_per_instrument_covers_multiple(repo):
    repo.store(
        [
            _obs("NQ", value=24800.0),
            _obs("ES", value=6350.0, unit="index points"),
            _obs("US2Y", value=3.91, unit="percent"),
        ]
    )
    latest = repo.latest_per_instrument()
    assert set(latest) == {"NQ", "ES", "US2Y"}
    assert latest["US2Y"].value == 3.91


def test_history_is_oldest_first(repo):
    base = datetime(2026, 1, 2, 15, 30, tzinfo=UTC)
    for i in range(5):
        repo.store([_obs(value=float(i), ts=base + timedelta(minutes=i))])
    rows = repo.history("NQ")
    assert [r.value for r in rows] == [0.0, 1.0, 2.0, 3.0, 4.0]


def test_history_respects_limit_keeping_most_recent(repo):
    base = datetime(2026, 1, 2, 15, 30, tzinfo=UTC)
    for i in range(10):
        repo.store([_obs(value=float(i), ts=base + timedelta(minutes=i))])
    rows = repo.history("NQ", limit=3)
    assert [r.value for r in rows] == [7.0, 8.0, 9.0]


def test_history_time_range_inclusive(repo):
    base = datetime(2026, 1, 2, 15, 30, tzinfo=UTC)
    for i in range(5):
        repo.store([_obs(value=float(i), ts=base + timedelta(minutes=i))])
    rows = repo.history(
        "NQ",
        start=base + timedelta(minutes=1),
        end=base + timedelta(minutes=3),
    )
    assert [r.value for r in rows] == [1.0, 2.0, 3.0]


def test_history_isolates_instruments(repo):
    base = datetime(2026, 1, 2, 15, 30, tzinfo=UTC)
    repo.store([_obs("NQ", value=1.0, ts=base)])
    repo.store([_obs("ES", value=2.0, ts=base)])
    assert [r.value for r in repo.history("NQ")] == [1.0]


def test_history_rejects_non_positive_limit(repo):
    with pytest.raises(ValueError):
        repo.history("NQ", limit=0)


def test_symbol_lookup_is_case_insensitive(repo):
    repo.store([_obs("NQ", value=1.0)])
    assert repo.history("nq")[0].value == 1.0


def test_timestamps_round_trip_as_utc(repo):
    """DuckDB renders TIMESTAMPTZ in local time; the repo must renormalize."""
    ts = datetime(2026, 1, 2, 15, 30, tzinfo=UTC)
    repo.store([_obs(ts=ts)])
    row = repo.latest_per_instrument()["NQ"]
    assert row.timestamp == ts
    assert row.timestamp.tzinfo is not None
    assert row.timestamp.utcoffset() == timedelta(0)
    assert row.received_at.utcoffset() == timedelta(0)


def test_timestamps_round_trip_as_utc_under_non_utc_local_timezone(tmp_path):
    """The UTC renormalization must hold when the process timezone is not UTC.

    DuckDB renders TIMESTAMPTZ in the connection's timezone. On a UTC machine
    this test would pass even without renormalization, so we force a non-UTC
    local timezone to actually exercise the conversion.
    """
    original_tz = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    try:
        repo = DuckDBObservationRepository(tmp_path / "tz.duckdb")
        repo.initialize()
        try:
            # Also set the DuckDB session timezone explicitly.
            repo._connection().execute("SET TimeZone='Asia/Tokyo'")  # noqa: SLF001
            ts = datetime(2026, 1, 2, 15, 30, tzinfo=UTC)
            repo.store([_obs(ts=ts)])
            row = repo.latest_per_instrument()["NQ"]
            assert row.timestamp == ts, "instant must be preserved"
            assert row.timestamp.utcoffset() == timedelta(0), "must be renormalized to UTC"
            assert row.received_at.utcoffset() == timedelta(0)
            # history must also return UTC-normalized timestamps
            assert repo.history("NQ")[0].timestamp.utcoffset() == timedelta(0)
        finally:
            repo.close()
    finally:
        if original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_tz
        time.tzset()


def test_aware_non_utc_input_is_normalized_on_write_and_read(repo):
    """An aware non-UTC instant is stored and returned as the same UTC instant."""
    tokyo = datetime(2026, 1, 3, 0, 30, tzinfo=timezone(timedelta(hours=9)))
    repo.store([_obs(ts=tokyo)])
    row = repo.latest_per_instrument()["NQ"]
    assert row.timestamp == datetime(2026, 1, 2, 15, 30, tzinfo=UTC)
    assert row.timestamp.utcoffset() == timedelta(0)


def test_metadata_round_trips(repo):
    repo.store([_obs(metadata={"previous_close": 24700.0, "simulated": True})])
    row = repo.latest_per_instrument()["NQ"]
    assert row.metadata["previous_close"] == 24700.0
    assert row.metadata["simulated"] is True


def test_health_false_before_initialize(tmp_path):
    repo = DuckDBObservationRepository(tmp_path / "fresh.duckdb")
    assert repo.health() is False
    repo.close()


def test_health_does_not_wait_for_inflight_store_lock(repo):
    assert repo.health() is True
    repo._lock.acquire()
    try:
        started = time.monotonic()
        assert repo.health() is True  # last verified state while storage is busy
        assert time.monotonic() - started < 0.05
    finally:
        repo._lock.release()
    repo.close()
    assert repo.health() is False


def test_close_is_idempotent(repo):
    repo.close()
    repo.close()


def test_databases_are_isolated(tmp_path):
    a = DuckDBObservationRepository(tmp_path / "a.duckdb")
    b = DuckDBObservationRepository(tmp_path / "b.duckdb")
    a.initialize()
    b.initialize()
    a.store([_obs("NQ", value=1.0)])
    assert a.count() == 1
    assert b.count() == 0
    a.close()
    b.close()


def test_data_persists_across_reopen(tmp_path):
    path = tmp_path / "persist.duckdb"
    first = DuckDBObservationRepository(path)
    first.initialize()
    first.store([_obs(value=42.0)])
    first.close()

    second = DuckDBObservationRepository(path)
    second.initialize()
    assert second.count() == 1
    assert second.latest_per_instrument()["NQ"].value == 42.0
    second.close()


def test_store_rejects_naive_datetime_via_schema(repo):
    # MarketObservation itself rejects naive datetimes; verify at the boundary.
    with pytest.raises(ValidationError):
        MarketObservation(
            symbol="NQ",
            timestamp=datetime(2026, 1, 2, 15, 30),  # naive
            value=1.0,
            unit="index points",
            source="test",
            received_at=datetime(2026, 1, 2, 15, 30),
        )


def test_naive_read_timestamp_is_rejected():
    with pytest.raises(StorageError):
        DuckDBObservationRepository._row_to_observation(
            (
                "NQ",
                datetime(2026, 1, 2, 15, 30),  # naive
                1.0,
                "index points",
                "test",
                datetime(2026, 1, 2, 15, 30),
                "{}",
            )
        )


# --- October 20, 2026: write path, migration and retention -------------------


def _minute(minute: int, second: int = 0) -> datetime:
    return datetime(2026, 9, 1, 14, minute, second, tzinfo=UTC)


def test_identical_repoll_is_not_stored_again(repo):
    first = _obs(metadata={"previous_close": 100.0, "feed_status": "delayed", "lag": "601s"})
    again = first.model_copy(
        update={
            "received_at": first.received_at + timedelta(seconds=5),
            # Receipt-derived detail differs; the quote itself does not.
            "metadata": {**first.metadata, "lag": "606s"},
        }
    )
    assert repo.store([first]) == 1
    assert repo.store([again, again]) == 0
    assert repo.count() == 1
    # The first receipt is the one kept.
    assert repo.latest_per_instrument()["NQ"].received_at == first.received_at


@pytest.mark.parametrize(
    "change",
    [
        {"value": 24801.0},
        {"ts": datetime(2026, 1, 2, 15, 31, tzinfo=UTC)},
        {"metadata": {"previous_close": 101.0}},
        {"metadata": {"feed_status": "stale"}},
        {"metadata": {"contract_code": "NQH27"}},
    ],
)
def test_a_changed_quote_is_stored(repo, change):
    base = _obs(metadata={"previous_close": 100.0})
    repo.store([base])
    assert repo.store([_obs(**change)]) == 1
    assert repo.count() == 2


def test_a_value_that_changes_back_is_kept(repo):
    rows = [_obs(value=v, received_at=_minute(0, i)) for i, v in enumerate((1.0, 2.0, 1.0))]
    assert repo.store(rows) == 3


def test_large_batch_is_one_atomic_insert(repo):
    rows = [
        _obs(value=float(i), ts=_minute(0) + timedelta(seconds=i)) for i in range(450)
    ]
    assert repo.store(rows) == 450
    assert repo.count() == 450
    assert [o.value for o in repo.history("NQ", limit=1000)] == [float(i) for i in range(450)]


def test_reads_do_not_wait_for_a_write_in_progress(repo):
    import threading

    repo.store([_obs()])
    with repo._lock:  # a write holding the connection
        result: list = []
        reader = threading.Thread(target=lambda: result.append(repo.history("NQ")))
        reader.start()
        reader.join(timeout=2)
        assert not reader.is_alive(), "read blocked behind the write lock"
    assert len(result[0]) == 1


def test_latest_finds_a_symbol_silent_for_weeks(repo):
    old = _obs("GOLD", value=2000.0, unit="USD per troy ounce", ts=datetime(2026, 8, 1, tzinfo=UTC))
    new = _obs("NQ", ts=datetime(2026, 9, 20, tzinfo=UTC))
    repo.store([old, new])
    latest = repo.latest_per_instrument()
    assert latest["GOLD"].value == 2000.0 and latest["NQ"].timestamp == new.timestamp


def _v1_database(path, rows):
    """A database written by the pre-October-20 code: schema v1, repeats included."""
    import duckdb

    conn = duckdb.connect(str(path))
    conn.execute(
        "CREATE TABLE observations (symbol VARCHAR NOT NULL, ts TIMESTAMPTZ NOT NULL,"
        " value DOUBLE NOT NULL, unit VARCHAR NOT NULL, source VARCHAR NOT NULL,"
        " received_at TIMESTAMPTZ NOT NULL, metadata VARCHAR NOT NULL)"
    )
    conn.execute("CREATE TABLE schema_meta (key VARCHAR PRIMARY KEY, value VARCHAR NOT NULL)")
    conn.execute("INSERT INTO schema_meta VALUES ('version', '1')")
    conn.executemany("INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    conn.close()


def test_v1_migration_removes_only_consecutive_repeats(tmp_path):
    path = tmp_path / "v1.duckdb"
    ts = _minute(0)

    def row(value, second, meta='{"previous_close": 100.0, "lag": "x"}', symbol="NQ"):
        return (symbol, ts, value, "index points", "cnbc", _minute(0, second), meta)

    _v1_database(
        path,
        [
            row(1.0, 1), row(1.0, 2, '{"previous_close": 100.0, "lag": "y"}'),  # repeat
            row(2.0, 3), row(1.0, 4),  # changed and changed back: kept
            row(1.0, 5, '{"previous_close": 99.0}'),  # new previous close: kept
            row(1.0, 6, symbol="ES"), row(1.0, 7, symbol="ES"),  # repeat per symbol
        ],
    )
    repo = DuckDBObservationRepository(path)
    repo.initialize()
    assert repo.count() == 5
    repo.initialize()  # already v2: nothing more removed
    assert repo.count() == 5
    repo.close()
    import duckdb

    conn = duckdb.connect(str(path))
    assert conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone() == ("2",)
    received = [r[0].astimezone(UTC).second for r in conn.execute(
        "SELECT received_at FROM observations WHERE symbol='NQ' ORDER BY received_at").fetchall()]
    conn.close()
    assert received == [1, 3, 4, 5]


def test_compact_thins_old_rows_to_the_last_per_minute(repo):
    now = datetime(2026, 10, 20, 4, 30, 15, tzinfo=UTC)
    old = now - timedelta(days=31)
    old_minute = old.replace(second=0, microsecond=0)
    rows = [
        _obs(value=float(i), ts=old_minute + timedelta(seconds=10 * i)) for i in range(6)
    ]  # one old minute, six quotes
    recent = [
        _obs(value=float(100 + i), ts=now - timedelta(days=2) + timedelta(seconds=5 * i))
        for i in range(6)
    ]
    repo.store(rows + recent)
    assert repo.compact(now=now) == 5
    history = repo.history("NQ", limit=100)
    assert [o.value for o in history if o.timestamp < now - timedelta(days=30)] == [5.0]
    # Recent data keeps every quote.
    assert len([o for o in history if o.timestamp > now - timedelta(days=30)]) == 6
    # Idempotent and incremental.
    assert repo.compact(now=now) == 0
    assert repo.compact(now=now + timedelta(seconds=30)) == 0


def test_compact_windows_never_split_a_minute(repo):
    now = datetime(2026, 10, 20, 4, 30, 0, tzinfo=UTC)
    minute = (now - timedelta(days=30)).replace(second=0)
    repo.store([_obs(value=float(i), ts=minute - timedelta(minutes=1) + timedelta(seconds=10 * i))
                for i in range(12)])  # the minute before the cutoff and the cutoff minute
    repo.compact(now=now + timedelta(seconds=40))  # cutoff floors to `minute`
    repo.compact(now=now + timedelta(minutes=1, seconds=20))
    minutes = [o.timestamp.replace(second=0) for o in repo.history("NQ", limit=100)]
    assert sorted(minutes) == [minute - timedelta(minutes=1), minute]


def test_compact_rejects_naive_time(repo):
    with pytest.raises(ValueError):
        repo.compact(now=datetime(2026, 10, 20))


def test_batch_values_round_trip_exactly(repo):
    """The JSON batch must not round floats, timestamps or metadata text."""
    awkward = [0.1 + 0.2, 1e-300, 123456789.123456789, 24801.25, -0.0001]
    base = datetime(2026, 1, 2, 15, 30, 0, 123457, tzinfo=UTC)
    rows = [
        _obs(value=v, ts=base + timedelta(microseconds=i),
             received_at=base + timedelta(seconds=1, microseconds=7 * i),
             metadata={"note": 'quote "é" \\ end', "n": i})
        for i, v in enumerate(awkward)
    ]
    assert repo.store(rows) == len(rows)
    stored = repo.history("NQ", limit=10)
    assert [(o.timestamp, o.received_at, o.value, o.metadata) for o in stored] == [
        (o.timestamp, o.received_at, o.value, o.metadata) for o in rows
    ]


def test_store_stays_fast_while_another_thread_runs_python(repo):
    """Regression (Oct 21, 2026): a 17-row write took 0.2-0.9 s whenever another
    thread held the GIL, because binding 119 Python parameters re-took it each time."""
    import statistics
    import threading

    stop = threading.Event()

    def busy() -> None:
        while not stop.is_set():
            sum(range(200_000))

    worker = threading.Thread(target=busy)
    worker.start()
    try:
        durations = []
        for batch in range(9):
            rows = [
                _obs(value=float(batch * 100 + i), ts=_minute(batch, i)) for i in range(17)
            ]
            started = time.perf_counter()
            repo.store(rows)
            durations.append(time.perf_counter() - started)
    finally:
        stop.set()
        worker.join()
    assert statistics.median(durations) < 0.1, durations
