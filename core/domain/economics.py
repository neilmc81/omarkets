from __future__ import annotations

import hashlib
import re
from datetime import UTC, date, datetime
from enum import StrEnum
from math import isfinite
from typing import Annotated, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    model_validator,
)

from core.domain.models import DataStatus


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _valid_period(value: str) -> str:
    if re.fullmatch(r"[0-9]{4}-Q[1-4]", value):
        date(int(value[:4]), 1, 1)
    elif re.fullmatch(r"[0-9]{4}-[0-9]{2}", value):
        date.fromisoformat(f"{value}-01")
    elif re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        date.fromisoformat(value)
    else:
        raise ValueError("period must be YYYY-MM, YYYY-MM-DD, or YYYY-Qn")
    return value


def _valid_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("timezone must be an IANA timezone") from exc
    return value


UTCDatetime = Annotated[AwareDatetime, AfterValidator(_as_utc)]
MacroPeriod = Annotated[str, AfterValidator(_valid_period)]
NonEmptyString = Annotated[str, Field(min_length=1)]
EconomicTimezone = Annotated[str, AfterValidator(_valid_timezone)]


class EconomicsMode(StrEnum):
    LIVE = "live"
    DEMO = "demo"
    DISABLED = "disabled"


class MacroCategory(StrEnum):
    INFLATION = "inflation"
    EMPLOYMENT = "employment"
    GROWTH = "growth"
    HOUSING = "housing"
    POLICY = "policy"


