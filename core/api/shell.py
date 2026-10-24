"""Shell endpoints for the native Omarchy integration.

Two small, versioned, read-only endpoints that present a compact projection of
already-computed deterministic state:

* ``GET /api/v1/shell/status``  — the bar widget payload.
* ``GET /api/v1/shell/context`` — the popup payload.

They exist so the Quickshell layer never assembles several internal models and
never depends on database schemas. The handlers are read-only projections; the
mapping lives in :mod:`core.api.shell_view` and is pure. The latest desk note
reaches the popup through ``/api/v1/desk`` (see ``omarchy/omarkets/update.py``).

Every field degrades safely: an unavailable service yields safe defaults rather
than a 500, because a missing section must never blank the whole shell.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

from fastapi import APIRouter, Request

from core.analysis.cross_market import build_cross_market_state
from core.api.shell_view import (
    aggregate_health,
    build_context,
    build_diagnostics,
    build_status,
)
from core.domain.shell import ShellContext, ShellStatus

router = APIRouter(prefix="/api/v1", tags=["shell"])
logger = logging.getLogger(__name__)


def _log_slow_projection(name: str, started: float) -> None:
    duration_ms = (time.perf_counter() - started) * 1000
    if duration_ms >= 250:
        logger.warning("shell_projection endpoint=%s duration_ms=%.2f", name, duration_ms)


def _now() -> datetime:
    return datetime.now(UTC)


def _market_state(request: Request, now: datetime):
    market = getattr(request.app.state, "service", None)
    if market is None:
        return None
    try:
        return market.current_state(now=now)
    except Exception:  # pragma: no cover - defensive isolation
        return None


def _calendar(request: Request) -> tuple[list, bool]:
    economics = getattr(request.app.state, "economics", None)
    if economics is None or getattr(economics, "mode", None) is None:
        return [], False
    try:
        if economics.mode.value == "disabled":
            return [], False
        return list(economics.calendar_events()), True
    except Exception:  # pragma: no cover
        return [], False


@router.get("/shell/status", response_model=ShellStatus)
async def shell_status(request: Request) -> ShellStatus:
    """Compact bar-widget status. Read-only; never triggers a note."""
    started = time.perf_counter()
    now = _now()
    market_state = _market_state(request, now)
    market_healthy = market_state is not None and market_state.provider_status.value == "ok"
    market_detail = market_state.provider_status.value if market_state else "unavailable"
    cross = build_cross_market_state(market_state, now=now) if market_state else None
    events, macro_ok = _calendar(request)

    desk = getattr(request.app.state, "desk", None)
    writer_ready = None if desk is None else bool(desk.writer.ready)
    writer_detail = None if desk is None else desk.writer.detail
    data_health, ai_health = aggregate_health(
        market_healthy=market_healthy, writer_ready=writer_ready
    )

    news = getattr(request.app.state, "news", None)
    news_healthy = 0
    news_total = 0
    if news is not None and news.enabled:
        try:
            sources = news.source_health()
            news_total = len(sources)
            news_healthy = sum(1 for s in sources if s.status.value == "healthy")
        except Exception:  # pragma: no cover
            pass

    result = build_status(
        now=now,
        market=market_state,
        cross=cross,
        events=events,
        data_health=data_health,
        ai_health=ai_health,
        providers=build_diagnostics(
            data_health=data_health,
            ai_health=ai_health,
            market_detail=market_detail,
            writer_detail=writer_detail,
            news_healthy=news_healthy,
            news_total=news_total,
            macro_ok=macro_ok,
        ),
    )
    _log_slow_projection("status", started)
    return result


@router.get("/shell/context", response_model=ShellContext)
async def shell_context(request: Request) -> ShellContext:
    """Compact popup context. Read-only; never triggers a note."""
    started = time.perf_counter()
    now = _now()
    market_state = _market_state(request, now)
    cross = build_cross_market_state(market_state, now=now) if market_state else None
    events, _ = _calendar(request)
    result = build_context(now=now, cross=cross, market=market_state, events=events, diagnostics=[])
    _log_slow_projection("context", started)
    return result


__all__ = ["router"]
