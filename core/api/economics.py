from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from core.desk.links import ReleaseNotes
from core.domain.economics import (
    EconomicEvent,
    EconomicsMode,
    EventStatus,
    Importance,
    MacroCategory,
    MacroDefinition,
    MacroObservation,
    MacroProviderHealth,
    MacroState,
    UTCDatetime,
)
from core.services.economics import EconomicService, calendar_window

router = APIRouter(prefix="/api/v1", tags=["economics"])


class MacroSeriesResponse(BaseModel):
    generated_at: UTCDatetime
    series: list[MacroDefinition]


class MacroHistoryResponse(BaseModel):
    generated_at: UTCDatetime
    definition: MacroDefinition
    observations: list[MacroObservation]
    history_scope: Literal["observed_since_install"] = "observed_since_install"


class CalendarDisplayGroup(BaseModel):
    """Presentation identity only; source event records remain independent."""

    primary_event_id: str
    event_ids: list[str]
    event_code: str
    scheduled_at: UTCDatetime


class CalendarHistoryCoverage(BaseModel):
    scope: Literal["observed_source_records"] = "observed_source_records"
    earliest_scheduled_at: UTCDatetime | None = None
    stored_schedule_count: int = Field(default=0, ge=0)
    consensus_history: Literal["partial_observed_only"] = "partial_observed_only"
    official_schedule_lookback_days: Literal[90] = 90
    official_schedule_event_codes: list[str] = Field(
        default_factory=lambda: ["CPI", "PAYROLLS", "JOLTS"])
    original_vintages: Literal["not_reconstructed"] = "not_reconstructed"


class CalendarEventView(EconomicEvent):
    """A calendar event as served, with the desk note written for its release."""

    note_id: str | None = None


class CalendarResponse(BaseModel):
    generated_at: UTCDatetime
    mode: EconomicsMode
    events: list[CalendarEventView]
    display_groups: list[CalendarDisplayGroup] = Field(default_factory=list)
    providers: list[MacroProviderHealth]
    stale: bool = Field(description="Calendar schedule coverage lacks a healthy source; "
                        "individual actual-source failures remain in provider health.")
    coverage: list[str] = Field(default_factory=list)
    history_coverage: CalendarHistoryCoverage = Field(default_factory=CalendarHistoryCoverage)


def calendar_display_groups(events: list[EconomicEvent]) -> list[CalendarDisplayGroup]:
    grouped: dict[tuple[str, datetime], list[EconomicEvent]] = {}
    for event in events:
        if event.scheduled_at is None:
            continue
        # Only release families with overlapping FRED/consensus slots.
        # Other same-time records may be distinct releases.
        key = (
            event.event_code if event.event_code in {
                "CPI", "PAYROLLS", "JOLTS", "INITIAL_CLAIMS", "ISM_MFG", "ISM_MFG_PRICES"
            } else event.id,
            event.scheduled_at,
        )
        if event.event_code in {"CPI", "CORE_CPI"}:
            metric = EconomicService._calendar_series(event)
            if metric is None and event.unit in {"percent m/m", "percent y/y"}:
                metric = event.event_code + ("_MOM" if event.unit == "percent m/m" else "_YOY")
            # Unknown metric titles stay separate; annual figures cannot disappear
            # behind the monthly headline card. Same-metric source rows still group.
            key = (f"{event.event_code}:{metric}" if metric else event.id, event.scheduled_at)
        elif event.event_code in {"RETAIL_SALES", "CORE_RETAIL_SALES", "PPI", "CORE_PPI"}:
            metric = EconomicService._calendar_series(event)
            key = (
                (f"monthly:{metric}:{event.country}:{event.mode}:{event.unit}"
                 if metric else event.id),
                event.scheduled_at,
            )
        grouped.setdefault(key, []).append(event)
    for event in events:
        # An issuer notice is not a schedule. Attach it only to an existing
        # independently scheduled ISM release with the exact publication instant
        # and reporting period; retain both records and their source details.
        if (
            event.scheduled_at is not None
            or event.event_code not in {"ISM_MFG", "ISM_MFG_PRICES"}
            or event.provider != "ism_newswire"
            or event.actual_basis != "source_release"
            or event.actual is None
            or event.released_at is None
            or event.period is None
        ):
            continue
        members = grouped.get((event.event_code, event.released_at))
        if members and all(
            member.period == event.period
            and member.country == event.country
            and member.unit == event.unit
            # The consensus adapter labels event cadence generically as
            # 'release'; ISM's issuer describes the same report as monthly.
            and (
                member.frequency == event.frequency
                or (member.provider == "consensus"
                    and member.frequency == "release"
                    and event.frequency == "monthly")
            )
            and (member.actual is None or member.actual == event.actual)
            for member in members
        ):
            members.append(event)
    groups = []
    for (_, scheduled_at), members in grouped.items():
        # Prefer a stored actual/forecast for the main card; otherwise use the
        # source-dated FRED slot. Never combine numeric fields across records.
        def priority(item: EconomicEvent) -> tuple[bool, bool, bool, bool, str]:
            return (
                item.actual is not None,
                item.consensus is not None,
                item.previous is not None,
                item.provider == "fred_release_calendar",
                item.id,
            )

        primary = max(members, key=priority)
        groups.append(
            CalendarDisplayGroup(
                primary_event_id=primary.id,
                event_ids=sorted(item.id for item in members),
                event_code=primary.event_code,
                scheduled_at=scheduled_at,
            )
        )
    return groups


