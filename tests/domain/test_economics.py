from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from core.domain.economics import (
    EconomicEvent,
    EconomicProviderStatus,
    EconomicsMode,
    EventStatus,
    Importance,
    MacroCategory,
    MacroDefinition,
    MacroInput,
    MacroObservation,
    MacroProvenance,
    MacroProviderHealth,
    MacroRegistry,
    MacroSeriesState,
    MacroState,
)
from core.domain.models import DataStatus

NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)
LOCAL = NOW.astimezone(timezone(timedelta(hours=5, minutes=30)))


def event(**overrides: object) -> EconomicEvent:
    return EconomicEvent.model_validate(
        {
            "id": "release-2026-09",
            "event_code": "test-release",
            "name": "Test release",
            "category": "growth",
            "importance": "high",
            "scheduled_at": NOW,
            "unit": "percent",
            "frequency": "quarterly",
            "status": "released",
            "provider": "test",
            "source_event_id": "source-1",
            "received_at": NOW,
            "mode": "live",
            **overrides,
        }
    )


def observation(**overrides: object) -> MacroObservation:
    return MacroObservation.model_validate(
        {
            "id": "test.series",
            "period": "2026-Q2",
            "value": 3.2,
            "unit": "percent",
            "source_series_id": "source-series",
            "provider": "test",
            "received_at": NOW,
            **overrides,
        }
    )


def definition(**overrides: object) -> MacroDefinition:
    return MacroDefinition.model_validate(
        {
            "id": "test.series",
            "name": "Test series",
            "category": "growth",
            "unit": "percent",
            "frequency": "quarterly",
            "source_series_id": "source-series",
            "provider": "test",
            **overrides,
        }
    )


@pytest.mark.parametrize(
    ("actual", "consensus", "expected"),
    [
        (3.5, 3.0, 0.5),
        (3.0, 3.5, -0.5),
        (0, 0, 0),
        (None, 3.0, None),
        (3.0, None, None),
        (None, None, None),
    ],
)
def test_surprise(actual, consensus, expected):
    result = event(actual=actual, consensus=consensus)
    assert result.raw_surprise == expected
    assert EconomicEvent.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    "fields",
    [
        {"actual": 1, "consensus": 2, "raw_surprise": 99},
        {"raw_surprise": 0},
        {"actual": 1e308, "consensus": -1e308},
    ],
)
def test_invalid_surprise(fields):
    with pytest.raises(ValidationError):
        event(**fields)


def test_notice_without_numeric_actual_or_schedule():
    result = event(scheduled_at=None, released_at=NOW)
    assert result.status == EventStatus.RELEASED
    assert result.actual is None
    assert result.raw_surprise is None
    assert result.scheduled_at is None


@pytest.mark.parametrize("field", ["scheduled_at", "released_at", "received_at"])
def test_event_timestamps(field):
    result = event(**{field: LOCAL})
    assert getattr(result, field) == NOW
    assert getattr(result, field).tzinfo is UTC
    with pytest.raises(ValidationError):
        event(**{field: NOW.replace(tzinfo=None)})


@pytest.mark.parametrize(
    "field", ["actual", "consensus", "previous", "revised_previous", "raw_surprise"]
)
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_finite_event_numbers(field, value):
    with pytest.raises(ValidationError):
        event(**{field: value})


@pytest.mark.parametrize(
    "fields",
    [
        {"mode": "disabled"},
        {"timezone": "Not/AZone"},
        {"revision": 0},
        {"provider": ""},
        {"id": ""},
        {"unknown": 1},
    ],
)
def test_invalid_event_fields(fields):
    with pytest.raises(ValidationError):
        event(**fields)


def test_event_defaults_and_frozen_fields():
    result = event()
    assert result.country == "US"
    assert result.timezone == "America/New_York"
    assert result.revision == 1
    assert result.notes == []
    assert result.mode == EconomicsMode.LIVE
    with pytest.raises(ValidationError):
        result.actual = 1
    result.notes.append("local note")
    assert event().notes == []


@pytest.mark.parametrize("period", ["2026-01", "2024-02-29", "2026-Q4"])
def test_macro_period(period):
    assert observation(period=period).period == period


@pytest.mark.parametrize(
    "period", ["", "2026-13", "2026-02-29", "2026-Q0", "2026-Q5", "0000-Q1", "2026-1", "2026"]
)
def test_invalid_macro_period(period):
    with pytest.raises(ValidationError):
        observation(period=period)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_finite_observation(value):
    with pytest.raises(ValidationError):
        observation(value=value)


