"""Tests for staleness policy and market-state construction.

``build_instrument_state`` and ``build_market_state`` are pure functions: given
observations, a policy, and an injected ``now``, they are fully deterministic.
These tests cover the live/stale/unavailable boundaries, change computation,
and the guarantee that the market state always contains all 18 instruments.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.domain.instruments import (
    DEFAULT_STALE_SECONDS,
    INSTRUMENTS,
    AssetClass,
)
from core.domain.models import (
    DataStatus,
    MarketObservation,
    ProviderStatus,
)
from core.services.market_state import (
    StalePolicy,
    build_instrument_state,
    build_market_state,
)

NOW = datetime(2026, 1, 2, 15, 30, 0, tzinfo=UTC)


def _observation(
    symbol: str,
    *,
    timestamp: datetime = NOW,
    value: float = 24800.0,
    unit: str | None = None,
    previous_close: float | None = None,
) -> MarketObservation:
    instrument = INSTRUMENTS[symbol]
    metadata: dict[str, object] = {}
    if previous_close is not None:
        metadata["previous_close"] = previous_close
    return MarketObservation(
        symbol=symbol,
        timestamp=timestamp,
        value=value,
        unit=unit if unit is not None else instrument.unit,
        source="fake",
        received_at=timestamp,
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# StalePolicy
# ---------------------------------------------------------------------------


def test_threshold_for_returns_per_asset_class_defaults() -> None:
    policy = StalePolicy()
    for symbol, instrument in INSTRUMENTS.items():
        assert policy.threshold_for(symbol) == DEFAULT_STALE_SECONDS[instrument.asset_class], symbol


def test_threshold_for_unknown_symbol_returns_default() -> None:
    policy = StalePolicy(default_seconds=42.0)
    assert policy.threshold_for("ZZZ") == 42.0


def test_threshold_for_honors_override() -> None:
    policy = StalePolicy(overrides={"equity_index_future": 1.0})
    assert policy.threshold_for("NQ") == 1.0
    assert policy.threshold_for("ES") == 1.0
    # Other asset classes keep their defaults.
    assert policy.threshold_for("US2Y") == DEFAULT_STALE_SECONDS[AssetClass.TREASURY_YIELD]


@pytest.mark.parametrize("bad", [0, -1.0, -0.001])
def test_non_positive_default_seconds_raises(bad: float) -> None:
    with pytest.raises(ValueError):
        StalePolicy(default_seconds=bad)


@pytest.mark.parametrize("bad", [0, -1.0, -0.001])
def test_non_positive_override_raises(bad: float) -> None:
    with pytest.raises(ValueError):
        StalePolicy(overrides={"equity_index_future": bad})


def test_unknown_override_asset_class_raises() -> None:
    """A typo'd asset class must fail loudly, not silently keep defaults.

    Silently ignoring a bad key would mislabel stale data as live.
    """
    with pytest.raises(ValueError, match="unknown asset class"):
        StalePolicy(overrides={"equity_index_futures": 5.0})


# ---------------------------------------------------------------------------
# build_instrument_state: freshness boundaries
# ---------------------------------------------------------------------------


def test_observation_at_age_zero_is_live() -> None:
    policy = StalePolicy()
    state = build_instrument_state("NQ", _observation("NQ", timestamp=NOW), policy=policy, now=NOW)
    assert state.status is DataStatus.LIVE
    assert state.age_seconds == 0.0


def test_observation_exactly_at_threshold_is_live() -> None:
    policy = StalePolicy()
    threshold = policy.threshold_for("NQ")
    state = build_instrument_state(
        "NQ",
        _observation("NQ", timestamp=NOW - timedelta(seconds=threshold)),
        policy=policy,
        now=NOW,
    )
    assert state.status is DataStatus.LIVE
    assert state.age_seconds == pytest.approx(threshold)


def test_observation_just_over_threshold_is_stale() -> None:
    policy = StalePolicy()
    threshold = policy.threshold_for("NQ")
    state = build_instrument_state(
        "NQ",
        _observation("NQ", timestamp=NOW - timedelta(seconds=threshold + 0.001)),
        policy=policy,
        now=NOW,
    )
    assert state.status is DataStatus.STALE
    assert state.age_seconds == pytest.approx(threshold + 0.001)


def test_age_seconds_is_correct() -> None:
    policy = StalePolicy()
    state = build_instrument_state(
        "NQ",
        _observation("NQ", timestamp=NOW - timedelta(seconds=2.5)),
        policy=policy,
        now=NOW,
    )
    assert state.age_seconds == pytest.approx(2.5)


def test_future_timestamp_is_unavailable_until_observed() -> None:
    policy = StalePolicy()
    state = build_instrument_state(
        "NQ",
        _observation("NQ", timestamp=NOW + timedelta(seconds=30)),
        policy=policy,
        now=NOW,
    )
    assert state.age_seconds is None
    assert state.value is None
    assert state.status is DataStatus.UNAVAILABLE


def test_override_changes_staleness_boundary() -> None:
    policy = StalePolicy(overrides={"equity_index_future": 1.0})
    state = build_instrument_state(
        "NQ",
        _observation("NQ", timestamp=NOW - timedelta(seconds=2)),
        policy=policy,
        now=NOW,
    )
    assert state.status is DataStatus.STALE
    assert state.stale_after_seconds == 1.0


# ---------------------------------------------------------------------------
# build_instrument_state: missing observation
# ---------------------------------------------------------------------------


def test_missing_observation_is_unavailable_with_no_values() -> None:
    policy = StalePolicy()
    state = build_instrument_state("NQ", None, policy=policy, now=NOW)

    assert state.status is DataStatus.UNAVAILABLE
    assert state.value is None
    assert state.previous_close is None
    assert state.change is None
    assert state.change_pct is None
    assert state.timestamp is None
    assert state.received_at is None
    assert state.age_seconds is None
    assert state.source is None
    # Static instrument metadata is still populated.
    assert state.symbol == "NQ"
    assert state.display_name == INSTRUMENTS["NQ"].display_name
    assert state.unit == INSTRUMENTS["NQ"].unit


def test_unknown_symbol_raises_key_error() -> None:
    policy = StalePolicy()
    with pytest.raises(KeyError):
        build_instrument_state("ZZZ", None, policy=policy, now=NOW)


# ---------------------------------------------------------------------------
# build_instrument_state: change computation
# ---------------------------------------------------------------------------


def test_change_and_change_pct_computed_when_previous_close_present() -> None:
    policy = StalePolicy()
    state = build_instrument_state(
        "NQ",
        _observation("NQ", value=110.0, previous_close=100.0),
        policy=policy,
        now=NOW,
    )
    assert state.previous_close == 100.0
    assert state.change == pytest.approx(10.0)
    assert state.change_pct == pytest.approx(10.0)


def test_change_fields_none_when_previous_close_absent() -> None:
    policy = StalePolicy()
    state = build_instrument_state("NQ", _observation("NQ", value=110.0), policy=policy, now=NOW)
    assert state.previous_close is None
    assert state.change is None
    assert state.change_pct is None


def test_change_fields_none_when_previous_close_is_zero() -> None:
    policy = StalePolicy()
    state = build_instrument_state(
        "NQ",
        _observation("NQ", value=110.0, previous_close=0.0),
        policy=policy,
        now=NOW,
    )
    # The baseline is preserved, but no divide-by-zero change is fabricated.
    assert state.previous_close == 0.0
    assert state.change is None
    assert state.change_pct is None


def test_non_numeric_previous_close_is_ignored() -> None:
    policy = StalePolicy()
    obs = _observation("NQ", value=110.0)
    obs = obs.model_copy(update={"metadata": {"previous_close": "not-a-number"}})
    state = build_instrument_state("NQ", obs, policy=policy, now=NOW)
    assert state.previous_close is None
    assert state.change is None
    assert state.change_pct is None


# ---------------------------------------------------------------------------
# build_market_state
# ---------------------------------------------------------------------------


def test_market_state_always_contains_all_instruments_even_when_empty() -> None:
    policy = StalePolicy()
    state = build_market_state({}, policy=policy, now=NOW, provider="fake")

    assert len(state.instruments) == 18
    assert set(state.instruments) == set(INSTRUMENTS)
    assert all(s.status is DataStatus.UNAVAILABLE for s in state.instruments.values())
    assert state.live_count == 0
    assert state.stale_count == 0
    assert state.unavailable_count == 18


def test_market_state_counters_sum_to_instrument_count_and_match_statuses() -> None:
    policy = StalePolicy()
    observations = {
        # Live: age 0.
        "NQ": _observation("NQ", timestamp=NOW),
        # Stale: well past the 5s equity-index-future threshold.
        "ES": _observation("ES", timestamp=NOW - timedelta(seconds=100)),
        # Live treasury yield (60s threshold).
        "US2Y": _observation("US2Y", timestamp=NOW - timedelta(seconds=30)),
    }
    state = build_market_state(observations, policy=policy, now=NOW, provider="fake")

    assert len(state.instruments) == 18
    assert state.live_count + state.stale_count + state.unavailable_count == 18

    counted_live = sum(1 for s in state.instruments.values() if s.status is DataStatus.LIVE)
    counted_stale = sum(1 for s in state.instruments.values() if s.status is DataStatus.STALE)
    counted_unavailable = sum(
        1 for s in state.instruments.values() if s.status is DataStatus.UNAVAILABLE
    )
    assert state.live_count == counted_live
    assert state.stale_count == counted_stale
    assert state.unavailable_count == counted_unavailable

    assert state.instruments["NQ"].status is DataStatus.LIVE
    assert state.instruments["ES"].status is DataStatus.STALE
    assert state.instruments["US2Y"].status is DataStatus.LIVE


def test_stale_instrument_is_not_counted_as_live() -> None:
    policy = StalePolicy()
    state = build_market_state(
        {"NQ": _observation("NQ", timestamp=NOW - timedelta(seconds=100))},
        policy=policy,
        now=NOW,
        provider="fake",
    )
    assert state.instruments["NQ"].status is DataStatus.STALE
    assert state.live_count == 0
    assert state.stale_count == 1
    assert state.unavailable_count == 17


def test_market_state_preserves_provider_and_generated_at() -> None:
    policy = StalePolicy()
    state = build_market_state(
        {},
        policy=policy,
        now=NOW,
        provider="fake",
        provider_status=ProviderStatus.DEGRADED,
    )
    assert state.generated_at == NOW
    assert state.provider == "fake"
    assert state.provider_status is ProviderStatus.DEGRADED


@pytest.mark.parametrize(
    "symbol,delay", [("NQ", 600), ("ES", 600), ("WTI", 600), ("GOLD", 600), ("VXN", 900)]
)
def test_expected_delay_is_separate_from_excess_staleness(symbol, delay):
    obs = _observation(symbol, timestamp=NOW - timedelta(seconds=delay + 30)).model_copy(
        update={"source": "cnbc", "received_at": NOW, "metadata": {
            "feed_status": "delayed", "expected_delay_seconds": delay,
        }}
    )
    fresh = build_instrument_state(symbol, obs, policy=StalePolicy(), now=NOW)
    assert fresh.status is DataStatus.LIVE
    assert fresh.feed_status.value == "delayed"
    assert fresh.expected_delay_seconds == delay
    assert fresh.excess_age_seconds == 30
    stale = build_instrument_state(
        symbol, obs, policy=StalePolicy(), now=NOW + timedelta(seconds=31)
    )
    assert stale.status is DataStatus.STALE
    assert stale.feed_status.value == "delayed"
