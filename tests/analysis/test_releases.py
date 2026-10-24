from datetime import UTC, datetime, timedelta

from core.analysis.releases import (
    importance_for,
    observed_release_events,
    parse_release_id,
    release_history,
    release_id,
)
from core.domain.economics import MacroDefinition, MacroObservation, MacroRegistry

NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)


def observation(
    id="CPI_YOY", period="2026-08", value=3.2, revision=1, received_at=NOW, released=None
):
    return MacroObservation(
        id=id,
        period=period,
        value=value,
        unit="percent y/y",
        source_series_id="CUUR0000SA0",
        provider="bls",
        received_at=received_at,
        released_at=released,
        revision=revision,
    )


def definition(id="CPI_YOY"):
    return MacroDefinition(
        id=id,
        name=f"{id} name",
        category="inflation",
        unit="percent y/y",
        frequency="monthly",
        source_series_id="CUUR0000SA0",
        provider="bls",
    )


def registry(*ids):
    return MacroRegistry(definitions=[definition(id) for id in (ids or ("CPI_YOY",))])


def test_actual_previous_and_revision():
    versions = [
        observation(period="2026-07", value=2.9, received_at=NOW - timedelta(days=30)),
        observation(period="2026-08", value=3.2),
        observation(period="2026-07", value=3.0, revision=2, received_at=NOW),
    ]
    events = observed_release_events(versions, registry(), mode="live", now=NOW)
    assert len(events) == 1
    event = events[0]
    assert event.period == "2026-08"
    assert event.actual == 3.2
    assert event.previous == 2.9
    assert event.revised_previous == 3.0


def test_no_schedule_or_consensus_is_invented():
    events = observed_release_events([observation()], registry(), mode="live", now=NOW)
    assert len(events) == 1
    event = events[0]
    assert event.scheduled_at is None
    assert event.consensus is None
    assert event.raw_surprise is None
    assert event.status == "released"
    assert event.actual == 3.2


def test_ids_are_stable_and_reversible():
    first = release_id("CPI_YOY", "2026-08")
    second = release_id("CPI_YOY", "2026-08")
    assert first == second
    assert parse_release_id(first) == ("CPI_YOY", "2026-08")
    assert parse_release_id("cpi-release-2026-09") is None
    assert parse_release_id("observed:broken") is None


def test_importance_map_and_default():
    assert importance_for("CPI_YOY").value == "critical"
    assert importance_for("PAYROLLS").value == "critical"
    assert importance_for("UNEMPLOYMENT").value == "high"
    assert importance_for("EFFR").value == "medium"
    assert importance_for("UNKNOWN_SERIES").value == "medium"


def test_reference_rate_series_are_not_release_events():
    from core.analysis.releases import NON_RELEASE_SERIES

    effr = MacroDefinition(
        id="EFFR",
        name="Effective federal funds rate",
        category="policy",
        unit="percent",
        frequency="daily",
        source_series_id="EFFR",
        provider="nyfed",
    )
    observation = MacroObservation(
        id="EFFR",
        period="2026-09-16",
        value=3.63,
        unit="percent",
        source_series_id="EFFR",
        provider="nyfed",
        received_at=NOW,
    )
    events = observed_release_events(
        [observation], MacroRegistry(definitions=[effr]), mode="live", now=NOW
    )
    assert events == []
    assert "EFFR" in NON_RELEASE_SERIES


def test_missing_value_is_not_a_release():
    events = observed_release_events([observation(value=None)], registry(), mode="live", now=NOW)
    assert events == []


def test_release_history_accumulates_versions():
    versions = [
        observation(period="2026-08", value=3.2, revision=1),
        observation(period="2026-08", value=3.3, revision=2, received_at=NOW + timedelta(hours=1)),
    ]
    history = release_history(versions, definition(), "2026-08", mode="live")
    assert [item.revision for item in history] == [1, 2]
    assert [item.actual for item in history] == [3.2, 3.3]


def test_disabled_mode_returns_nothing():
    assert observed_release_events([observation()], registry(), mode="disabled", now=NOW) == []


def test_received_time_note_when_publication_unknown():
    events = observed_release_events([observation()], registry(), mode="live", now=NOW)
    assert any("not the official source publication timestamp" in note for note in events[0].notes)
    published = observation(released=NOW - timedelta(days=1))
    events = observed_release_events([published], registry(), mode="live", now=NOW)
    assert any("Publication timestamp supplied" in note for note in events[0].notes)


def test_only_latest_observed_period_is_emitted():
    versions = [
        observation(period="2026-06", value=3.0, received_at=NOW - timedelta(days=60)),
        observation(period="2026-07", value=3.1, received_at=NOW - timedelta(days=30)),
        observation(period="2026-08", value=3.2),
    ]
    events = observed_release_events(versions, registry(), mode="live", now=NOW)
    assert [event.period for event in events] == ["2026-08"]
    assert events[0].previous == 3.1


def test_multiple_series_grouped_independently():
    versions = [
        observation(id="CPI_YOY", period="2026-08", value=3.2),
        MacroObservation(
            id="PAYROLLS",
            period="2026-08",
            value=162,
            unit="thousand jobs",
            source_series_id="CES0000000001",
            provider="bls",
            received_at=NOW,
        ),
    ]
    payrolls = MacroDefinition(
        id="PAYROLLS",
        name="Payrolls",
        category="employment",
        unit="thousand jobs",
        frequency="monthly",
        source_series_id="CES0000000001",
        provider="bls",
    )
    events = observed_release_events(
        versions, MacroRegistry(definitions=[definition(), payrolls]), mode="live", now=NOW
    )
    assert {event.event_code for event in events} == {"CPI_YOY", "PAYROLLS"}
