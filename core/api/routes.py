"""REST routes.

Route handlers stay thin: they resolve dependencies, call the state service or
repository, and map results to response schemas. Market logic lives in
:mod:`core.services.market_state`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from core.api.schemas import (
    HealthResponse,
    HistoryPoint,
    HistoryResponse,
    InstrumentDetail,
    InstrumentsResponse,
    NewsHealth,
    ProvidersResponse,
)
from core.domain.contracts import DEFAULT_FUTURES_ROOTS, contract_for_symbol
from core.domain.cross_market import CrossMarketState
from core.domain.instruments import get_instrument, normalize_symbol
from core.domain.models import MarketState
from core.providers.registry import ProviderRegistry
from core.services.market_state import MarketStateService
from core.storage.base import StorageError

router = APIRouter()


def get_service(request: Request) -> MarketStateService:
    """Resolve the process-wide state service from application state."""
    service: MarketStateService | None = getattr(request.app.state, "service", None)
    if service is None:  # pragma: no cover - misconfiguration
        raise HTTPException(status_code=503, detail="market state service unavailable")
    return service


ServiceDep = Annotated[MarketStateService, Depends(get_service)]


@router.get("/api/v1/cross-market", response_model=CrossMarketState, tags=["market"])
async def cross_market(service: ServiceDep) -> CrossMarketState:
    """Measured cross-asset quote context, independent of technical analysis."""
    from core.analysis.cross_market import build_cross_market_state

    return build_cross_market_state(service.current_state())


def _require_aware(value: datetime | None) -> datetime | None:
    """Reject naive datetimes at the boundary with a 422, not a 500."""
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise HTTPException(
            status_code=422,
            detail="start/end must be timezone-aware (e.g. 2026-01-02T15:30:00Z)",
        )
    return value


@router.get("/health", response_model=HealthResponse, tags=["system"])
async def health(service: ServiceDep, request: Request) -> HealthResponse:
    """Application, storage, and provider health."""
    from core import __version__

    detail = service.health_detail()
    status = "ok" if detail.storage_healthy and detail.stream_running else "degraded"
    news_service = getattr(request.app.state, "news", None)
    news_health = None
    if news_service is not None:
        from core.domain.news import SourceStatus

        sources = news_service.source_health()
        counts = {status_value: 0 for status_value in SourceStatus}
        for source in sources:
            counts[source.status] += 1
        news_health = NewsHealth(
            enabled=news_service.enabled,
            source_count=len(sources),
            healthy_sources=counts[SourceStatus.HEALTHY],
            degraded_sources=counts[SourceStatus.DEGRADED],
            offline_sources=counts[SourceStatus.OFFLINE],
            disabled_sources=counts[SourceStatus.DISABLED],
            event_count=news_service.event_count,
            subscriber_count=news_service.subscriber_count,
        )
    return HealthResponse(
        status=status,
        app="omarkets",
        version=__version__,
        time=service.current_state().generated_at,
        database_ok=detail.storage_healthy,
        provider=detail.provider,
        provider_status=detail.provider_status.value,
        provider_error=detail.provider_error,
        stream_running=detail.stream_running,
        cached_instruments=detail.cached_instruments,
        updates_published=detail.updates_published,
        last_update_at=detail.last_update_at,
        subscriber_count=service.subscriber_count,
        economic_providers=request.app.state.economics.provider_health(),
        news=news_health,
    )


@router.get("/api/v1/state", response_model=MarketState, tags=["market"])
async def state(service: ServiceDep) -> MarketState:
    """Current normalized market state for the whole universe."""
    return service.current_state()


@router.get("/api/v1/instruments", response_model=InstrumentsResponse, tags=["market"])
async def instruments(service: ServiceDep) -> InstrumentsResponse:
    """Configured instruments with their current state."""
    current = service.current_state()
    details = []
    for state in current.instruments.values():
        definition = get_instrument(state.symbol)
        if definition is None:  # pragma: no cover - states come from the registry
            raise HTTPException(
                status_code=500, detail=f"instrument definition missing: {state.symbol}"
            )
        details.append(
            InstrumentDetail(
                symbol=state.symbol,
                display_name=state.display_name,
                asset_class=state.asset_class.value,
                quote_type=state.quote_type.value,
                timezone=definition.timezone,
                unit=state.unit,
                stale_after_seconds=state.stale_after_seconds,
                metadata=definition.metadata,
                state=state,
            )
        )
    return InstrumentsResponse(
        generated_at=current.generated_at,
        count=len(details),
        instruments=details,
    )


@router.get(
    "/api/v1/instruments/{symbol}",
    response_model=InstrumentDetail,
    tags=["market"],
    responses={404: {"description": "Unknown instrument symbol"}},
)
async def instrument(symbol: str, service: ServiceDep) -> InstrumentDetail:
    """Current state for a single instrument."""
    canonical = normalize_symbol(symbol)
    definition = get_instrument(canonical)
    if definition is None:
        raise HTTPException(status_code=404, detail=f"unknown instrument: {symbol}")

    current = service.current_state()
    state = current.instruments[canonical]
    return InstrumentDetail(
        symbol=state.symbol,
        display_name=state.display_name,
        asset_class=state.asset_class.value,
        quote_type=state.quote_type.value,
        timezone=definition.timezone,
        unit=state.unit,
        stale_after_seconds=state.stale_after_seconds,
        metadata=definition.metadata,
        state=state,
    )


@router.get(
    "/api/v1/history/{symbol}",
    response_model=HistoryResponse,
    tags=["market"],
    responses={
        404: {"description": "Unknown instrument symbol"},
        422: {"description": "Invalid time range"},
        503: {"description": "Storage unavailable"},
    },
)
async def history(
    symbol: str,
    service: ServiceDep,
    start: Annotated[datetime | None, Query(description="Inclusive start (ISO 8601)")] = None,
    end: Annotated[datetime | None, Query(description="Inclusive end (ISO 8601)")] = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
) -> HistoryResponse:
    """Stored observations for one instrument, oldest first."""
    canonical = normalize_symbol(symbol)
    if get_instrument(canonical) is None:
        raise HTTPException(status_code=404, detail=f"unknown instrument: {symbol}")

    _require_aware(start)
    _require_aware(end)
    if start is not None and end is not None and start > end:
        raise HTTPException(status_code=422, detail="start must not be after end")

    try:
        observations = service.history(canonical, start=start, end=end, limit=limit)
    except StorageError as exc:
        raise HTTPException(status_code=503, detail="storage unavailable") from exc

    return HistoryResponse(
        symbol=canonical,
        count=len(observations),
        observations=[
            HistoryPoint(
                timestamp=obs.timestamp,
                value=obs.value,
                unit=obs.unit,
                source=obs.source,
                received_at=obs.received_at,
                contract_code=(
                    obs.metadata.get("contract_code")
                    if isinstance(obs.metadata.get("contract_code"), str)
                    else None
                ),
                feed_status=(
                    obs.metadata.get("feed_status")
                    if isinstance(obs.metadata.get("feed_status"), str)
                    else None
                ),
                exchange_time_known=(
                    obs.metadata.get("exchange_time_known")
                    if isinstance(obs.metadata.get("exchange_time_known"), bool)
                    else None
                ),
            )
            for obs in observations
        ],
    )


@router.get("/api/v1/market/providers", response_model=ProvidersResponse, tags=["market"])
async def providers(request: Request, service: ServiceDep) -> ProvidersResponse:
    """Market-data provider routing and health."""
    from core.main import build_registry

    registry: ProviderRegistry = (
        getattr(request.app.state, "provider_registry", None) or build_registry()
    )
    states = []
    current = service.current_state()
    for route in registry.routes():
        state = current.instruments.get(route.symbol)
        states.append(
            {
                "symbol": route.symbol,
                "provider": route.provider,
                "feed_status": state.feed_status.value if state else "unavailable",
                "source": state.source if state else None,
            }
        )
    return ProvidersResponse(
        generated_at=current.generated_at,
        provider_mode=current.provider,
        provider_status=current.provider_status.value,
        routes=states,
    )


@router.get("/api/v1/contracts", tags=["market"])
async def contracts(service: ServiceDep) -> dict:
    """Active futures contracts for canonical futures roots."""
    now = service.current_state().generated_at
    result = []
    for symbol in DEFAULT_FUTURES_ROOTS:
        contract = contract_for_symbol(symbol, now.date())
        if contract is None:  # pragma: no cover - static roots
            continue
        state = service.current_state().instruments.get(symbol)
        result.append(
            {
                "symbol": symbol,
                "contract_code": contract.code,
                "contract_month": contract.contract_month,
                "expiry": contract.expiry.isoformat(),
                "roll_days_before_expiry": contract.roll_days_before_expiry,
                "provider_symbol": state.provider_symbol if state else contract.provider_symbol(),
            }
        )
    return {"generated_at": now, "contracts": result}


@router.get("/api/v1/markets/status", tags=["market"])
async def markets_status(service: ServiceDep) -> dict:
    """One row per instrument with feed quality and source details."""
    current = service.current_state()
    rows = []
    for symbol, state in current.instruments.items():
        rows.append(
            {
                "symbol": symbol,
                "value": state.value,
                "change": state.change,
                "change_pct": state.change_pct,
                "status": state.status.value,
                "feed_status": state.feed_status.value,
                "source": state.source,
                "provider_symbol": state.provider_symbol,
                "contract_code": state.contract_code,
                "exchange_timestamp": state.timestamp,
                "received_at": state.received_at,
                "age_seconds": state.age_seconds,
                "latency_seconds": state.latency_seconds,
                "stale_after_seconds": state.stale_after_seconds,
            }
        )
    return {"generated_at": current.generated_at, "markets": rows}
