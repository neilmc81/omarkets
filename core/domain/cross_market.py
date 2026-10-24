"""Typed cross-market context: NQ against yields, the dollar and volatility.

Every computed value is wrapped in :class:`Value` so the API and UI can always
distinguish a real number from ``unavailable`` or ``insufficient_data``. Omarkets
never substitutes zero or a fabricated value for missing data.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, Field

from core.domain.models import DataStatus, FeedStatus


class Availability(StrEnum):
    """Whether a value could be computed, and if not, why not."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    INSUFFICIENT_DATA = "insufficient_data"


class Value(BaseModel):
    """A scalar that is explicitly available, unavailable, or insufficient.

    ``reason`` is required for anything but ``available`` so a consumer is never
    left guessing why a number is missing.
    """

    model_config = {"frozen": True}

    value: float | None = Field(default=None, allow_inf_nan=False)
    availability: Availability = Availability.UNAVAILABLE
    reason: str | None = None

    @classmethod
    def available(cls, value: float) -> Value:
        return cls(value=value, availability=Availability.AVAILABLE)

    @classmethod
    def unavailable(cls, reason: str) -> Value:
        return cls(value=None, availability=Availability.UNAVAILABLE, reason=reason)

    @classmethod
    def insufficient(cls, reason: str) -> Value:
        return cls(value=None, availability=Availability.INSUFFICIENT_DATA, reason=reason)


class RelativeStrength(StrEnum):
    OUTPERFORMING = "outperforming"
    ALIGNED = "aligned"
    UNDERPERFORMING = "underperforming"
    UNKNOWN = "unknown"


class CrossAssetChange(BaseModel):
    """One cross-asset instrument's level and change."""

    symbol: str
    price: Value
    change: Value
    change_pct: Value
    #: Change in basis points; only meaningful for yields.
    change_bp: Value
    timestamp: AwareDatetime | None = None
    feed_status: FeedStatus = FeedStatus.UNAVAILABLE
    status: DataStatus = DataStatus.UNAVAILABLE


class CrossMarketState(BaseModel):
    """NQ measured against its cross-asset context.

    Phase 4 states relationships only. It never asserts that one instrument
    caused another to move.
    """

    generated_at: AwareDatetime
    nq_change_pct: Value
    es_change_pct: Value
    nq_vs_es: Value
    nq_vs_es_label: RelativeStrength = RelativeStrength.UNKNOWN
    tolerance_pct: float = Field(gt=0)
    #: Feed quality of the NQ/ES legs, so a consumer never shows a stale leg as
    #: live. The relative-strength value itself is withheld when a leg is stale.
    nq_timestamp: AwareDatetime | None = None
    es_timestamp: AwareDatetime | None = None
    nq_data_status: DataStatus | None = None
    es_data_status: DataStatus | None = None
    nq_feed_status: FeedStatus = FeedStatus.UNAVAILABLE
    es_feed_status: FeedStatus = FeedStatus.UNAVAILABLE
    us2y: CrossAssetChange
    us10y: CrossAssetChange
    dxy: CrossAssetChange
    vix: CrossAssetChange
    vxn: CrossAssetChange


__all__ = [
    "Availability",
    "CrossAssetChange",
    "CrossMarketState",
    "RelativeStrength",
    "Value",
]
