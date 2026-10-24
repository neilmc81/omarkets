"""Schema validation tests for the core domain models.

These tests pin the boundary contract described in ``core/domain/models.py``:
timestamps are timezone-aware and normalized to UTC, values are finite, symbols
are canonical, and units must match the instrument registry. All time is
injected; nothing here depends on the wall clock or the network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from core.domain.instruments import AssetClass, QuoteType
from core.domain.models import (
    DataStatus,
    InstrumentState,
    MarketObservation,
    MarketState,
    PriceQuote,
    ProviderStatus,
    to_utc,
)

NOW = datetime(2026, 1, 2, 15, 30, 0, tzinfo=UTC)
# A non-UTC but aware timezone (UTC+05:30) used to prove normalization.
IST = timezone(timedelta(hours=5, minutes=30))


def _observation(**overrides: object) -> MarketObservation:
    """Build a valid NQ observation, overriding individual fields."""
    fields: dict[str, object] = {
        "symbol": "NQ",
        "timestamp": NOW,
        "value": 24800.0,
        "unit": "index points",
        "source": "fake",
        "received_at": NOW,
    }
    fields.update(overrides)
    return MarketObservation(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# MarketObservation
# ---------------------------------------------------------------------------


def test_market_observation_accepts_valid_aware_utc() -> None:
    obs = _observation()
    assert obs.symbol == "NQ"
    assert obs.value == 24800.0
    assert obs.unit == "index points"
    assert obs.source == "fake"
    assert obs.timestamp == NOW
    assert obs.received_at == NOW
    assert obs.timestamp.utcoffset() == timedelta(0)
    assert obs.received_at.utcoffset() == timedelta(0)
    assert obs.metadata == {}


def test_market_observation_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError):
        _observation(timestamp=datetime(2026, 1, 2, 15, 30, 0))


def test_market_observation_rejects_naive_received_at() -> None:
    with pytest.raises(ValidationError):
        _observation(received_at=datetime(2026, 1, 2, 15, 30, 0))


def test_market_observation_converts_non_utc_timestamp_to_utc() -> None:
    # 21:00 at UTC+05:30 is 15:30 UTC.
    obs = _observation(timestamp=datetime(2026, 1, 2, 21, 0, 0, tzinfo=IST))
    assert obs.timestamp == NOW
    assert obs.timestamp.utcoffset() == timedelta(0)


def test_market_observation_converts_non_utc_received_at_to_utc() -> None:
    obs = _observation(received_at=datetime(2026, 1, 2, 21, 0, 0, tzinfo=IST))
    assert obs.received_at == NOW
    assert obs.received_at.utcoffset() == timedelta(0)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_market_observation_rejects_non_finite_values(bad: float) -> None:
    with pytest.raises(ValidationError):
        _observation(value=bad)


def test_market_observation_rejects_unknown_symbol() -> None:
    with pytest.raises(ValidationError):
        _observation(symbol="ZZZ", unit="USD")


def test_market_observation_rejects_unit_mismatch() -> None:
    # NQ is declared in "index points", not USD.
    with pytest.raises(ValidationError):
        _observation(unit="USD")


def test_market_observation_canonicalizes_symbol() -> None:
    obs = _observation(symbol="  nq ")
    assert obs.symbol == "NQ"


@pytest.mark.parametrize("field", ["symbol", "unit", "source"])
def test_market_observation_rejects_empty_required_strings(field: str) -> None:
    with pytest.raises(ValidationError):
        _observation(**{field: ""})


# ---------------------------------------------------------------------------
# PriceQuote
# ---------------------------------------------------------------------------


def test_price_quote_accepts_minimal_fields() -> None:
    quote = PriceQuote(symbol="NQ", last=24800.0, timestamp=NOW, source="fake")
    assert quote.symbol == "NQ"
    assert quote.last == 24800.0
    assert quote.bid is None
    assert quote.ask is None
    assert quote.open is None
    assert quote.high is None
    assert quote.low is None
    assert quote.previous_close is None
    assert quote.volume is None


def test_price_quote_accepts_full_fields() -> None:
    quote = PriceQuote(
        symbol="NQ",
        last=24800.0,
        bid=24799.5,
        ask=24800.5,
        open=24750.0,
        high=24850.0,
        low=24700.0,
        previous_close=24780.0,
        volume=12345,
        timestamp=NOW,
        source="fake",
    )
    assert quote.bid == 24799.5
    assert quote.ask == 24800.5
    assert quote.open == 24750.0
    assert quote.high == 24850.0
    assert quote.low == 24700.0
    assert quote.previous_close == 24780.0
    assert quote.volume == 12345


def test_price_quote_rejects_unknown_symbol() -> None:
    with pytest.raises(ValidationError):
        PriceQuote(symbol="ZZZ", last=1.0, timestamp=NOW, source="fake")


def test_price_quote_normalizes_timestamp_to_utc() -> None:
    quote = PriceQuote(
        symbol="NQ",
        last=24800.0,
        timestamp=datetime(2026, 1, 2, 21, 0, 0, tzinfo=IST),
        source="fake",
    )
    assert quote.timestamp == NOW
    assert quote.timestamp.utcoffset() == timedelta(0)


def test_price_quote_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError):
        PriceQuote(
            symbol="NQ",
            last=24800.0,
            timestamp=datetime(2026, 1, 2, 15, 30, 0),
            source="fake",
        )


def test_price_quote_rejects_negative_volume() -> None:
    with pytest.raises(ValidationError):
        PriceQuote(symbol="NQ", last=24800.0, volume=-1, timestamp=NOW, source="fake")


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_price_quote_rejects_non_finite_last(bad: float) -> None:
    with pytest.raises(ValidationError):
        PriceQuote(symbol="NQ", last=bad, timestamp=NOW, source="fake")


# ---------------------------------------------------------------------------
# InstrumentState / MarketState
# ---------------------------------------------------------------------------


def _instrument_state(**overrides: object) -> InstrumentState:
    fields: dict[str, object] = {
        "symbol": "NQ",
        "display_name": "E-mini Nasdaq-100 futures",
        "asset_class": AssetClass.EQUITY_INDEX_FUTURE,
        "quote_type": QuoteType.PRICE,
        "unit": "index points",
        "stale_after_seconds": 5.0,
        "status": DataStatus.LIVE,
    }
    fields.update(overrides)
    return InstrumentState(**fields)  # type: ignore[arg-type]


def test_instrument_state_accepts_valid_state() -> None:
    state = _instrument_state(value=24800.0, age_seconds=0.0, source="fake")
    assert state.status is DataStatus.LIVE
    assert state.value == 24800.0
    assert state.change is None
    assert state.change_pct is None


def test_instrument_state_rejects_negative_age() -> None:
    with pytest.raises(ValidationError):
        _instrument_state(age_seconds=-1.0)


def test_instrument_state_rejects_non_positive_stale_after() -> None:
    with pytest.raises(ValidationError):
        _instrument_state(stale_after_seconds=0.0)


def test_instrument_state_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError):
        _instrument_state(timestamp=datetime(2026, 1, 2, 15, 30, 0))


def test_market_state_accepts_valid_state() -> None:
    state = MarketState(
        generated_at=NOW,
        provider="fake",
        provider_status=ProviderStatus.OK,
        instruments={"NQ": _instrument_state()},
        live_count=1,
        stale_count=0,
        unavailable_count=0,
    )
    assert state.generated_at == NOW
    assert state.provider == "fake"
    assert state.provider_status is ProviderStatus.OK
    assert state.instruments["NQ"].symbol == "NQ"


@pytest.mark.parametrize("field", ["live_count", "stale_count", "unavailable_count"])
def test_market_state_rejects_negative_counters(field: str) -> None:
    counters = {"live_count": 0, "stale_count": 0, "unavailable_count": 0}
    counters[field] = -1
    with pytest.raises(ValidationError):
        MarketState(
            generated_at=NOW,
            provider="fake",
            provider_status=ProviderStatus.OK,
            instruments={},
            **counters,
        )


def test_market_state_rejects_counters_that_do_not_match_statuses() -> None:
    """A distribution that sums correctly but disagrees with the instrument
    statuses must still be rejected by the counter invariant."""
    with pytest.raises(ValidationError, match="counters do not match"):
        MarketState(
            generated_at=NOW,
            provider="fake",
            provider_status=ProviderStatus.OK,
            instruments={"NQ": _instrument_state(status=DataStatus.LIVE)},
            live_count=0,
            stale_count=1,
            unavailable_count=0,
        )


def test_market_state_rejects_counters_when_instruments_are_empty() -> None:
    with pytest.raises(ValidationError, match="counters do not match"):
        MarketState(
            generated_at=NOW,
            provider="fake",
            provider_status=ProviderStatus.OK,
            instruments={},
            live_count=1,
            stale_count=0,
            unavailable_count=0,
        )


def test_market_state_rejects_naive_generated_at() -> None:
    with pytest.raises(ValidationError):
        MarketState(
            generated_at=datetime(2026, 1, 2, 15, 30, 0),
            provider="fake",
            provider_status=ProviderStatus.OK,
            instruments={},
            live_count=0,
            stale_count=0,
            unavailable_count=0,
        )


# ---------------------------------------------------------------------------
# to_utc helper
# ---------------------------------------------------------------------------


def test_to_utc_converts_aware_datetime() -> None:
    assert to_utc(datetime(2026, 1, 2, 21, 0, 0, tzinfo=IST)) == NOW


def test_to_utc_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError):
        to_utc(datetime(2026, 1, 2, 15, 30, 0))
