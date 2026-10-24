from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from core.domain.economics import (
    EconomicEvent,
    EconomicProviderStatus,
    EconomicsMode,
    MacroObservation,
    MacroProviderHealth,
)
from core.providers.economic_base import EconomicBatch
from core.storage.economics import SQLiteEconomicRepository

NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)


def observation(**overrides):
    return MacroObservation.model_validate(
        {
            "id": "CPI_YOY",
            "period": "2026-08",
            "value": 3.2,
            "unit": "percent y/y",
            "source_series_id": "CUSR0000SA0",
            "provider": "bls",
            "received_at": NOW,
            **overrides,
        }
    )


def event(**overrides):
    return EconomicEvent.model_validate(
        {
            "id": "cpi-release-2026-09",
            "event_code": "CPI",
            "name": "Consumer Price Index",
            "category": "inflation",
            "importance": "critical",
            "scheduled_at": NOW,
            "unit": "percent m/m",
            "frequency": "monthly",
            "status": "released",
            "actual": 0.3,
            "consensus": 0.2,
            "provider": "bls",
            "source_event_id": "bls-cpi-123",
            "received_at": NOW,
            "mode": "live",
            **overrides,
        }
    )


def batch(provider="bls", **overrides):
    return EconomicBatch.model_validate(
        {
            "provider": provider,
            "checked_at": NOW,
            "status": "healthy",
            "observations": [observation()],
            "events": [event()],
            **overrides,
        }
    )


def test_store_batch_writes_versions_and_check(tmp_path):
    repo = SQLiteEconomicRepository(tmp_path / "econ.sqlite3", EconomicsMode.LIVE)
    repo.initialize()
    changes = repo.store_batch(batch())
    assert [item.id for item in changes.observations] == ["CPI_YOY"]
    assert [item.id for item in changes.events] == ["cpi-release-2026-09"]
    assert changes.observations[0].revision == 1
    assert changes.events[0].revision == 1
    checks = repo.checks()
    assert len(checks) == 1
    assert checks[0].name == "bls"
    assert checks[0].status == EconomicProviderStatus.HEALTHY
    repo.close()


def test_identical_batch_is_semantic_dedup(tmp_path):
    repo = SQLiteEconomicRepository(tmp_path / "econ.sqlite3", EconomicsMode.LIVE)
    repo.initialize()
    first = repo.store_batch(batch())
    later = repo.store_batch(
        batch(
            checked_at=NOW + timedelta(hours=1),
            observations=[observation(received_at=NOW + timedelta(hours=1))],
            events=[event(received_at=NOW + timedelta(hours=1))],
        )
    )
    assert first.observations and not later.observations
    assert not later.events
    assert repo.observations() == first.observations
    repo.close()


def test_value_change_creates_new_version(tmp_path):
    repo = SQLiteEconomicRepository(tmp_path / "econ.sqlite3", EconomicsMode.LIVE)
    repo.initialize()
    repo.store_batch(batch())
    second = repo.store_batch(batch(observations=[observation(value=3.4)]))
    assert second.observations[0].revision == 2
    assert second.observations[0].value == 3.4
    all_versions = repo.observations("CPI_YOY", all_versions=True)
    assert [item.revision for item in all_versions] == [1, 2]
    assert repo.observations("CPI_YOY")[0].revision == 2
    repo.close()


def test_aba_cycle_creates_versions(tmp_path):
    repo = SQLiteEconomicRepository(tmp_path / "econ.sqlite3", EconomicsMode.LIVE)
    repo.initialize()
    repo.store_batch(batch())
    repo.store_batch(batch(observations=[observation(value=3.4)]))
    third = repo.store_batch(batch(observations=[observation(value=3.2)]))
    assert third.observations[0].revision == 3
    assert [item.value for item in repo.observations("CPI_YOY", all_versions=True)] == [
        3.2,
        3.4,
        3.2,
    ]
    repo.close()


