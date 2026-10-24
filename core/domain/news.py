"""Typed domain schemas for the Phase 5 news / geopolitical event pipeline.

The pipeline is deterministic and source-agnostic::

    SOURCE -> FETCH -> NORMALIZE -> DEDUPLICATE -> STORE -> STREAM

Every incoming item becomes a :class:`SourceItem` at the adapter boundary and a
:class:`MarketEvent` after normalization. An event may be reported by several
sources; the canonical :class:`MarketEvent` is deduplicated while each source's
:class:`SourceOccurrence` is retained, so Omarkets can say "three independent
feeds reported this".

Nothing here performs relevance, novelty, materiality or market classification.
That work is deliberately deferred to Phase 6 (TypeSafe Jev). ``event_type`` is
only ever a source-provided category or a deterministic keyword mapping; it is
never an LLM judgement.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    model_validator,
)

from core.domain.instruments import INSTRUMENTS


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


UTCDatetime = Annotated[AwareDatetime, AfterValidator(_as_utc)]
NonEmptyString = Annotated[str, Field(min_length=1)]


class SourceType(StrEnum):
    """Broad family of an event source.

    Extensible: new adapters pick an existing family or a documented new one.
    """

    NEWS = "news"
    ECONOMIC_CALENDAR = "economic_calendar"
    CENTRAL_BANK = "central_bank"
    GOVERNMENT = "government"
    GEOPOLITICAL = "geopolitical"
    COMMODITY = "commodity"
    MARKET_DATA_EVENT = "market_data_event"
    SOCIAL = "social"


class IngestionStatus(StrEnum):
    """Where an item is in the deterministic pipeline."""

    RECEIVED = "received"
    NORMALIZED = "normalized"
    STORED = "stored"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"
    ERROR = "error"


class SourceStatus(StrEnum):
    """Health of a source adapter."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    DISABLED = "disabled"


class EntityKind(StrEnum):
    """Kind of a deterministically extracted entity."""

    COUNTRY = "country"
    REGION = "region"
    INSTITUTION = "institution"
    INSTRUMENT = "instrument"
    COMPANY = "company"
    ASSET = "asset"
    PERSON = "person"


#: Documented vocabulary of deterministic/source-provided event types. Adapters
#: may emit other values; consumers must treat unknown values as opaque strings.
KNOWN_EVENT_TYPES: tuple[str, ...] = (
    "monetary_policy",
    "rate_decision",
    "speech",
    "statement",
    "minutes",
    "testimony",
    "economic_release",
    "inflation",
    "employment",
    "growth",
    "housing",
    "sanctions",
    "tariffs",
    "trade",
    "military_conflict",
    "diplomacy",
    "election",
    "shipping_disruption",
    "energy_supply",
    "supply_chain",
    "corporate",
    "market",
)


class EntityRef(BaseModel):
    """A deterministic entity mention within an event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EntityKind
    id: NonEmptyString
    label: str = Field(min_length=1)


class CentralBankMetadata(BaseModel):
    """Deterministic central-bank metadata extracted where possible.

    No semantic interpretation is performed here. ``speaker`` and
    ``document_type`` come from source structure or direct title matching.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    central_bank: NonEmptyString
    speaker: str | None = None
    document_type: str | None = None
    meeting_date: date | None = None
    decision: str | None = None


class EconomicCalendarDetail(BaseModel):
    """Scheduled macro release carried on an event.

    Mirrors the Phase 2 :class:`~core.domain.economics.EconomicEvent` numeric
    fields without re-deriving them. Actual/forecast/previous are only ever the
    values the source supplied; they are never invented or computed here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_code: NonEmptyString
    name: NonEmptyString
    country: str = Field(min_length=1, default="US")
    period: str | None = None
    unit: NonEmptyString
    importance: str | None = None
    status: str | None = None
    actual: FiniteFloat | None = None
    consensus: FiniteFloat | None = None
    previous: FiniteFloat | None = None
    revised_previous: FiniteFloat | None = None


class SourceMetadata(BaseModel):
    """Static description of a source adapter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: NonEmptyString
    display_name: NonEmptyString
    source_type: SourceType
    homepage: str | None = None
    language: str = "en"
    official: bool = False
    reliable: bool = False
    requires_credentials: bool = False
    attribution: str | None = None
    notes: list[str] = Field(default_factory=list)


