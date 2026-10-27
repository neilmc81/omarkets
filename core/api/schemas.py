"""HTTP response schemas for the Omarkets API.

These are API contracts, distinct from the domain models though often derived
from them. Keeping them explicit means internal refactors do not silently
change the wire format.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from core.domain.economics import MacroProviderHealth
from core.domain.models import InstrumentState


class NewsHealth(BaseModel):
    """Health of the deterministic news/geopolitical ingestion service."""

    enabled: bool
    source_count: int = Field(ge=0)
    healthy_sources: int = Field(ge=0)
    degraded_sources: int = Field(ge=0)
    offline_sources: int = Field(ge=0)
    disabled_sources: int = Field(ge=0)
    event_count: int = Field(ge=0)
    subscriber_count: int = Field(ge=0)


class HealthResponse(BaseModel):
    """Overall application health."""

    status: str
    app: str
    version: str
    time: datetime
    database_ok: bool
    provider: str
    provider_status: str
    provider_error: str | None = None
    stream_running: bool
    cached_instruments: int = Field(ge=0)
    updates_published: int = Field(ge=0)
    last_update_at: datetime | None = None
    subscriber_count: int = Field(ge=0)
    economic_providers: list[MacroProviderHealth] = Field(default_factory=list)
    news: NewsHealth | None = None


class InstrumentDetail(BaseModel):
    """An instrument definition plus its current state."""

    symbol: str
    display_name: str
    asset_class: str
    quote_type: str
    timezone: str
    unit: str
    venue: str
    currency: str | None
    session_calendar: str
    session_calendar_label: str
    price_precision: int = Field(ge=0)
    stale_after_seconds: float
    metadata: dict[str, str]
    state: InstrumentState


class InstrumentsResponse(BaseModel):
    generated_at: datetime
    count: int = Field(ge=0)
    instruments: list[InstrumentDetail]


class HistoryResponse(BaseModel):
    symbol: str
    count: int = Field(ge=0)
    observations: list[HistoryPoint]


class HistoryPoint(BaseModel):
    timestamp: datetime
    value: float
    unit: str
    source: str
    received_at: datetime
    # Persisted observation provenance, not today's provider/contract state.
    contract_code: str | None = None
    feed_status: str | None = None
    exchange_time_known: bool | None = None


class ErrorResponse(BaseModel):
    detail: str


class ProviderRouteState(BaseModel):
    """Routing and live quality for one canonical instrument."""

    symbol: str
    provider: str
    feed_status: str
    source: str | None = None


class ProvidersResponse(BaseModel):
    generated_at: datetime
    provider_mode: str
    provider_status: str
    routes: list[ProviderRouteState]
