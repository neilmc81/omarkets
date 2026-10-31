"""Chart focus and watchlist API (no AI, no desk).

* ``GET /api/v1/chart/instruments`` — every registry instrument, as the Chart
  page draws it (label, precision, whether it has levels and release pins).
* ``GET|PUT /api/v1/chart/focus`` — the instrument the Chart page shows
  (default NQ), saved across restarts.
* ``GET|PUT /api/v1/watchlist`` — the one watchlist (default: the eight desk
  strip symbols).

These routes read and write only the workspace store. They never call the
desk, so a focus change cannot write a note or spend on AI; Today, the shell
bar and the desk stay on NQ.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from core.api.chart import LEVEL_SYMBOLS, RELEASE_PIN_SYMBOLS
from core.desk.facts import LABELS, STRIP_SYMBOLS
from core.domain.instruments import PRIMARY_SYMBOL, all_symbols, get_instrument
from core.domain.workspace import (
    ChartFocus,
    ChartInstrument,
    ChartInstruments,
    FocusUpdate,
    SavedWatchlist,
    Watchlist,
)
from core.storage.workspace import WorkspaceStore

router = APIRouter(prefix="/api/v1", tags=["chart"])

DEFAULT_FOCUS = PRIMARY_SYMBOL
DEFAULT_WATCHLIST = Watchlist(name="Desk strip", symbols=list(STRIP_SYMBOLS))

_UNAVAILABLE = {503: {"description": "Workspace store unavailable"}}


def _store(request: Request) -> WorkspaceStore:
    store: WorkspaceStore | None = getattr(request.app.state, "workspace", None)
    if store is None:
        raise HTTPException(status_code=503, detail="workspace store unavailable")
    return store


async def _run[T](fn: Callable[..., T], *args: Any) -> T:
    """Run a store call off the event loop; a broken store file is a 503, not a 500."""
    try:
        return await asyncio.to_thread(fn, *args)
    except sqlite3.Error as exc:
        raise HTTPException(status_code=503, detail="workspace store unavailable") from exc


def chart_instrument(symbol: str) -> ChartInstrument:
    inst = get_instrument(symbol)
    if inst is None:  # pragma: no cover - callers pass registry symbols
        raise KeyError(symbol)
    return ChartInstrument(
        symbol=inst.symbol, label=LABELS.get(inst.symbol, inst.display_name), unit=inst.unit,
        price_precision=inst.price_precision, levels=inst.symbol in LEVEL_SYMBOLS,
        release_pins=inst.symbol in RELEASE_PIN_SYMBOLS,
    )


@router.get("/chart/instruments", response_model=ChartInstruments)
async def chart_instruments() -> ChartInstruments:
    """Every instrument the Chart page can show, in registry order."""
    return ChartInstruments(instruments=[chart_instrument(s) for s in all_symbols()])


def _focus(store: WorkspaceStore) -> ChartFocus:
    saved = store.focus()
    symbol, at = saved if saved is not None else (DEFAULT_FOCUS, None)
    return ChartFocus(symbol=symbol, updated_at=at, instrument=chart_instrument(symbol))


@router.get("/chart/focus", response_model=ChartFocus, responses=_UNAVAILABLE)
async def get_focus(request: Request) -> ChartFocus:
    """The instrument the Chart page shows (NQ until one is chosen)."""
    return await _run(_focus, _store(request))


@router.put("/chart/focus", response_model=ChartFocus,
            responses={**_UNAVAILABLE, 422: {"description": "Unknown instrument"}})
async def put_focus(body: FocusUpdate, request: Request) -> ChartFocus:
    """Save the Chart page's instrument. Writes no note and calls no AI."""
    store = _store(request)
    at = await _run(store.set_focus, body)
    return ChartFocus(symbol=body.symbol, updated_at=at, instrument=chart_instrument(body.symbol))


def _watchlist(store: WorkspaceStore) -> SavedWatchlist:
    saved = store.watchlist()
    if saved is None:
        return SavedWatchlist(**DEFAULT_WATCHLIST.model_dump())
    watchlist, at = saved
    return SavedWatchlist(**watchlist.model_dump(), updated_at=at)


@router.get("/watchlist", response_model=SavedWatchlist, responses=_UNAVAILABLE)
async def get_watchlist(request: Request) -> SavedWatchlist:
    """The watchlist (the desk strip's eight symbols until one is saved)."""
    return await _run(_watchlist, _store(request))


@router.put("/watchlist", response_model=SavedWatchlist,
            responses={**_UNAVAILABLE, 422: {"description": "Unknown or repeated symbol"}})
async def put_watchlist(body: Watchlist, request: Request) -> SavedWatchlist:
    """Replace the watchlist's name and ordered symbols."""
    store = _store(request)
    at = await _run(store.set_watchlist, body)
    return SavedWatchlist(**body.model_dump(), updated_at=at)


__all__ = ["DEFAULT_FOCUS", "DEFAULT_WATCHLIST", "chart_instrument", "router"]