class SourceItem(BaseModel):
    """Adapter output: one fetched item, before shared normalization.

    A raw vendor payload lives in ``raw`` and is persisted verbatim by storage.
    Adapters must not leak vendor SDK types past this boundary.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: NonEmptyString
    source_type: SourceType
    source_id: NonEmptyString
    source_url: str | None = None
    headline: NonEmptyString
    summary: str | None = None
    published_at: UTCDatetime | None = None
    published_at_assumed_utc: bool = False
    received_at: UTCDatetime
    language: str = "en"
    event_type: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)
    central_bank: CentralBankMetadata | None = None
    economic: EconomicCalendarDetail | None = None


class MarketEvent(BaseModel):
    """Canonical normalized event in the unified market event stream.

    The same real-world event reported by several sources yields one
    :class:`MarketEvent` plus one :class:`SourceOccurrence` per source report.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: NonEmptyString
    source: NonEmptyString
    source_type: SourceType
    source_id: NonEmptyString
    source_url: str | None = None
    headline: NonEmptyString
    summary: str | None = None
    published_at: UTCDatetime | None = None
    received_at: UTCDatetime
    language: str = "en"
    event_type: str | None = None
    entities: list[EntityRef] = Field(default_factory=list)
    instruments: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    raw_payload: dict[str, Any] | None = None
    central_bank: CentralBankMetadata | None = None
    economic: EconomicCalendarDetail | None = None
    deduplication_key: NonEmptyString
    ingestion_status: IngestionStatus = IngestionStatus.STORED
    revision: int = Field(default=1, ge=1)
    first_seen_at: UTCDatetime | None = None
    last_seen_at: UTCDatetime | None = None
    occurrence_count: int = Field(default=1, ge=1)
    sources: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _canonical_values(self) -> MarketEvent:
        unknown = sorted(set(self.instruments) - set(INSTRUMENTS))
        if unknown:
            raise ValueError(f"instruments must be canonical symbols, got {unknown}")
        if len(set(self.instruments)) != len(self.instruments):
            raise ValueError("instruments must be unique")
        if len(set(self.countries)) != len(self.countries):
            raise ValueError("countries must be unique")
        return self


class SourceOccurrence(BaseModel):
    """One source's report of a canonical event.

    ``occurrence_key`` is ``source:source_id`` and makes re-fetching a feed
    idempotent even when two feeds carry the same story.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    occurrence_key: NonEmptyString
    event_id: NonEmptyString
    source: NonEmptyString
    source_type: SourceType
    source_id: NonEmptyString
    source_url: str | None = None
    headline: NonEmptyString
    published_at: UTCDatetime | None = None
    received_at: UTCDatetime
    fingerprint: NonEmptyString
    raw_seq: int | None = Field(default=None, ge=1)


class RawEvent(BaseModel):
    """Verbatim fetched payload, retained after normalization."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: NonEmptyString
    source_id: NonEmptyString
    source_url: str | None = None
    fetched_at: UTCDatetime
    content_hash: NonEmptyString
    payload: dict[str, Any]


class SourceHealth(BaseModel):
    """Runtime health of one source adapter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: NonEmptyString
    source_type: SourceType
    status: SourceStatus
    enabled: bool = True
    last_attempt_at: UTCDatetime | None = None
    last_success_at: UTCDatetime | None = None
    last_error: str | None = None
    recent_event_count: int = Field(default=0, ge=0)
    consecutive_failures: int = Field(default=0, ge=0)
    detail: str | None = None

    @model_validator(mode="after")
    def _disabled_is_explicit(self) -> SourceHealth:
        if not self.enabled and self.status != SourceStatus.DISABLED:
            raise ValueError("a disabled source must report status 'disabled'")
        return self


__all__ = [
    "KNOWN_EVENT_TYPES",
    "CentralBankMetadata",
    "EconomicCalendarDetail",
    "EntityKind",
    "EntityRef",
    "IngestionStatus",
    "MarketEvent",
    "RawEvent",
    "SourceHealth",
    "SourceItem",
    "SourceMetadata",
    "SourceOccurrence",
    "SourceStatus",
    "SourceType",
    "UTCDatetime",
]
