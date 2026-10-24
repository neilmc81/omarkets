"""Typed payloads for the chart, the session calendar, key levels and risk tone.

Everything here is computed by the core (``core.analysis.bars``,
``core.analysis.levels``, ``core.analysis.risk_tone``, ``core.desk.pins``); the
web app only draws it. Times are UTC. A value the core could not compute is
``None`` (never zero), with the reason alongside where one is known.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from core.domain.sessions import SessionPhase, SessionSegment


class Interval(StrEnum):
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"


class ChartRange(StrEnum):
    DAY = "1d"
    FIVE_DAYS = "5d"
    MONTH = "1m"


INTERVAL_SECONDS: dict[Interval, int] = {Interval.M5: 300, Interval.M15: 900, Interval.H1: 3600}


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ChartSession(_Frozen):
    """One Globex session in a chart window, with its display segments."""

    trade_date: date
    opens_at: AwareDatetime
    closes_at: AwareDatetime
    rth_opens_at: AwareDatetime
    rth_closes_at: AwareDatetime
    early_close: bool
    segments: list[SessionSegment]


class Bar(_Frozen):
    """One OHLC bar. A gap inside a session is a bar with ``count == 0`` and no prices."""

    time: AwareDatetime
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    #: Stored observations in the bar (0 for a gap).
    count: int = Field(ge=0)
    #: Close as a % change from the first bar's open in the range (overlays).
    change_pct: float | None
    delayed: bool
    source: str | None
    contract: str | None


class BarsResponse(_Frozen):
    symbol: str
    label: str
    unit: str
    interval: Interval
    range: ChartRange
    generated_at: AwareDatetime
    #: The window the bars cover: the first session's open to now (or the last close).
    start: AwareDatetime
    end: AwareDatetime
    sessions: list[ChartSession]
    bars: list[Bar]
    #: Exchange time of the newest observation in the window.
    last_at: AwareDatetime | None
    #: Any bar from a delayed feed; ``feed_status`` / ``status`` are the quote's now.
    delayed: bool
    feed_status: str
    status: str
    #: The quote is old only because the market is shut (show the last session).
    market_closed: bool = False
    sources: list[str]
    contracts: list[str]
    #: Bars before this time are built from one stored quote per minute.
    fine_since: AwareDatetime
    note: str | None = None


class SessionCalendar(_Frozen):
    """Where now sits in the futures day: the timeline bar on Today."""

    now: AwareDatetime
    phase: SessionPhase
    text: str
    session: ChartSession
    #: The next daily maintenance break (or None when the next open is not tomorrow).
    maintenance_starts_at: AwareDatetime | None
    maintenance_ends_at: AwareDatetime | None
    next_open_at: AwareDatetime | None
    holidays: list[Holiday]


class Holiday(_Frozen):
    date: date
    name: str
    early_close: bool = False


class KeyLevel(_Frozen):
    key: Literal["overnight_high", "overnight_low", "prior_settle", "prior_day_high",
                 "prior_day_low", "prior_week_high", "prior_week_low", "gap_vs_settle"]
    label: str
    value: float | None
    value_text: str
    #: Plain-English rule of thumb; explanation, not advice.
    explainer: str
    #: The stretch of time the level is measured over (None for the settle).
    window_start: AwareDatetime | None = None
    window_end: AwareDatetime | None = None
    #: True while the window is still running (e.g. the overnight so far).
    partial: bool = False
    unavailable_reason: str | None = None


class KeyLevels(_Frozen):
    symbol: str
    generated_at: AwareDatetime
    trade_date: date
    contract: str | None
    delayed: bool
    levels: list[KeyLevel]


class RiskInput(_Frozen):
    symbol: str
    label: str
    value: float | None
    change: float | None
    change_text: str
    #: +1 helps risk appetite, -1 hurts it, 0 neutral; None when left out.
    signal: Literal[-1, 0, 1] | None
    used: bool
    note: str


class RiskTone(_Frozen):
    generated_at: AwareDatetime
    tone: Literal["risk_on", "leaning_risk_on", "quiet", "mixed", "leaning_risk_off",
                  "risk_off", "unclear", "closed"]
    label: str
    #: One plain sentence per input, strongest first.
    reasons: list[str]
    inputs: list[RiskInput]
    rules: str


class ChartPin(_Frozen):
    """A scheduled release on the chart. ``note_id`` links its desk note."""

    at: AwareDatetime
    title: str
    importance: Literal["high", "medium"]
    released: bool
    #: e.g. "Lower than forecast"; None before the release or without a forecast.
    result_text: str | None
    explainer: str | None
    #: NQ's % change in the first 15 minutes, measured from stored quotes.
    nq_15m: float | None
    nq_15m_text: str | None
    note_id: str | None


class ChartPins(_Frozen):
    range: ChartRange
    start: AwareDatetime
    end: AwareDatetime
    pins: list[ChartPin]


class SparklinePoint(_Frozen):
    time: AwareDatetime
    value: float | None


class Sparkline(_Frozen):
    symbol: str
    points: list[SparklinePoint]


class Sparklines(_Frozen):
    generated_at: AwareDatetime
    interval: Interval
    #: One line per strip instrument, ending at that instrument's latest quote.
    lines: list[Sparkline]


SessionCalendar.model_rebuild()

__all__ = [
    "Bar",
    "BarsResponse",
    "ChartPin",
    "ChartPins",
    "ChartRange",
    "ChartSession",
    "Holiday",
    "INTERVAL_SECONDS",
    "Interval",
    "KeyLevel",
    "KeyLevels",
    "RiskInput",
    "RiskTone",
    "SessionCalendar",
    "Sparkline",
    "SparklinePoint",
    "Sparklines",
]
