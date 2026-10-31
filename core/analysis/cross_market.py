"""Deterministic cross-market calculations.

Phase 4 measures relationships. It never asserts cause: "US2Y +6.8bp alongside
NQ -0.4%" is a fact; "rates are pushing NQ down" is an analyst interpretation
that belongs to a later layer.

All calculations are pure functions of :class:`~core.domain.models.MarketState`.
A missing or stale instrument yields an explicit ``Value.unavailable`` rather
than zero, and relative strength stays ``unknown`` if either leg is unavailable.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from core.domain.cross_market import (
    CrossAssetChange,
    CrossMarketState,
    RelativeStrength,
    Value,
)
from core.domain.models import DataStatus, FeedStatus, InstrumentState, MarketState

#: NQ minus ES percentage points within this tolerance is "aligned".
DEFAULT_TOLERANCE_PCT = 0.10


def _effective_feed_status(state: InstrumentState | None) -> FeedStatus:
    """Present current freshness before the source's original feed quality."""
    if state is None or state.status is DataStatus.UNAVAILABLE or state.value is None:
        return FeedStatus.UNAVAILABLE
    if state.status is DataStatus.STALE:
        return FeedStatus.STALE
    return state.feed_status


def _change_fields(state: InstrumentState | None) -> tuple[Value, Value, Value]:
    """Return ``(price, change, change_pct)`` for an instrument state.

    A stale instrument is treated as unavailable for *derived* relative-strength
    purposes: its last value may still be shown, but it must not drive a fresh
    "NQ outperforming" claim.
    """
    # ``!=``/``==`` rather than ``is``: a plain-string status compares equal.
    if state is None or state.status != DataStatus.LIVE or state.value is None:
        reason = (
            "instrument stale; relative strength withheld"
            if state is not None and state.status == DataStatus.STALE
            else "instrument unavailable"
        )
        return Value.unavailable(reason), Value.unavailable(reason), Value.unavailable(reason)
    price = Value.available(state.value)
    if state.change is None:
        change = Value.unavailable(
            "no provider baseline; change cannot be computed from a prior close"
        )
    else:
        change = Value.available(state.change)
    if state.change_pct is None:
        change_pct = Value.unavailable("no provider baseline; percent change cannot be computed")
    else:
        change_pct = Value.available(state.change_pct)
    return price, change, change_pct


def cross_asset_change(state: InstrumentState | None) -> CrossAssetChange:
    """Level and change for one cross-asset instrument.

    This one keeps a stale value visible (with its status), because the panel
    should show the last level explicitly labelled stale rather than blank it.
    ``change_bp`` is only populated for yields: a change of 0.01 percentage
    points is 1 bp.
    """
    if state is None or state.value is None:
        reason = "instrument unavailable"
        price = change = change_pct = Value.unavailable(reason)
    else:
        price = Value.available(state.value)
        change = (
            Value.available(state.change)
            if state.change is not None
            else Value.unavailable("no provider baseline")
        )
        change_pct = (
            Value.available(state.change_pct)
            if state.change_pct is not None
            else Value.unavailable("no provider baseline")
        )
    is_yield = state is not None and state.quote_type.value == "yield"
    if is_yield and change.availability.value == "available":
        change_bp = Value.available(change.value * 100.0)
    elif is_yield:
        change_bp = Value.unavailable(change.reason or "yield change unavailable")
    else:
        change_bp = Value.unavailable("basis-point change is only defined for yields")
    return CrossAssetChange(
        symbol=state.symbol if state is not None else "",
        price=price,
        change=change,
        change_pct=change_pct,
        change_bp=change_bp,
        timestamp=state.timestamp if state is not None else None,
        feed_status=_effective_feed_status(state),
        status=state.status if state is not None else DataStatus.UNAVAILABLE,
    )


def relative_strength(
    nq_change_pct: Value,
    es_change_pct: Value,
    *,
    tolerance_pct: float = DEFAULT_TOLERANCE_PCT,
) -> tuple[Value, RelativeStrength]:
    """NQ percentage change minus ES percentage change, in percentage points."""
    if (
        nq_change_pct.value is None
        or es_change_pct.value is None
        or nq_change_pct.availability.value != "available"
        or es_change_pct.availability.value != "available"
    ):
        reason = "NQ or ES session change unavailable"
        return Value.unavailable(reason), RelativeStrength.UNKNOWN
    difference = nq_change_pct.value - es_change_pct.value
    if difference > tolerance_pct:
        label = RelativeStrength.OUTPERFORMING
    elif difference < -tolerance_pct:
        label = RelativeStrength.UNDERPERFORMING
    else:
        label = RelativeStrength.ALIGNED
    return Value.available(difference), label


def _baseline(state: InstrumentState) -> str | None:
    if state.baseline_period is not None:
        return state.baseline_period
    return (state.timestamp.astimezone(ZoneInfo("America/New_York")).date().isoformat()
            if state.timestamp else None)


def compare_nq_es(
    nq: InstrumentState | None,
    es: InstrumentState | None,
    *,
    tolerance_pct: float = DEFAULT_TOLERANCE_PCT,
) -> tuple[Value, RelativeStrength]:
    """NQ minus ES today, only when both legs are live and comparable: the same
    source, quote times within a minute and the same baseline period."""
    compatible = (
        nq is not None and es is not None
        and nq.timestamp is not None and es.timestamp is not None
        and abs((nq.timestamp - es.timestamp).total_seconds()) <= 60
        and nq.source is not None and nq.source == es.source
        and _baseline(nq) == _baseline(es)
    )
    if not compatible:
        return (Value.unavailable("NQ/ES source times or baseline periods incompatible"),
                RelativeStrength.UNKNOWN)
    return relative_strength(_change_fields(nq)[2], _change_fields(es)[2],
                             tolerance_pct=tolerance_pct)


def build_cross_market_state(
    market: MarketState,
    *,
    now: datetime | None = None,
    tolerance_pct: float = DEFAULT_TOLERANCE_PCT,
) -> CrossMarketState:
    """Build the cross-market state from the current market state."""
    instruments = market.instruments
    nq = instruments.get("NQ")
    es = instruments.get("ES")

    _nq_price, nq_change, nq_change_pct = _change_fields(nq)
    _es_price, es_change, es_change_pct = _change_fields(es)
    nq_vs_es, label = compare_nq_es(nq, es, tolerance_pct=tolerance_pct)

    return CrossMarketState(
        generated_at=now or market.generated_at,
        nq_change_pct=nq_change_pct,
        es_change_pct=es_change_pct,
        nq_vs_es=nq_vs_es,
        nq_vs_es_label=label,
        tolerance_pct=tolerance_pct,
        nq_data_status=nq.status if nq is not None else DataStatus.UNAVAILABLE,
        es_data_status=es.status if es is not None else DataStatus.UNAVAILABLE,
        nq_timestamp=nq.timestamp if nq is not None else None,
        es_timestamp=es.timestamp if es is not None else None,
        nq_feed_status=_effective_feed_status(nq),
        es_feed_status=_effective_feed_status(es),
        us2y=cross_asset_change(instruments.get("US2Y")),
        us10y=cross_asset_change(instruments.get("US10Y")),
        dxy=cross_asset_change(instruments.get("DXY")),
        vix=cross_asset_change(instruments.get("VIX")),
        vxn=cross_asset_change(instruments.get("VXN")),
    )


__all__ = [
    "DEFAULT_TOLERANCE_PCT",
    "build_cross_market_state",
    "compare_nq_es",
    "cross_asset_change",
    "relative_strength",
]