class Importance(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class EventStatus(StrEnum):
    SCHEDULED = "scheduled"
    DUE = "due"
    RELEASED = "released"
    DELAYED = "delayed"
    CANCELLED = "cancelled"
    MISSING = "missing"
    UNKNOWN = "unknown"


class EconomicProviderStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    NOT_CONFIGURED = "not_configured"


class EconomicStatement(BaseModel):
    """Published policy text and the exact extraction's provenance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(min_length=100, max_length=40_000)
    source_url: NonEmptyString
    published_at: UTCDatetime
    received_at: UTCDatetime
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def _valid_provenance(self) -> EconomicStatement:
        parts = urlsplit(self.source_url)
        published_day = self.published_at.astimezone(ZoneInfo("America/New_York"))
        if (
            parts.scheme != "https"
            or parts.netloc != "www.federalreserve.gov"
            or parts.query
            or parts.fragment
            or parts.path != (f"/newsevents/pressreleases/monetary{published_day:%Y%m%d}a.htm")
        ):
            raise ValueError("statement requires matching official published statement URL")
        if self.published_at > self.received_at:
            raise ValueError("statement publication cannot follow retrieval")
        if hashlib.sha256(self.text.encode("utf-8")).hexdigest() != self.content_sha256:
            raise ValueError("statement content hash does not match text")
        return self


class EconomicSourceRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: NonEmptyString
    event_code: NonEmptyString
    name: NonEmptyString
    country: NonEmptyString = "US"
    category: MacroCategory
    importance: Importance
    scheduled_at: UTCDatetime | None = None
    timezone: EconomicTimezone = "America/New_York"
    period: str | None = None
    unit: NonEmptyString
    frequency: NonEmptyString
    status: EventStatus
    statement: EconomicStatement | None = None
    actual: FiniteFloat | None = None
    actual_basis: Literal["current_vintage", "source_release"] | None = None
    consensus: FiniteFloat | None = None
    previous: FiniteFloat | None = None
    revised_previous: FiniteFloat | None = None
    raw_surprise: FiniteFloat | None = None
    provider: NonEmptyString
    source_event_id: NonEmptyString
    source_url: str | None = None
    released_at: UTCDatetime | None = None
    received_at: UTCDatetime
    source_revision: str | None = None
    revision: int = Field(default=1, ge=1)
    notes: list[str] = Field(default_factory=list)
    mode: Literal[EconomicsMode.LIVE, EconomicsMode.DEMO]

    @model_validator(mode="after")
    def _compute_surprise(self) -> EconomicSourceRecord:
        expected = None
        if self.actual is not None and self.consensus is not None:
            expected = self.actual - self.consensus
            if not isfinite(expected):
                raise ValueError("actual - consensus must be finite")
        if self.raw_surprise is not None and self.raw_surprise != expected:
            raise ValueError("raw_surprise must equal actual - consensus, or null without both")
        object.__setattr__(self, "raw_surprise", expected)
        return self


class EconomicEvent(EconomicSourceRecord):
    """Source event or canonical occurrence with explicit source-ID aliases."""

    occurrence_aliases: list[str] = Field(default_factory=list)
    occurrence_sources: list[EconomicSourceRecord] = Field(default_factory=list)


class MacroDefinition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: NonEmptyString
    name: NonEmptyString
    category: MacroCategory
    unit: NonEmptyString
    frequency: NonEmptyString
    source_series_id: NonEmptyString
    provider: NonEmptyString
    transform: str | None = None
    info: str | None = None


class MacroRegistry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    definitions: tuple[MacroDefinition, ...] = ()

    @model_validator(mode="after")
    def _unique_ids(self) -> MacroRegistry:
        ids = [definition.id for definition in self.definitions]
        if len(ids) != len(set(ids)):
            raise ValueError("macro definition ids must be unique")
        return self

    def get(self, series_id: str) -> MacroDefinition | None:
        return next((item for item in self.definitions if item.id == series_id), None)


class MacroInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_series_id: NonEmptyString
    provider: NonEmptyString
    period: MacroPeriod
    value: FiniteFloat | None = None
    unit: NonEmptyString
    released_at: UTCDatetime | None = None
    received_at: UTCDatetime
    source_revision: str | None = None


class MacroProvenance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_url: str | None = None
    source_revision: str | None = None
    transform: str | None = None
    inputs: list[MacroInput] = Field(default_factory=list)


class MacroObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: NonEmptyString
    period: MacroPeriod
    value: FiniteFloat | None = None
    unit: NonEmptyString
    source_series_id: NonEmptyString
    provider: NonEmptyString
    released_at: UTCDatetime | None = None
    received_at: UTCDatetime
    revision: int = Field(default=1, ge=1)
    notes: list[str] = Field(default_factory=list)
    metadata: MacroProvenance = Field(default_factory=MacroProvenance)


class MacroProviderHealth(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: NonEmptyString
    status: EconomicProviderStatus
    checked_at: UTCDatetime | None = None
    detail: str | None = None


class MacroSeriesState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    definition: MacroDefinition
    latest: MacroObservation | None = None
    status: DataStatus
    last_checked_at: UTCDatetime | None = None
    recent: list[MacroObservation] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent_latest(self) -> MacroSeriesState:
        if self.latest is not None:
            for field in ("id", "unit", "provider", "source_series_id"):
                if getattr(self.latest, field) != getattr(self.definition, field):
                    raise ValueError(f"latest observation {field} does not match definition")
        if self.status != DataStatus.UNAVAILABLE and (
            self.latest is None or self.latest.value is None
        ):
            raise ValueError("live or stale series requires a numeric latest observation")
        if len(self.recent) > 60:
            raise ValueError("recent observations must be bounded")
        for item in self.recent:
            if item.id != self.definition.id or item.unit != self.definition.unit:
                raise ValueError("recent observation does not match definition")
        return self


class MacroState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    generated_at: UTCDatetime
    mode: EconomicsMode
    series: list[MacroSeriesState] = Field(default_factory=list)
    providers: list[MacroProviderHealth] = Field(default_factory=list)
    upcoming_events: list[EconomicEvent] = Field(default_factory=list)


__all__ = [
    "EconomicEvent",
    "EconomicStatement",
    "EconomicProviderStatus",
    "EconomicsMode",
    "EventStatus",
    "Importance",
    "MacroCategory",
    "MacroDefinition",
    "MacroInput",
    "MacroObservation",
    "MacroPeriod",
    "MacroProvenance",
    "MacroProviderHealth",
    "MacroRegistry",
    "MacroSeriesState",
    "MacroState",
    "UTCDatetime",
]