def test_notes_and_provenance_differences_version(tmp_path):
    repo = SQLiteEconomicRepository(tmp_path / "econ.sqlite3", EconomicsMode.LIVE)
    repo.initialize()
    repo.store_batch(batch())
    changed = repo.store_batch(
        batch(observations=[observation(notes=["revised seasonal factors"])])
    )
    assert changed.observations[0].revision == 2
    assert changed.observations[0].notes == ["revised seasonal factors"]
    repo.close()


def test_actual_difference_versions_event(tmp_path):
    repo = SQLiteEconomicRepository(tmp_path / "econ.sqlite3", EconomicsMode.LIVE)
    repo.initialize()
    repo.store_batch(batch())
    changed = repo.store_batch(batch(events=[event(actual=0.4)]))
    assert changed.events[0].revision == 2
    assert changed.events[0].raw_surprise == pytest.approx(0.2)
    repo.close()


def test_same_source_key_different_series_are_separate(tmp_path):
    repo = SQLiteEconomicRepository(tmp_path / "econ.sqlite3", EconomicsMode.LIVE)
    repo.initialize()
    repo.store_batch(batch())
    other = repo.store_batch(batch(observations=[observation(id="CORE_CPI_YOY")]))
    assert other.observations[0].revision == 1
    assert len(repo.observations()) == 2
    repo.close()


def test_namespace_rejects_wrong_mode_events(tmp_path):
    repo = SQLiteEconomicRepository(tmp_path / "econ.sqlite3", EconomicsMode.LIVE)
    repo.initialize()
    with pytest.raises(ValueError, match="namespace"):
        repo.store_batch(batch(events=[event(mode="demo")]))
    repo.close()


def test_namespace_rejects_reopening_live_db_as_demo(tmp_path):
    path = tmp_path / "econ.sqlite3"
    live = SQLiteEconomicRepository(path, EconomicsMode.LIVE)
    live.initialize()
    live.close()
    demo = SQLiteEconomicRepository(path, EconomicsMode.DEMO)
    with pytest.raises(ValueError, match="namespace"):
        demo.initialize()


def test_disabled_repository_rejects_writes(tmp_path):
    repo = SQLiteEconomicRepository(tmp_path / "econ.sqlite3", EconomicsMode.DISABLED)
    repo.initialize()
    with pytest.raises(ValueError):
        repo.store_batch(batch())
    repo.close()


def test_store_check_separate_from_versions(tmp_path):
    repo = SQLiteEconomicRepository(tmp_path / "econ.sqlite3", EconomicsMode.LIVE)
    repo.initialize()
    repo.store_batch(batch())
    repo.store_check(
        MacroProviderHealth(
            name="bls",
            status=EconomicProviderStatus.UNAVAILABLE,
            checked_at=NOW + timedelta(minutes=5),
            detail="timeout",
        )
    )
    checks = repo.checks()
    assert len(checks) == 1
    assert checks[0].status == EconomicProviderStatus.UNAVAILABLE
    assert len(repo.observations()) == 1
    repo.close()


def test_checks_latest_per_provider(tmp_path):
    repo = SQLiteEconomicRepository(tmp_path / "econ.sqlite3", EconomicsMode.LIVE)
    repo.initialize()
    repo.store_check(MacroProviderHealth(name="bls", status="healthy", checked_at=NOW))
    repo.store_check(MacroProviderHealth(name="fred", status="degraded", checked_at=NOW))
    repo.store_check(
        MacroProviderHealth(
            name="bls",
            status="unavailable",
            checked_at=NOW + timedelta(seconds=60),
        )
    )
    checks = {item.name: item for item in repo.checks()}
    assert set(checks) == {"bls", "fred"}
    assert checks["bls"].status == EconomicProviderStatus.UNAVAILABLE
    assert checks["fred"].status == EconomicProviderStatus.DEGRADED
    repo.close()