class EventHistoryResponse(BaseModel):
    generated_at: UTCDatetime
    event: EconomicEvent
    versions: list[EconomicEvent]
    history_scope: Literal["observed_since_install"] = "observed_since_install"


class ProvidersResponse(BaseModel):
    generated_at: UTCDatetime
    mode: EconomicsMode
    providers: list[MacroProviderHealth]


class CalendarFilters(BaseModel):
    start: UTCDatetime | None = None
    end: UTCDatetime | None = None
    importance: Importance | None = None
    category: MacroCategory | None = None
    status: EventStatus | None = None
    limit: int = Field(default=500, ge=1, le=500)
    days: int = Field(default=30, ge=1, le=365)


def get_economic_service(request: Request) -> EconomicService:
    return request.app.state.economics


ServiceDep = Annotated[EconomicService, Depends(get_economic_service)]
FilterDep = Annotated[CalendarFilters, Query()]


@router.get("/macro/state", response_model=MacroState)
async def macro_state(service: ServiceDep) -> MacroState:
    return service.current_state()


@router.get("/macro/series", response_model=MacroSeriesResponse)
async def macro_series(service: ServiceDep) -> MacroSeriesResponse:
    return MacroSeriesResponse(
        generated_at=service.clock(), series=list(service.registry.definitions)
    )


@router.get("/macro/series/{series_id}", response_model=MacroHistoryResponse)
async def macro_history(
    series_id: str,
    service: ServiceDep,
    all_versions: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 500,
) -> MacroHistoryResponse:
    definition = service.registry.get(series_id)
    if definition is None:
        raise HTTPException(404, "Unknown macro series")
    try:
        observations = (
            service.repository.observations(
                series_id,
                all_versions=all_versions,
                limit=limit,
            )
            if service.repository
            else []
        )
    except Exception as exc:
        raise HTTPException(503, "Economic storage unavailable") from exc
    return MacroHistoryResponse(
        generated_at=service.clock(), definition=definition, observations=observations
    )


def with_note_ids(events: list[EconomicEvent], notes: ReleaseNotes | None,
                  ) -> list[CalendarEventView]:
    """Attach the desk note for each event's release: same UTC minute, same event code."""
    def note_id(event: EconomicEvent) -> str | None:
        if notes is None or event.country != "US":
            return None
        return notes.for_event(event.scheduled_at, event.event_code)

    return [CalendarEventView.model_validate({**event.model_dump(), "note_id": note_id(event)})
            for event in events]


def _release_notes(request: Request) -> ReleaseNotes | None:
    desk = getattr(request.app.state, "desk", None)
    return desk.release_notes() if desk is not None else None


