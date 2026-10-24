"""Cross-market calculation tests: relative strength, bp, missing data."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.analysis.cross_market import (
    build_cross_market_state,
    cross_asset_change,
    relative_strength,
)
from core.domain.cross_market import Availability, RelativeStrength, Value
from core.domain.instruments import AssetClass, QuoteType, get_instrument
from core.domain.models import (
    DataStatus,
    FeedStatus,
    InstrumentState,
    MarketObservation,
    MarketState,
)
from core.services.market_state import StalePolicy, build_market_state

NOW = datetime(2026, 9, 18, 14, 30, tzinfo=UTC)


def instrument(
    symbol: str,
    *,
    value: float | None,
    previous_close: float | None,
    quote_type: QuoteType = QuoteType.PRICE,
    unit: str = "index points",
    status: DataStatus = DataStatus.LIVE,
    feed_status: FeedStatus = FeedStatus.LIVE,
) -> InstrumentState:
    change = None
    change_pct = None
    if value is not None and previous_close is not None and previous_close != 0:
        change = value - previous_close
        change_pct = change / previous_close * 100.0
    return InstrumentState(
        symbol=symbol,
        display_name=symbol,
        asset_class=(
            AssetClass.TREASURY_YIELD
            if quote_type is QuoteType.YIELD
            else AssetClass.EQUITY_INDEX_FUTURE
        ),
        quote_type=quote_type,
        unit=unit,
        value=value,
        previous_close=previous_close,
        change=change,
        change_pct=change_pct,
        timestamp=NOW if value is not None else None,
        received_at=NOW if value is not None else None,
        age_seconds=0.0 if value is not None else None,
        stale_after_seconds=900.0,
        status=status,
        source="cnbc" if value is not None else None,
        feed_status=feed_status,
    )


def market(**instruments) -> MarketState:
    return MarketState(
        generated_at=NOW,
        provider="cnbc",
        provider_status="ok",
        instruments=instruments,
        live_count=sum(1 for i in instruments.values() if i.status is DataStatus.LIVE),
        stale_count=sum(1 for i in instruments.values() if i.status is DataStatus.STALE),
        unavailable_count=sum(
            1 for i in instruments.values() if i.status is DataStatus.UNAVAILABLE
        ),
    )


def test_relative_strength_outperforming():
    value, label = relative_strength(Value.available(0.65), Value.available(0.30))
    assert value.value == pytest.approx(0.35)
    assert label is RelativeStrength.OUTPERFORMING


def test_relative_strength_underperforming_and_aligned():
    assert relative_strength(Value.available(-0.4), Value.available(0.1))[1] is (
        RelativeStrength.UNDERPERFORMING
    )
    assert relative_strength(Value.available(0.05), Value.available(0.02))[1] is (
        RelativeStrength.ALIGNED
    )


def test_relative_strength_unknown_without_legs():
    assert relative_strength(Value.unavailable("x"), Value.available(0.3))[1] is (
        RelativeStrength.UNKNOWN
    )
    assert relative_strength(Value.available(0.3), Value.unavailable("x"))[1] is (
        RelativeStrength.UNKNOWN
    )


def test_yield_change_is_reported_in_basis_points():
    state = instrument(
        "US2Y", value=3.98, previous_close=3.91, quote_type=QuoteType.YIELD, unit="percent"
    )
    change = cross_asset_change(state)
    assert change.change.value == pytest.approx(0.07)
    assert change.change_bp.value == pytest.approx(7.0)
    # Non-yields do not populate bp.
    price = instrument("DXY", value=101.5, previous_close=101.3)
    assert cross_asset_change(price).change_bp.availability is Availability.UNAVAILABLE


def test_cross_asset_missing_value_is_unavailable_not_zero():
    state = instrument("VXN", value=None, previous_close=None, status=DataStatus.UNAVAILABLE)
    change = cross_asset_change(state)
    assert change.price.availability is Availability.UNAVAILABLE
    assert change.price.value is None
    assert change.change_pct.value is None


def test_build_cross_market_state_full():
    m = market(
        NQ=instrument("NQ", value=24832.5, previous_close=24680.0),
        ES=instrument("ES", value=6350.0, previous_close=6331.0),
        US2Y=instrument(
            "US2Y",
            value=3.98,
            previous_close=3.91,
            quote_type=QuoteType.YIELD,
            unit="percent",
        ),
        US10Y=instrument(
            "US10Y",
            value=4.28,
            previous_close=4.24,
            quote_type=QuoteType.YIELD,
            unit="percent",
        ),
        DXY=instrument("DXY", value=101.5, previous_close=101.3),
        VIX=instrument("VIX", value=16.8, previous_close=16.1),
        VXN=instrument("VXN", value=20.5, previous_close=19.5),
    )
    state = build_cross_market_state(m)
    assert state.nq_change_pct.value is not None
    assert state.nq_vs_es.value is not None
    assert state.nq_vs_es_label is not RelativeStrength.UNKNOWN
    assert state.us2y.change_bp.value == pytest.approx(7.0)
    assert state.us10y.change_bp.value == pytest.approx(4.0)
    assert state.dxy.change_pct.value is not None
    assert state.vix.change_pct.value is not None
    assert state.vxn.change_pct.value is not None


def test_build_cross_market_state_degrades_gracefully():
    m = market(
        NQ=instrument("NQ", value=24832.5, previous_close=24680.0),
        ES=instrument("ES", value=None, previous_close=None, status=DataStatus.UNAVAILABLE),
        VXN=instrument("VXN", value=None, previous_close=None, status=DataStatus.UNAVAILABLE),
    )
    state = build_cross_market_state(m)
    assert state.nq_vs_es.value is None
    assert state.nq_vs_es_label is RelativeStrength.UNKNOWN
    assert state.vxn.change_pct.value is None
    assert state.vxn.status is DataStatus.UNAVAILABLE
    # NQ itself still resolves.
    assert state.nq_change_pct.value is not None


def test_stale_leg_withholds_relative_strength():
    m = market(
        NQ=instrument(
            "NQ",
            value=24832.5,
            previous_close=24680.0,
            status=DataStatus.STALE,
            feed_status=FeedStatus.STALE,
        ),
        ES=instrument("ES", value=6350.0, previous_close=6331.0),
    )
    state = build_cross_market_state(m)
    assert state.nq_change_pct.value is None
    assert state.nq_vs_es.value is None
    assert state.nq_vs_es_label is RelativeStrength.UNKNOWN
    assert state.nq_feed_status is FeedStatus.STALE


def test_cross_market_carries_feed_status_for_legs():
    m = market(
        NQ=instrument("NQ", value=24832.5, previous_close=24680.0, feed_status=FeedStatus.DELAYED),
        ES=instrument("ES", value=6350.0, previous_close=6331.0),
    )
    state = build_cross_market_state(m)
    assert state.nq_feed_status is FeedStatus.DELAYED
    assert state.es_feed_status is FeedStatus.LIVE


def test_missing_previous_close_leaves_change_unavailable():
    m = market(NQ=instrument("NQ", value=24832.5, previous_close=None))
    state = build_cross_market_state(m)
    assert state.nq_change_pct.availability is Availability.UNAVAILABLE
    assert state.nq_change_pct.value is None


def test_delayed_comparison_requires_compatible_source_times_and_baselines():
    from datetime import timedelta

    nq = instrument("NQ", value=101, previous_close=100, feed_status=FeedStatus.DELAYED)
    es = instrument("ES", value=100.5, previous_close=100, feed_status=FeedStatus.DELAYED)
    assert build_cross_market_state(market(NQ=nq, ES=es)).nq_vs_es.value == 0.5
    for update in ({"timestamp": NOW - timedelta(seconds=61)},
                   {"baseline_period": "2026-09-17"}, {"status": DataStatus.STALE}):
        result = build_cross_market_state(market(NQ=nq, ES=es.model_copy(update=update)))
        assert result.nq_vs_es.value is None


@pytest.mark.parametrize("age_seconds", [600, 2 * 24 * 60 * 60])
def test_cross_market_freshness_matches_market_state_for_delayed_quotes(age_seconds):
    observed_at = NOW - timedelta(seconds=age_seconds)
    observations = {}
    for symbol, value, close in (
        ("NQ", 101.0, 100.0), ("ES", 100.5, 100.0),
        ("US10Y", 4.28, 4.24), ("VXN", 20.5, 19.5),
    ):
        observations[symbol] = MarketObservation(
            symbol=symbol,
            timestamp=observed_at,
            received_at=observed_at + timedelta(seconds=600),
            value=value,
            unit=get_instrument(symbol).unit,
            source="cnbc",
            metadata={
                "feed_status": "delayed",
                "expected_delay_seconds": 600,
                "previous_close": close,
            },
        )
    raw = build_market_state(
        observations, policy=StalePolicy(), now=NOW, provider="cnbc"
    )
    cross = build_cross_market_state(raw)
    expired = age_seconds > 600
    expected_status = DataStatus.STALE if expired else DataStatus.LIVE
    expected_feed = FeedStatus.STALE if expired else FeedStatus.DELAYED
    for symbol, status, feed, timestamp in (
        ("NQ", cross.nq_data_status, cross.nq_feed_status, cross.nq_timestamp),
        ("ES", cross.es_data_status, cross.es_feed_status, cross.es_timestamp),
        ("US10Y", cross.us10y.status, cross.us10y.feed_status, cross.us10y.timestamp),
        ("VXN", cross.vxn.status, cross.vxn.feed_status, cross.vxn.timestamp),
    ):
        assert status is raw.instruments[symbol].status is expected_status
        assert feed is expected_feed
        assert timestamp == raw.instruments[symbol].timestamp == observed_at
        # Source metadata remains intact; the view applies effective freshness.
        assert raw.instruments[symbol].feed_status is FeedStatus.DELAYED
    assert cross.us10y.price.value == observations["US10Y"].value
    assert cross.vxn.price.value == observations["VXN"].value
    if expired:
        assert cross.nq_vs_es.availability is Availability.UNAVAILABLE
        assert cross.nq_vs_es_label is RelativeStrength.UNKNOWN
    else:
        assert cross.nq_vs_es.value == pytest.approx(0.5)
        assert cross.nq_vs_es_label is RelativeStrength.OUTPERFORMING


@pytest.mark.parametrize("value", [None, 101.0])
def test_unavailable_status_overrides_original_delayed_feed(value):
    nq = instrument(
        "NQ", value=value, previous_close=100,
        status=DataStatus.UNAVAILABLE, feed_status=FeedStatus.DELAYED,
    )
    vxn = nq.model_copy(update={"symbol": "VXN"})
    cross = build_cross_market_state(market(NQ=nq, VXN=vxn))
    assert cross.nq_feed_status is FeedStatus.UNAVAILABLE
    assert cross.es_feed_status is FeedStatus.UNAVAILABLE
    assert cross.vxn.feed_status is FeedStatus.UNAVAILABLE
    assert cross.nq_vs_es.availability is Availability.UNAVAILABLE
