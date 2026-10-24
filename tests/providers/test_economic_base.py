from __future__ import annotations

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
    MacroObservation,
)
from core.providers.economic_base import (
    EconomicBatch,
    EconomicProvider,
    EconomicProviderError,
)

NOW = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)


class _StubProvider(EconomicProvider):
    @property
    def name(self) -> str:
        return "stub"

    async def fetch(self) -> EconomicBatch:
        return EconomicBatch(provider="stub", checked_at=NOW, status=EconomicProviderStatus.HEALTHY)


def _observation() -> MacroObservation:
    return MacroObservation(
        id="cpi.index",
        period="2026-08",
        value=3.2,
        unit="percent y/y",
        source_series_id="CUSR0000SA0",
        provider="stub",
        received_at=NOW,
    )


def _event() -> EconomicEvent:
    return EconomicEvent(
        id="cpi-2026-09",
        event_code="CPI",
        name="Consumer Price Index",
        category=MacroCategory.INFLATION,
        importance=Importance.CRITICAL,
        scheduled_at=NOW,
        unit="percent m/m",
        frequency="monthly",
        status=EventStatus.RELEASED,
        actual=0.3,
        consensus=0.2,
        provider="stub",
        source_event_id="stub-1",
        received_at=NOW,
        mode=EconomicsMode.LIVE,
    )


def test_batch_holds_observations_events_and_issues() -> None:
    batch = EconomicBatch(
        provider="stub",
        checked_at=NOW,
        status=EconomicProviderStatus.DEGRADED,
        observations=[_observation()],
        events=[_event()],
        issues=["series X missing"],
    )
    assert batch.provider == "stub"
    assert len(batch.observations) == 1
    assert len(batch.events) == 1
    assert batch.issues == ["series X missing"]
    assert batch.status is EconomicProviderStatus.DEGRADED


def test_batch_defaults_empty() -> None:
    batch = EconomicBatch(provider="stub", checked_at=NOW, status=EconomicProviderStatus.HEALTHY)
    assert batch.observations == []
    assert batch.events == []
    assert batch.issues == []


def test_batch_rejects_naive_checked_at() -> None:
    with pytest.raises(ValidationError):
        EconomicBatch(
            provider="stub",
            checked_at=datetime(2026, 9, 17, 12, 0, 0),
            status=EconomicProviderStatus.HEALTHY,
        )


def test_batch_is_frozen() -> None:
    batch = EconomicBatch(provider="stub", checked_at=NOW, status=EconomicProviderStatus.HEALTHY)
    with pytest.raises(ValidationError):
        batch.status = EconomicProviderStatus.UNAVAILABLE


async def test_stub_provider_implements_interface() -> None:
    provider = _StubProvider()
    assert provider.name == "stub"
    batch = await provider.fetch()
    assert batch.provider == "stub"
    await provider.aclose()


def test_provider_error_carries_provider_and_detail() -> None:
    error = EconomicProviderError("bls", "rate limited")
    assert error.provider == "bls"
    assert error.detail == "rate limited"
    assert "bls: rate limited" in str(error)


def test_cannot_instantiate_abstract_provider() -> None:
    with pytest.raises(TypeError):
        EconomicProvider()


def test_batch_normalizes_and_roundtrips() -> None:
    batch = EconomicBatch(
        provider="stub",
        checked_at=NOW.astimezone(timezone(timedelta(hours=-4))),
        status="healthy",
        events=[_event()],
        observations=[_observation()],
    )
    assert batch.checked_at == NOW
    assert batch.checked_at.tzinfo is UTC
    assert EconomicBatch.model_validate_json(batch.model_dump_json()) == batch


@pytest.mark.parametrize("payload", ["events", "observations"])
def test_batch_rejects_mixed_provider_payload(payload) -> None:
    item = _event() if payload == "events" else _observation()
    with pytest.raises(ValidationError):
        EconomicBatch.model_validate(
            {
                "provider": "other",
                "checked_at": NOW,
                "status": "healthy",
                payload: [item],
            }
        )


def test_failed_check_can_be_empty_without_substitution() -> None:
    batch = EconomicBatch(
        provider="stub",
        checked_at=NOW,
        status="unavailable",
        issues=["check failed"],
    )
    assert batch.events == batch.observations == []
    assert batch.status == EconomicProviderStatus.UNAVAILABLE


def test_batch_defaults_are_not_shared() -> None:
    first = EconomicBatch(provider="stub", checked_at=NOW, status="healthy")
    second = EconomicBatch(provider="stub", checked_at=NOW, status="healthy")
    first.issues.append("local issue")
    assert second.issues == []