def calendar_coverage(service: EconomicService) -> tuple[list[str], bool]:
    """The calendar's source caveats, and whether no schedule source is healthy."""
    calendar_providers = service.calendar_provider_health()
    source_dated = any(
        item.name == "fred_release_calendar" and item.status == "healthy"
        for item in calendar_providers
    )
    coverage = [
        (
            "CPI, Employment Situation and JOLTS slots use FRED's source-published schedule; "
            "this is not evidence of publication or FRED data availability"
            if source_dated
            else "CPI, Employment Situation and JOLTS schedule coverage may be partial"
        ),
        "Claims, Census and ISM release schedules may be incomplete",
        "Official FRED schedule backfill is limited to 90 days for CPI, Employment "
        "Situation and JOLTS; date-range queries outside stored coverage are partial "
        "and do not fetch arbitrary archive dates.",
        "Consensus history is partial: the public consensus feed covers its current week; "
        "older original forecasts and vintages are retained only when observed by Omarkets, "
        "and are not reconstructed from current values.",
        "Official issuer releases can restore dated actual evidence where supported; "
        "they do not restore missing historical consensus or complete calendar coverage.",
        "Observed releases are derived from stored macro observations; schedule and "
        "consensus are not fabricated",
        "Scheduled BLS/FRED actuals, when shown, are current-vintage series values received "
        "by Omarkets; the original release value and publication instant are unverified",
        "Notices without scheduled times are listed by publication time; publication "
        "time is not a scheduled release time",
        "Forecast/consensus is shown only where its source supplies it; observed "
        "actual, previous and revised previous are shown where available",
    ]
    if service.mode == EconomicsMode.DISABLED:
        coverage.append("Economic data is disabled")
    stale = service.mode == EconomicsMode.DISABLED or not any(
        item.status == "healthy" and item.name in (
            {"consensus", "bea_calendar", "bls_calendar", "fred_release_calendar",
             "fed_calendar", "demo"} | service.calendar_schedule_providers()
        ) for item in calendar_providers)
    return coverage, stale


def _calendar(
    service: EconomicService, filters: CalendarFilters, scope: str | None = None,
    notes: ReleaseNotes | None = None,
) -> CalendarResponse:
    start, end = filters.start, filters.end
    now = service.clock()
    if scope is not None:
        default_start, default_end = calendar_window(now, scope, filters.days)
        start = start if start is not None else default_start
        end = end if end is not None else default_end
    if start is not None and end is not None and start >= end:
        raise HTTPException(422, "start must be before end; ranges are half-open")
    providers = service.provider_health()
    coverage, stale = calendar_coverage(service)
    events = service.calendar_events(
        start=start,
        end=end,
        importance=filters.importance,
        category=filters.category,
        status=filters.status,
        limit=filters.limit,
    )
    scheduled = service.calendar_schedule_history()
    return CalendarResponse(
        history_coverage=CalendarHistoryCoverage(
            earliest_scheduled_at=min(scheduled, default=None),
            stored_schedule_count=len(scheduled)),
        generated_at=now,
        mode=service.mode,
        events=with_note_ids(events, notes),
        display_groups=calendar_display_groups(events),
        providers=providers,
        stale=stale,
        coverage=coverage,
    )


@router.get("/calendar", response_model=CalendarResponse)
async def calendar(
    request: Request, service: ServiceDep, filters: FilterDep
) -> CalendarResponse:
    return _calendar(service, filters, notes=_release_notes(request))


@router.get("/calendar/today", response_model=CalendarResponse)
async def calendar_today(
    request: Request, service: ServiceDep, filters: FilterDep
) -> CalendarResponse:
    return _calendar(service, filters, "today", notes=_release_notes(request))


@router.get("/calendar/week", response_model=CalendarResponse)
async def calendar_week(
    request: Request, service: ServiceDep, filters: FilterDep
) -> CalendarResponse:
    return _calendar(service, filters, "week", notes=_release_notes(request))


@router.get("/calendar/upcoming", response_model=CalendarResponse)
async def calendar_upcoming(
    request: Request, service: ServiceDep, filters: FilterDep
) -> CalendarResponse:
    return _calendar(service, filters, "upcoming", notes=_release_notes(request))


@router.get("/calendar/{event_id}", response_model=EventHistoryResponse)
async def calendar_event(
    event_id: str, service: ServiceDep, limit: Annotated[int, Query(ge=1, le=500)] = 500
) -> EventHistoryResponse:
    occurrence = service.calendar_occurrence(event_id)
    aliases = occurrence.occurrence_aliases or [event_id] if occurrence else [event_id]
    try:
        versions = []
        for alias in aliases:
            saved = (service.repository.events(alias, all_versions=True, limit=limit)
                     if service.repository else [])
            versions.extend(saved or service.release_event_history(alias))
        versions = sorted(versions, key=lambda item: (
            item.received_at, item.id, item.revision
        ))[-limit:]
    except Exception as exc:
        raise HTTPException(503, "Economic storage unavailable") from exc
    if not versions:
        derived = service.release_event_history(event_id)[-limit:]
        if not derived:
            raise HTTPException(404, "Unknown economic event")
        versions = derived
    return EventHistoryResponse(
        generated_at=service.clock(),
        event=occurrence or service.calendar_event_state(versions[-1]),
        versions=versions,
    )


@router.get("/providers", response_model=ProvidersResponse)
async def providers(service: ServiceDep) -> ProvidersResponse:
    return ProvidersResponse(
        generated_at=service.clock(), mode=service.mode, providers=service.provider_health()
    )
