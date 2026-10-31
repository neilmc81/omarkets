"""FastAPI application factory and startup/shutdown lifecycle.

The app is built by :func:`create_app` rather than created at import time, so
tests can construct isolated instances with their own storage and provider.
Dependencies are attached to ``app.state``; there is no module-level singleton.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from core import __version__
from core.api import (
    chart,
    desk,
    economics,
    explainers,
    news,
    routes,
    shell,
    theme,
    websocket,
    workspace,
)
from core.api.availability import RequestTimingMiddleware, monitor_event_loop
from core.config.settings import Settings
from core.providers.base import MarketDataProvider
from core.services.economics import EconomicService
from core.services.market_state import MarketStateService, StalePolicy
from core.services.news import NewsService
from core.storage.base import ObservationRepository

logger = logging.getLogger(__name__)


def create_app(
    *,
    service: MarketStateService,
    economic_service: EconomicService | None = None,
    news_service: NewsService | None = None,
    desk_service=None,
    theme_source=None,
    ai_budget=None,
    workspace_store=None,
    frontend_dist: object | None = None,
    version: str = __version__,
) -> FastAPI:
    """Build the FastAPI app around an already-constructed state service.

    Optional services report unavailable when omitted; none are constructed
    implicitly.
    """

    economic_service = economic_service or EconomicService()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info("Omarkets API starting")
        try:
            if ai_budget is not None:
                await ai_budget.start()
            await service.start()
            await economic_service.start()
            if news_service is not None:
                await news_service.start()
            if desk_service is not None:
                await desk_service.start()
            logger.info("Omarkets API ready")
            async with monitor_event_loop():
                yield
        finally:
            try:
                if desk_service is not None:
                    # The desk only reads other services; stop it first.
                    await desk_service.stop()
            finally:
                try:
                    if news_service is not None:
                        await news_service.stop()
                finally:
                    try:
                        await economic_service.stop()
                    finally:
                        await service.stop()
            logger.info("Omarkets API stopped")
            if ai_budget is not None:
                await ai_budget.stop()

    app = FastAPI(
        title="Omarkets",
        version=version,
        summary="Local deterministic market-analysis core",
        lifespan=lifespan,
    )
    app.add_middleware(RequestTimingMiddleware)
    app.state.service = service
    app.state.ai_budget = ai_budget
    app.state.economics = economic_service
    app.state.news = news_service
    app.state.desk = desk_service
    app.state.workspace = workspace_store
    from core.theme.omarchy import OmarchyTheme

    app.state.theme = theme_source or OmarchyTheme()
    from core.main import build_registry

    app.state.provider_registry = build_registry()
    app.include_router(desk.router)
    app.include_router(chart.router)
    app.include_router(workspace.router)
    app.include_router(explainers.router)
    app.include_router(theme.router)
    app.include_router(economics.router)
    app.include_router(news.router)
    from core.api.ai_budget import router as ai_budget_router

    app.include_router(ai_budget_router)
    app.include_router(shell.router)
    app.include_router(routes.router)
    app.include_router(websocket.router)

    # Serve the built web workstation from the backend when a directory is
    # supplied and present, so the native layer and launcher need one process.
    # Mounted last so API routes, /health and /ws always take precedence.
    # Omitted when absent (development still uses the standalone Vite server).
    from pathlib import Path

    from fastapi.staticfiles import StaticFiles

    class FrontendFiles(StaticFiles):
        async def get_response(self, path, scope):
            response = await super().get_response(path, scope)
            # Revalidate the entry point after deployment. Content-hashed assets
            # may remain cached; old HTML must not pin users to an old bundle.
            if path in {"", ".", "/"} or path.endswith(".html"):
                response.headers["Cache-Control"] = "no-cache"
            return response

    dist = Path(frontend_dist) if frontend_dist is not None else None
    if dist is not None and dist.is_dir() and (dist / "index.html").is_file():
        app.mount("/", FrontendFiles(directory=str(dist), html=True), name="frontend")
        logger.info("serving frontend from %s", dist)
    return app


def build_service(
    *,
    settings: Settings,
    provider: MarketDataProvider,
    repository: ObservationRepository,
) -> MarketStateService:
    """Construct the state service from settings and injected dependencies."""
    overrides = dict(settings.stale_overrides)
    if settings.market_data_mode == "live" and not settings.data_provider:
        # Real feeds are not tick-perfect; use realistic live thresholds unless
        # the operator explicitly overrides them.
        for asset_class, seconds in settings.market_stale_overrides.items():
            overrides.setdefault(asset_class, seconds)
    policy = StalePolicy(
        default_seconds=settings.stale_default_seconds,
        overrides=overrides,
    )
    return MarketStateService(
        provider=provider,
        repository=repository,
        policy=policy,
    )


__all__ = ["build_service", "create_app"]
