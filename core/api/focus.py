"""The global focus: the instrument Today, the web shell and the Omarchy bar lead with.

The focus and the watchlist are the ones the Chart page saves
(:mod:`core.api.workspace`, ``workspace.sqlite3``). This module only *reads*
them:

* :func:`read_focus` — the saved focus and watchlist, or the defaults (NQ and
  the desk strip) when the store is missing, broken or holds nothing valid.
* ``GET /api/v1/focus/strip`` — Today's quote strip: the focus first, then the
  watchlist, as desk quote tiles.

Nothing here calls the desk, so reading or changing the focus can never write a
note or spend on AI. The desk itself stays on its own instruments.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from fastapi import APIRouter, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict

from core.api.shell_view import focus_symbols
from core.api.workspace import DEFAULT_FOCUS, DEFAULT_WATCHLIST, chart_instrument
from core.desk import facts as F
from core.desk.models import Quote
from core.domain.workspace import ChartInstrument, Watchlist

router = APIRouter(prefix="/api/v1", tags=["focus"])
logger = logging.getLogger(__name__)

#: Today's strip holds at most this many tiles (the desk strip has eight).
TODAY_STRIP_SIZE = 8


@dataclass(frozen=True)
class GlobalFocus:
    symbol: str
    #: The saved watchlist; ``None`` while the default (the desk strip) applies.
    saved_watchlist: Watchlist | None

    @property
    def watchlist(self) -> Watchlist:
        return self.saved_watchlist or DEFAULT_WATCHLIST


DEFAULT = GlobalFocus(symbol=DEFAULT_FOCUS, saved_watchlist=None)


def read_focus(state: object) -> GlobalFocus:
    """The saved focus and watchlist; the defaults when either can't be read.

    Never raises: a missing or broken workspace store must not blank Today or
    the shell bar, it just leaves them on NQ.
    """
    store = getattr(state, "workspace", None)
    if store is None:
        return DEFAULT
    try:
        focus = store.focus()
        watchlist = store.watchlist()
    except (sqlite3.Error, OSError, ValueError):
        logger.warning("focus: workspace store unreadable; using NQ and the desk strip")
        return DEFAULT
    return GlobalFocus(symbol=focus[0] if focus else DEFAULT_FOCUS,
                       saved_watchlist=watchlist[0] if watchlist else None)


class FocusStrip(BaseModel):
    """Today's strip: the focus instrument, then the watchlist, as quote tiles."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    generated_at: AwareDatetime | None
    focus: ChartInstrument
    watchlist_name: str
    #: The strip's symbols in display order (focus first, at most eight).
    symbols: list[str]
    #: One tile per symbol the market state knows, in ``symbols`` order.
    quotes: list[Quote]


def focus_strip(state: object, market: object | None,
                focus: GlobalFocus | None = None) -> FocusStrip:
    focus = focus or read_focus(state)
    symbols = focus_symbols(focus.symbol, focus.watchlist.symbols, TODAY_STRIP_SIZE)
    quotes = F.quote_strip(market, symbols) if market is not None else []
    generated_at: datetime | None = getattr(market, "generated_at", None)
    return FocusStrip(generated_at=generated_at, focus=chart_instrument(focus.symbol),
                      watchlist_name=focus.watchlist.name, symbols=symbols, quotes=quotes)


@router.get("/focus/strip", response_model=FocusStrip)
async def get_focus_strip(request: Request) -> FocusStrip:
    """Today's strip for the focus instrument. Read-only; writes no note, calls no AI."""
    service = getattr(request.app.state, "service", None)
    market = None
    if service is not None:
        try:
            market = service.current_state()
        except Exception:  # noqa: BLE001 - a broken feed leaves an empty strip, not a 500
            logger.warning("focus: market state unavailable", exc_info=True)
    focus = await asyncio.to_thread(read_focus, request.app.state)
    return focus_strip(request.app.state, market, focus)


__all__ = ["DEFAULT", "TODAY_STRIP_SIZE", "FocusStrip", "GlobalFocus", "focus_strip",
           "read_focus", "router"]
