from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.domain.news import (
    EntityKind,
    EntityRef,
    IngestionStatus,
    MarketEvent,
    RawEvent,
    SourceHealth,
    SourceOccurrence,
    SourceStatus,
    SourceType,
)
from core.storage.events import EventRepositoryError, SQLiteEventRepository

NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def repo(tmp_path):
    repository = SQLiteEventRepository(tmp_path / "events.sqlite3", "live")
    repository.initialize()
    yield repository
    repository.close()


def _event(event_id: str, **overrides) -> MarketEvent:
    values = {
        "id": event_id,
        "source": "fed_press_monetary",
        "source_type": SourceType.CENTRAL_BANK,
        "source_id": event_id,
        "headline": "Fed statement",
        "published_at": NOW,
        "received_at": NOW,
        "deduplication_key": event_id,
        "ingestion_status": IngestionStatus.STORED,
        "instruments": ["NQ"],
        "countries": ["US"],
        "entities": [EntityRef(kind=EntityKind.INSTITUTION, id="FED", label="Federal Reserve")],
    }
    values.update(overrides)
    return MarketEvent.model_validate(values)


def test_requires_initialize() -> None:
    repository = SQLiteEventRepository(":memory:", "live")
    with pytest.raises(EventRepositoryError):
        repository.count()


def test_namespace_mismatch_rejected(tmp_path) -> None:
    path = tmp_path / "events.sqlite3"
    SQLiteEventRepository(path, "live").initialize()
    second = SQLiteEventRepository(path, "demo")
    with pytest.raises(ValueError):
        second.initialize()


def test_upsert_and_read_event(repo) -> None:
    event = _event("ev:1")
    repo.upsert_event(event)
    assert repo.event_by_id("ev:1") == event
    assert repo.event_by_key("ev:1") == event
    assert repo.count() == 1


def test_query_filters_by_instrument_country_source_type(repo) -> None:
    repo.upsert_event(_event("ev:1"))
    repo.upsert_event(
        _event(
            "ev:2",
            instruments=["ES"],
            countries=["CN"],
            source="bbc_world",
            source_type=SourceType.NEWS,
            published_at=NOW + timedelta(hours=1),
        )
    )
    assert [event.id for event in repo.query_events(instrument="NQ")] == ["ev:1"]
    assert [event.id for event in repo.query_events(country="CN")] == ["ev:2"]
    assert [event.id for event in repo.query_events(source_type=SourceType.NEWS)] == ["ev:2"]
    assert [event.id for event in repo.query_events(source="bbc_world")] == ["ev:2"]


def test_query_source_matches_secondary_reporter(repo) -> None:
    # Canonical event primary source is wire_a; wire_b is a secondary reporter.
    event = _event("ev:1", source="wire_a", sources=["wire_a", "wire_b"])
    repo.upsert_event(event)
    for reporter, source_id in (("wire_a", "1"), ("wire_b", "9")):
        repo.store_occurrence(
            SourceOccurrence(
                occurrence_key=f"{reporter}:{source_id}",
                event_id="ev:1",
                source=reporter,
                source_type=SourceType.NEWS,
                source_id=source_id,
                headline="Fed statement",
                received_at=NOW,
                fingerprint="key",
            )
        )
    assert [item.id for item in repo.query_events(source="wire_a")] == ["ev:1"]
    assert [item.id for item in repo.query_events(source="wire_b")] == ["ev:1"]


def test_query_time_range_is_half_open(repo) -> None:
    repo.upsert_event(_event("ev:1", published_at=NOW))
    repo.upsert_event(_event("ev:2", published_at=NOW + timedelta(hours=2)))
    events = repo.query_events(start=NOW, end=NOW + timedelta(hours=1))
    assert [event.id for event in events] == ["ev:1"]


def test_query_event_type_and_limit_ordering(repo) -> None:
    repo.upsert_event(_event("ev:1", event_type="rate_decision"))
    repo.upsert_event(_event("ev:2", event_type="speech"))
    assert [event.id for event in repo.query_events(event_type="speech")] == ["ev:2"]
    assert [event.id for event in repo.query_events(limit=1)] == ["ev:2"]


def test_recent_events_orders_newest_first(repo) -> None:
    repo.upsert_event(_event("ev:1", published_at=NOW))
    repo.upsert_event(_event("ev:2", published_at=NOW + timedelta(hours=1)))
    assert [event.id for event in repo.recent_events()] == ["ev:2", "ev:1"]


def test_raw_event_is_deduplicated_by_content_hash(repo) -> None:
    raw = RawEvent(
        source="fed_press_monetary",
        source_id="1",
        fetched_at=NOW,
        content_hash="abc",
        payload={"title": "x"},
    )
    first = repo.store_raw(raw)
    second = repo.store_raw(raw)
    assert first == second


def test_occurrence_insert_is_idempotent(repo) -> None:
    repo.upsert_event(_event("ev:1"))
    occurrence = SourceOccurrence(
        occurrence_key="fed_press_monetary:1",
        event_id="ev:1",
        source="fed_press_monetary",
        source_type=SourceType.CENTRAL_BANK,
        source_id="1",
        headline="Fed statement",
        received_at=NOW,
        fingerprint="key",
    )
    assert repo.store_occurrence(occurrence) is True
    assert repo.store_occurrence(occurrence) is False
    assert repo.occurrences_for("ev:1") == [occurrence]


def test_merge_event_updates_occurrence_count_and_sources(repo) -> None:
    event = _event("ev:1")
    repo.upsert_event(event)
    merged = event.model_copy(
        update={"occurrence_count": 2, "sources": ["fed_press_monetary", "bbc_world"]}
    )
    repo.merge_event(merged)
    stored = repo.event_by_id("ev:1")
    assert stored is not None
    assert stored.occurrence_count == 2
    assert stored.sources == ["fed_press_monetary", "bbc_world"]


def test_source_health_roundtrip(repo) -> None:
    health = SourceHealth(
        name="fed_press_monetary",
        source_type=SourceType.CENTRAL_BANK,
        status=SourceStatus.HEALTHY,
        recent_event_count=3,
    )
    repo.store_source_health(health)
    assert repo.source_health() == [health]


def test_upsert_overwrites_links_without_duplicates(repo) -> None:
    event = _event("ev:1")
    repo.upsert_event(event)
    repo.upsert_event(event)
    assert [item.id for item in repo.query_events(instrument="NQ")] == ["ev:1"]


def test_commits_skip_the_per_commit_fsync(repo) -> None:
    """Commits run on the event loop; a slow fsync stalled it (Oct 21, 2026)."""
    assert repo.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert repo.connection.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
