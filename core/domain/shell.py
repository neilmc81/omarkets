"""Typed schemas for the native Omarchy shell integration (Phase 8).

These are deliberately *not* the internal domain models. They are a small,
versioned, presentation-oriented contract that the shell consumes, so QML never
depends on database rows or internal object graphs. Every value is either copied
verbatim from a deterministic service or explicitly marked unavailable; nothing
here is computed by the shell.

Two rules encoded here:

* **Stale is never live.** A quote carries its ``feed_status`` and ``status``
  exactly as the core classified them. The shell renders; it does not judge.
* **Data health and AI health are separate.** A degraded desk writer must not
  make healthy market data look down, and vice versa.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from core.domain.economics import UTCDatetime
from core.domain.models import DataStatus, FeedStatus
from core.domain.news import NonEmptyString

#: Version of the shell contract. Bump on a breaking change so a stale shell can
#: detect a mismatch instead of mis-rendering.
SHELL_SCHEMA_VERSION = 2


class ShellHealth(StrEnum):
    """Coarse health for a shell consumer. Deliberately small and stable."""

    OK = "ok"
    DEGRADED = "degraded"
    DISABLED = "disabled"
    DOWN = "down"
    UNKNOWN = "unknown"


class ShellQuote(BaseModel):
    """One instrument's compact quote.

    ``value``/``change``/``change_pct`` are ``None`` when unavailable — never
    zero. ``feed_status`` and ``status`` are the core's own classification.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: NonEmptyString
    label: str | None = None
    value: float | None = Field(default=None, allow_inf_nan=False)
    change: float | None = Field(default=None, allow_inf_nan=False)
    change_pct: float | None = Field(default=None, allow_inf_nan=False)
    #: Change in basis points; only meaningful for yields.
    change_bp: float | None = Field(default=None, allow_inf_nan=False)
    feed_status: FeedStatus = FeedStatus.UNAVAILABLE
    status: DataStatus = DataStatus.UNAVAILABLE
    #: Old only because the market is shut; show CLOSED rather than STALE.
    market_closed: bool = False
    as_of: UTCDatetime | None = None
    age_seconds: float | None = Field(default=None, ge=0)


class ShellSession(BaseModel):
    """Where now sits in the futures trading cycle.

    ``maintenance_break`` is a *schedule* fact, distinct from a feed being stale.
    A planned closure must never be rendered as a provider failure.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    phase: str
    maintenance_break: bool = False
    next_open_at: UTCDatetime | None = None
    #: ``next_open_at`` in the core's local zone, e.g. "Mon 05:00 EDT".
    next_open_local: str | None = None


class ShellEvent(BaseModel):
    """An upcoming economic event, with display strings pre-rendered.

    ``time_et`` and ``time_local`` are formatted by the core (which owns
    timezones) so the shell performs no timezone math. ``scheduled_at`` is the
    authoritative UTC instant for any countdown.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str | None = None
    name: NonEmptyString
    event_code: str | None = None
    country: str | None = None
    importance: str | None = None
    status: str | None = None
    scheduled_at: UTCDatetime | None = None
    time_et: str | None = None
    time_local: str | None = None
    actual: float | None = Field(default=None, allow_inf_nan=False)
    consensus: float | None = Field(default=None, allow_inf_nan=False)
    previous: float | None = Field(default=None, allow_inf_nan=False)
    unit: str | None = None


class ShellRiskTone(BaseModel):
    """The core's rule-based risk tone, as one line for the popup."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tone: NonEmptyString
    label: NonEmptyString
    #: e.g. "Risk tone: mixed · 2-year yield +2.5bp: rising yields usually weigh…"
    line: NonEmptyString


class ShellProviderHealth(BaseModel):
    """One subsystem's health for the diagnostics view."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: NonEmptyString
    health: ShellHealth
    detail: str | None = None


class ShellStatus(BaseModel):
    """The bar-widget payload: compact, one request, safe defaults.

    ``health`` carries separate ``data`` and ``ai`` fields so a degraded AI layer
    never reads as a dead workstation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = Field(default=SHELL_SCHEMA_VERSION, ge=1)
    generated_at: UTCDatetime
    #: Primary instrument (NQ) quote, or None when no state is known.
    primary: ShellQuote | None = None
    #: The glanceable strip (NQ, US2Y, VIX, ...).
    strip: list[ShellQuote] = Field(default_factory=list)
    session: ShellSession
    next_event: ShellEvent | None = None
    #: Rule-based risk tone (``core.analysis.risk_tone``); None without market state.
    risk_tone: ShellRiskTone | None = None
    data_health: ShellHealth = ShellHealth.UNKNOWN
    ai_health: ShellHealth = ShellHealth.UNKNOWN
    providers: list[ShellProviderHealth] = Field(default_factory=list)


class ShellContext(BaseModel):
    """The popup payload: a little more context than the bar, still compact."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = Field(default=SHELL_SCHEMA_VERSION, ge=1)
    generated_at: UTCDatetime
    cross_market: list[ShellQuote] = Field(default_factory=list)
    upcoming_events: list[ShellEvent] = Field(default_factory=list)
    diagnostics: list[ShellProviderHealth] = Field(default_factory=list)


__all__ = [
    "SHELL_SCHEMA_VERSION",
    "ShellContext",
    "ShellEvent",
    "ShellHealth",
    "ShellProviderHealth",
    "ShellQuote",
    "ShellRiskTone",
    "ShellSession",
    "ShellStatus",
]