def test_read_ordering_and_limit(tmp_path):
    repo = SQLiteEconomicRepository(tmp_path / "econ.sqlite3", EconomicsMode.LIVE)
    repo.initialize()
    repo.store_batch(batch())
    repo.store_batch(batch(observations=[observation(value=3.4)]))
    repo.store_batch(batch(observations=[observation(value=3.1)]))
    assert [item.revision for item in repo.observations("CPI_YOY")] == [3]
    versions = repo.observations("CPI_YOY", all_versions=True)
    assert [item.revision for item in versions] == [1, 2, 3]
    assert [item.revision for item in repo.observations("CPI_YOY", all_versions=True, limit=2)] == [
        2,
        3,
    ]
    repo.close()


def test_events_history_all_versions(tmp_path):
    repo = SQLiteEconomicRepository(tmp_path / "econ.sqlite3", EconomicsMode.LIVE)
    repo.initialize()
    repo.store_batch(batch())
    repo.store_batch(batch(events=[event(actual=0.4)]))
    versions = repo.events("cpi-release-2026-09", all_versions=True)
    assert [item.revision for item in versions] == [1, 2]
    assert versions[0].raw_surprise == pytest.approx(0.1)
    assert versions[1].raw_surprise == pytest.approx(0.2)
    repo.close()


def test_persistence_across_reopen(tmp_path):
    path = tmp_path / "econ.sqlite3"
    repo = SQLiteEconomicRepository(path, EconomicsMode.LIVE)
    repo.initialize()
    repo.store_batch(batch())
    observations = repo.observations()
    events = repo.events()
    checks = repo.checks()
    repo.close()
    reopened = SQLiteEconomicRepository(path, EconomicsMode.LIVE)
    reopened.initialize()
    assert reopened.observations() == observations
    assert reopened.events() == events
    assert reopened.checks() == checks
    reopened.close()


def test_source_revision_and_publication_evidence_are_immutable_across_reopen(tmp_path):
    path = tmp_path / "evidence.sqlite3"
    repo = SQLiteEconomicRepository(path, "live")
    repo.initialize()
    first = repo.store_batch(
        batch(
            observations=[
                observation(
                    metadata={"source_revision": "v1"},
                    released_at=None,
                )
            ]
        )
    ).observations[0]
    second = repo.store_batch(
        batch(
            observations=[
                observation(
                    metadata={"source_revision": "v2"},
                    received_at=NOW + timedelta(hours=1),
                    released_at=NOW - timedelta(days=1),
                )
            ]
        )
    ).observations[0]
    assert second.revision == 2
    assert first.received_at == NOW
    assert first.released_at is None
    assert first.metadata.source_revision == "v1"
    repo.close()
    repo.initialize()
    try:
        assert repo.observations("CPI_YOY", all_versions=True) == [first, second]
        assert repo.observations("CPI_YOY") == [second]
    finally:
        repo.close()


def test_live_repository_rejects_demo_observation_only_batch(tmp_path):
    repo = SQLiteEconomicRepository(tmp_path / "live.sqlite3", "live")
    repo.initialize()
    try:
        with pytest.raises(ValueError):
            repo.store_batch(
                batch(
                    provider="demo",
                    events=[],
                    observations=[
                        observation(
                            provider="demo",
                            source_series_id="demo:CPI_YOY",
                        )
                    ],
                )
            )
        assert repo.observations() == []
    finally:
        repo.close()


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_series_id": "demo:CPI_YOY"},
        {
            "metadata": {
                "inputs": [
                    {
                        "provider": "demo",
                        "source_series_id": "CPI",
                        "period": "2026-08",
                        "unit": "percent",
                        "received_at": NOW,
                    }
                ]
            }
        },
        {
            "metadata": {
                "inputs": [
                    {
                        "provider": "bls",
                        "source_series_id": "demo:CPI",
                        "period": "2026-08",
                        "unit": "percent",
                        "received_at": NOW,
                    }
                ]
            }
        },
    ],
)
def test_live_rejects_demo_provenance_atomically(tmp_path, overrides):
    repo = SQLiteEconomicRepository(tmp_path / "live.sqlite3", "live")
    repo.initialize()
    try:
        with pytest.raises(ValueError, match="namespace"):
            repo.store_batch(
                batch(events=[], observations=[observation(), observation(**overrides)])
            )
        assert repo.observations() == repo.checks() == repo.series_checks() == []
    finally:
        repo.close()


