"""REST routes for the Phase 5 news / geopolitical event pipeline.

Thin handlers: resolve the news service, validate query parameters, and map
results to response schemas. All normalization/deduplication logic lives in
:mod:`core.services.news` and :mod:`core.ingest`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from core.domain.news import (
    KNOWN_EVENT_TYPES,
    MarketEvent,
    SourceHealth,
    SourceOccurrence,
    SourceType,
    UTCDatetime,
)
from core.services.news import IngestionStats, NewsService

router = APIRouter(prefix="/api/v1", tags=["news"])


def get_news_service(request: Request) -> NewsService:
    service: NewsService | None = getattr(request.app.state, "news", None)
    if service is None or not service.enabled:
        raise HTTPException(status_code=503, detail="news service unavailable")
    return service


ServiceDep = Annotated[NewsService, Depends(get_news_service)]


class EventsResponse(BaseModel):
    generated_at: UTCDatetime
    count: int = Field(ge=0)
    events: list[MarketEvent]


class EventDetailResponse(BaseModel):
    event: MarketEvent
    occurrences: list[SourceOccurrence]
    occurrence_count: int = Field(ge=1)


class SourcesResponse(BaseModel):
    generated_at: UTCDatetime
    enabled: bool
    source_types: list[SourceType]
    event_types: list[str]
    sources: list[SourceHealth]


class SourceStatusResponse(BaseModel):
    generated_at: UTCDatetime
    enabled: bool
    sources: list[SourceHealth]


class IngestionStatsResponse(BaseModel):
    generated_at: UTCDatetime
    stats: dict[str, IngestionStats]


def _require_aware(value: datetime | None) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise HTTPException(
            status_code=422,
            detail="start/end must be timezone-aware (e.g. 2026-01-02T15:30:00Z)",
        )
    return value


@router.get("/events", response_model=EventsResponse)
async def list_events(
    service: ServiceDep,
    source: Annotated[str | None, Query(description="Source adapter name")] = None,
    source_type: Annotated[SourceType | None, Query(description="Source family")] = None,
    event_type: Annotated[str | None, Query(description="Deterministic event type")] = None,
    instrument: Annotated[str | None, Query(description="Canonical instrument symbol")] = None,
    country: Annotated[str | None, Query(description="Country id, e.g. US")] = None,
    start: Annotated[datetime | None, Query(description="Inclusive start (ISO 8601)")] = None,
    end: Annotated[datetime | None, Query(description="Half-open end (ISO 8601)")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> EventsResponse:
    """Canonical events, newest first, with optional deterministic source filters."""
    _require_aware(start)
    _require_aware(end)
    if start is not None and end is not None and start >= end:
        raise HTTPException(
            status_code=422, detail="start must be before end; ranges are half-open"
        )
    events = service.events(
        source=source,
        source_type=source_type,
        event_type=event_type,
        instrument=instrument,
        country=country,
        start=start,
        end=end,
        limit=limit,
    )
    return EventsResponse(generated_at=service.clock(), count=len(events), events=events)


@router.get("/events/latest", response_model=EventsResponse)
async def latest_events(
    service: ServiceDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> EventsResponse:
    """Most recent canonical events."""
    events = service.latest_events(limit=limit)
    return EventsResponse(generated_at=service.clock(), count=len(events), events=events)


@router.get("/events/{event_id}", response_model=EventDetailResponse)
async def event_detail(event_id: str, service: ServiceDep) -> EventDetailResponse:
    """One canonical event with the source occurrences that reported it."""
    event = service.event(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail=f"unknown event: {event_id}")
    occurrences = service.occurrences(event_id)
    return EventDetailResponse(
        event=event,
        occurrences=occurrences,
        occurrence_count=max(event.occurrence_count, len(occurrences)),
    )


@router.get("/sources", response_model=SourcesResponse)
async def sources(service: ServiceDep) -> SourcesResponse:
    """Configured news sources and their current health."""
    return SourcesResponse(
        generated_at=service.clock(),
        enabled=service.enabled,
        source_types=list(SourceType),
        event_types=list(KNOWN_EVENT_TYPES),
        sources=service.source_health(),
    )


@router.get("/sources/status", response_model=SourceStatusResponse)
async def source_status(service: ServiceDep) -> SourceStatusResponse:
    """Compact source-health view."""
    return SourceStatusResponse(
        generated_at=service.clock(),
        enabled=service.enabled,
        sources=service.source_health(),
    )


@router.get("/sources/stats", response_model=IngestionStatsResponse)
async def source_stats(service: ServiceDep) -> IngestionStatsResponse:
    """Per-source counters from the most recent ingestion run."""
    return IngestionStatsResponse(
        generated_at=service.clock(),
        stats=service.ingestion_stats(),
    )


__all__ = ["router"]
