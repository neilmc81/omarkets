"""Typed domain schemas for the Omarkets core.

These models are the contract between providers, storage, services, and the
API. They are validated at every boundary; loose dicts are not an interface.

Timestamp rules (see ``docs/architecture.md`` §5):

* all timestamps are timezone-aware and stored in UTC,
* naive datetimes are rejected rather than guessed,
* market timestamps (event time) and retrieval timestamps are distinct.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from core.domain.instruments import (
    INSTRUMENTS,
    AssetClass,
    QuoteType,
    get_instrument,
)


def utc_now() -> datetime:
    """Current time as a timezone-aware UTC datetime.

    Centralized so it can be monkeypatched in tests and so the core never
    constructs a naive ``datetime``.
    """
    return datetime.now(UTC)


def to_utc(value: datetime) -> datetime:
    """Convert an aware datetime to UTC."""
    if value.tzinfo is None:
        raise ValueError("naive datetime is not allowed for market data")
    return value.astimezone(UTC)


class DataStatus(StrEnum):
    """Freshness of a value. Never conflate these."""

    LIVE = "live"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class FeedStatus(StrEnum):
    """Quality of a feed independently of freshness.

    ``LIVE`` and ``DELAYED`` are real feeds; ``DEMO`` is synthetic and must
    never be presented as real. ``UNAVAILABLE`` means no usable feed.
    """

    LIVE = "live"
    DELAYED = "delayed"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    DEMO = "demo"


class ProviderStatus(StrEnum):
    """Health of the data provider feeding the state service."""

    OK = "ok"
    DEGRADED = "degraded"
    ERROR = "error"
    STOPPED = "stopped"


class MarketObservation(BaseModel):
    """A single timestamped observation of one instrument.

    ``metadata`` may carry provider-specific extras but must not be used to
    smuggle vendor SDK types across a boundary.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1)
    timestamp: AwareDatetime
    value: float = Field(allow_inf_nan=False)
    unit: str = Field(min_length=1)
    source: str = Field(min_length=1)
    received_at: AwareDatetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timestamp", "received_at")
    @classmethod
    def _require_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value

    @field_validator("timestamp", "received_at")
    @classmethod
    def _as_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @field_validator("symbol")
    @classmethod
    def _canonical(cls, value: str) -> str:
        canonical = value.strip().upper()
        if canonical not in INSTRUMENTS:
            raise ValueError(f"unknown instrument symbol: {value!r}")
        return canonical

    @field_validator("unit")
    @classmethod
    def _unit_matches_instrument(cls, value: str, info) -> str:
        symbol = info.data.get("symbol")
        if symbol is None:
            return value
        instrument = get_instrument(symbol)
        if instrument is not None and value != instrument.unit:
            raise ValueError(f"unit {value!r} does not match {symbol} unit {instrument.unit!r}")
        return value


class PriceQuote(BaseModel):
    """Price-like detail for an instrument where OHLC/volume is meaningful.

    Rates and yields may not have a meaningful open/high/low; those fields are
    optional and are simply omitted for such instruments.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1)
    last: float = Field(allow_inf_nan=False)
    bid: float | None = Field(default=None, allow_inf_nan=False)
    ask: float | None = Field(default=None, allow_inf_nan=False)
    open: float | None = Field(default=None, allow_inf_nan=False)
    high: float | None = Field(default=None, allow_inf_nan=False)
    low: float | None = Field(default=None, allow_inf_nan=False)
    previous_close: float | None = Field(default=None, allow_inf_nan=False)
    volume: int | None = Field(default=None, ge=0)
    timestamp: AwareDatetime
    source: str = Field(min_length=1)

    @field_validator("symbol")
    @classmethod
    def _canonical(cls, value: str) -> str:
        canonical = value.strip().upper()
        if canonical not in INSTRUMENTS:
            raise ValueError(f"unknown instrument symbol: {value!r}")
        return canonical

    @field_validator("timestamp")
    @classmethod
    def _as_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class InstrumentState(BaseModel):
    """The current normalized state of one instrument in the market state.

    ``change`` and ``change_pct`` are ``None`` when no baseline is known. They
    are never fabricated as zero, so a consumer can distinguish "unchanged"
    from "baseline unavailable".
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    display_name: str
    asset_class: AssetClass
    quote_type: QuoteType
    unit: str
    value: float | None = Field(default=None, allow_inf_nan=False)
    previous_close: float | None = Field(default=None, allow_inf_nan=False)
    change: float | None = Field(default=None, allow_inf_nan=False)
    change_pct: float | None = Field(default=None, allow_inf_nan=False)
    timestamp: AwareDatetime | None = None
    received_at: AwareDatetime | None = None
    age_seconds: float | None = Field(default=None, ge=0)
    stale_after_seconds: float = Field(gt=0)
    status: DataStatus
    source: str | None = None
    feed_status: FeedStatus = FeedStatus.UNAVAILABLE
    provider_symbol: str | None = None
    contract_code: str | None = None
    contract_expiry: str | None = None
    latency_seconds: float | None = Field(default=None, ge=0)
    expected_delay_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)
    excess_age_seconds: float | None = Field(default=None, ge=0)
    baseline_period: str | None = None
    #: Stale only because the market is shut (weekend, holiday, daily break):
    #: the value is the last print of the latest session, not a broken feed.
    market_closed: bool = False


class MarketState(BaseModel):
    """A single structured snapshot of "what is true now".

    ``instruments`` always contains every instrument in the universe. Unknown
    or missing data is represented by an ``unavailable`` state, never omitted.
    """

    generated_at: AwareDatetime
    provider: str
    provider_status: ProviderStatus
    instruments: dict[str, InstrumentState]
    live_count: int = Field(ge=0)
    stale_count: int = Field(ge=0)
    unavailable_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _counters_match_statuses(self) -> MarketState:
        """Keep the summary counters honest with respect to the instruments."""
        live = sum(1 for s in self.instruments.values() if s.status is DataStatus.LIVE)
        stale = sum(1 for s in self.instruments.values() if s.status is DataStatus.STALE)
        unavailable = sum(
            1 for s in self.instruments.values() if s.status is DataStatus.UNAVAILABLE
        )
        if (self.live_count, self.stale_count, self.unavailable_count) != (
            live,
            stale,
            unavailable,
        ):
            raise ValueError(
                "market state counters do not match instrument statuses: "
                f"reported ({self.live_count}, {self.stale_count}, "
                f"{self.unavailable_count}) != actual ({live}, {stale}, {unavailable})"
            )
        return self