@pytest.mark.parametrize("field", ["released_at", "received_at"])
def test_observation_timestamps(field):
    assert getattr(observation(**{field: LOCAL}), field).tzinfo is UTC
    with pytest.raises(ValidationError):
        observation(**{field: NOW.replace(tzinfo=None)})


def test_observation_null_and_provenance():
    result = observation(value=None)
    assert result.value is None
    assert result.metadata == MacroProvenance()
    source = MacroInput(
        source_series_id="source-series",
        provider="test",
        period="2026-Q2",
        value=123,
        unit="index",
        received_at=LOCAL,
        released_at=LOCAL,
    )
    result = observation(metadata=MacroProvenance(inputs=[source], transform="identity"))
    assert result.metadata.inputs[0].received_at.tzinfo is UTC
    assert MacroObservation.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(ValidationError):
        observation(metadata={"arbitrary_vendor_object": 1})
    with pytest.raises(ValidationError):
        observation(revision=0)


def test_registry_is_explicit_unique_and_read_only():
    item = definition()
    registry = MacroRegistry(definitions=[item])
    assert registry.get(item.id) == item
    assert registry.get("unknown") is None
    assert MacroRegistry().definitions == ()
    with pytest.raises(ValidationError):
        MacroRegistry(definitions=[item, item])
    with pytest.raises(ValidationError):
        item.unit = "other"
    with pytest.raises(ValidationError):
        registry.definitions = ()


def test_state_contract_roundtrip():
    state = MacroState(
        generated_at=LOCAL,
        mode="live",
        series=[
            MacroSeriesState(
                definition=definition(), latest=observation(), status="live", last_checked_at=LOCAL
            )
        ],
        providers=[MacroProviderHealth(name="test", status="healthy", checked_at=LOCAL)],
        upcoming_events=[event(status="scheduled")],
    )
    assert state.generated_at.tzinfo is UTC
    assert state.series[0].last_checked_at.tzinfo is UTC
    assert state.providers[0].checked_at.tzinfo is UTC
    payload = state.model_dump(mode="json")
    assert payload["series"][0]["status"] == "live"
    assert payload["series"][0]["last_checked_at"] == "2026-09-17T12:00:00Z"
    assert MacroState.model_validate_json(state.model_dump_json()) == state


def test_never_checked_unavailable_state():
    series = MacroSeriesState(definition=definition(), status=DataStatus.UNAVAILABLE)
    assert series.last_checked_at is None
    assert series.latest is None
    health = MacroProviderHealth(name="test", status="not_configured")
    assert health.checked_at is None
    state = MacroState(generated_at=NOW, mode="disabled")
    assert state.series == state.providers == state.upcoming_events == []


@pytest.mark.parametrize("status", ["live", "stale"])
@pytest.mark.parametrize("latest", [None, observation(value=None)])
def test_no_numeric_value_is_unavailable(status, latest):
    with pytest.raises(ValidationError):
        MacroSeriesState(definition=definition(), latest=latest, status=status)


@pytest.mark.parametrize("field", ["id", "unit", "provider", "source_series_id"])
def test_latest_matches_definition(field):
    with pytest.raises(ValidationError):
        MacroSeriesState(
            definition=definition(), latest=observation(**{field: "other"}), status="stale"
        )


@pytest.mark.parametrize(
    "model, fields",
    [
        (MacroState, {"generated_at": NOW.replace(tzinfo=None), "mode": "live"}),
        (
            MacroProviderHealth,
            {"name": "test", "status": "healthy", "checked_at": NOW.replace(tzinfo=None)},
        ),
        (
            MacroSeriesState,
            {
                "definition": definition(),
                "status": "unavailable",
                "last_checked_at": NOW.replace(tzinfo=None),
            },
        ),
    ],
)
def test_state_naive_timestamps(model, fields):
    with pytest.raises(ValidationError):
        model.model_validate(fields)


def test_enum_contract():
    assert {item.value for item in EconomicsMode} == {"live", "demo", "disabled"}
    assert {item.value for item in MacroCategory} == {
        "inflation",
        "employment",
        "growth",
        "housing",
        "policy",
    }
    assert {item.value for item in Importance} == {"low", "medium", "high", "critical"}
    assert {item.value for item in EventStatus} == {
        "scheduled",
        "due",
        "released",
        "delayed",
        "cancelled",
        "missing",
        "unknown",
    }
    assert {item.value for item in EconomicProviderStatus} == {
        "healthy",
        "degraded",
        "unavailable",
        "not_configured",
    }
