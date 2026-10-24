from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.domain.economics import EconomicEvent
from core.domain.news import SourceMetadata, SourceType
from core.providers.news.economic_calendar import EconomicCalendarSource

NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)


def _event(**overrides) -> EconomicEvent:
    values = {
        "id": "cpi-release-2026-09",
        "event_code": "CPI",
        "name": "Consumer Price Index",
        "category": "inflation",
        "importance": "critical",
        "scheduled_at": NOW,
        "unit": "percent m/m",
        "frequency": "monthly",
        "status": "scheduled",
        "consensus": 0.3,
        "previous": 0.2,
        "provider": "consensus",
        "source_event_id": "cpi-1",
        "received_at": NOW,
        "mode": "live",
    }
    values.update(overrides)
    return EconomicEvent.model_validate(values)


def _source(events) -> EconomicCalendarSource:
    return EconomicCalendarSource(
        events=lambda: events,
        metadata=SourceMetadata(
            name="economic_calendar",
            display_name="Calendar",
            source_type=SourceType.ECONOMIC_CALENDAR,
        ),
        clock=lambda: NOW,
    )


async def test_calendar_bridge_maps_event_fields() -> None:
    source = _source([_event(status="released", released_at=NOW)])
    items = await source.fetch()
    assert len(items) == 1
    item = items[0]
    assert item.source_type is SourceType.ECONOMIC_CALENDAR
    assert item.economic is not None
    assert item.economic.consensus == 0.3
    assert item.economic.previous == 0.2
    assert item.metadata["country"] == "US"
    assert item.event_type == "inflation"
    assert item.published_at == NOW


async def test_undated_current_vintage_is_not_fresh_release_news():
    item = _event(status="released", actual=.3, actual_basis="current_vintage")
    assert await _source([item]).fetch() == []


async def test_future_calendar_schedule_is_not_published_news() -> None:
    future = _event(scheduled_at=NOW + timedelta(days=30))
    assert await _source([future]).fetch() == []


async def test_calendar_bridge_does_not_replay_observed_series_as_fresh_news() -> None:
    observed = _event(
        id="observed:CPI:2026-08",
        status="released",
        released_at=NOW,
        actual=0.4,
        provider="bls",
    )
    source = _source([observed])
    assert await source.fetch() == []


async def test_calendar_bridge_revision_changes_source_id() -> None:
    first = _source([_event(revision=1, released_at=NOW)])
    second = _source([_event(revision=2, released_at=NOW)])
    assert (await first.fetch())[0].source_id != (await second.fetch())[0].source_id


async def test_calendar_bridge_deduplicates_within_fetch() -> None:
    duplicate = _event(released_at=NOW)
    source = _source([duplicate, duplicate])
    assert len(await source.fetch()) == 1


async def test_calendar_bridge_never_invents_values() -> None:
    bare = _event(consensus=None, previous=None, released_at=NOW)
    source = _source([bare])
    item = (await source.fetch())[0]
    assert item.economic is not None
    assert item.economic.consensus is None
    assert item.economic.previous is None


def test_calendar_source_requires_metadata() -> None:
    with pytest.raises(TypeError):
        EconomicCalendarSource()  # type: ignore[call-arg]