def test_demo_rejects_live_batch(tmp_path):
    repo = SQLiteEconomicRepository(tmp_path / "demo.sqlite3", "demo")
    repo.initialize()
    try:
        with pytest.raises(ValueError, match="namespace"):
            repo.store_batch(batch(events=[]))
    finally:
        repo.close()


def test_series_checks_migration_keeps_old_versions(tmp_path):
    path = tmp_path / "old.sqlite3"
    repo = SQLiteEconomicRepository(path, "live")
    repo.initialize()
    repo.store_batch(batch())
    before = repo.observations()
    repo.connection.execute("DROP TABLE series_checks")
    repo.close()
    repo.initialize()
    try:
        assert repo.observations() == before
        assert repo.series_checks() == []
        repo.store_batch(batch())
        assert repo.series_checks()[0].successful is True
        assert len(repo.observations(all_versions=True)) == 1
    finally:
        repo.close()


def test_verified_period_migrates_legacy_checks_without_inventing_verification(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    repo = SQLiteEconomicRepository(path, "live")
    repo.initialize()
    repo.store_batch(batch())
    repo.connection.execute("DROP TABLE series_checks")
    repo.connection.execute(
        "CREATE TABLE series_checks (seq INTEGER PRIMARY KEY, provider TEXT NOT NULL, "
        "canonical_id TEXT NOT NULL, payload TEXT NOT NULL)"
    )
    from core.storage.economics import SeriesCheck

    legacy = SeriesCheck(
        provider="bls", id="CPI_YOY", attempted_at=NOW, successful=True, last_success_at=NOW
    )
    repo.connection.execute(
        "INSERT INTO series_checks VALUES (1, ?, ?, ?)",
        ("bls", "CPI_YOY", legacy.model_dump_json(exclude={"latest_verified_period"})),
    )
    repo.connection.commit()
    repo.close()
    repo.initialize()
    try:
        assert "latest_verified_period" in {
            row[1] for row in repo.connection.execute("PRAGMA table_info(series_checks)")
        }
        assert repo.series_checks()[0].latest_verified_period is None
        assert repo.series_checks()[0].last_success_at == NOW
        repo.store_batch(batch())
        assert repo.series_checks()[0].latest_verified_period == "2026-08"
        assert repo.connection.execute(
            "SELECT latest_verified_period FROM series_checks ORDER BY seq"
        ).fetchall() == [(None,), ("2026-08",)]
        assert len(repo.observations(all_versions=True)) == 1
    finally:
        repo.close()


def test_uninitialized_repository_raises(tmp_path):
    repo = SQLiteEconomicRepository(tmp_path / "econ.sqlite3", EconomicsMode.LIVE)
    with pytest.raises(RuntimeError):
        repo.observations()


def test_disabled_mode_wal_and_schema(tmp_path):
    repo = SQLiteEconomicRepository(tmp_path / "econ.sqlite3", EconomicsMode.LIVE)
    repo.initialize()
    with sqlite3.connect(str(tmp_path / "econ.sqlite3")) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert mode == "wal"
    assert {"namespace", "versions", "checks"} <= tables
    namespace = conn.execute("SELECT mode FROM namespace").fetchone()
    assert namespace[0] == "live"
    repo.close()


def test_commits_skip_the_per_commit_fsync(tmp_path):
    """Commits run on the event loop; a slow fsync stalled it (Oct 21, 2026)."""
    repo = SQLiteEconomicRepository(tmp_path / "econ.sqlite3", EconomicsMode.LIVE)
    repo.initialize()
    assert repo.connection.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
