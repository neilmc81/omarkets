"""Chart API: bars, the session calendar, key levels, risk tone, pins, sparklines.

Read-only projections of stored observations and the session engine. Every
number is computed here in the core; the web app only draws it.

* ``GET /api/v1/bars/{symbol}?interval=5m|15m|1h&range=1d|5d|1m``
* ``GET /api/v1/session`` — where now sits in the futures day (timeline bar).
* ``GET /api/v1/levels/{symbol}[?date=YYYY-MM-DD]`` — key levels with their explainers,
  now or as they stood at a past session's cash close.
* ``GET /api/v1/risk-tone`` — the rule-based risk tone.
* ``GET /api/v1/chart/pins?range=1d|5d|1m`` — release pins linked to notes.
* ``GET /api/v1/sparklines`` — the last 24 hours of 15-minute closes per tile.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from core.analysis import bars as B
from core.analysis.levels import key_levels
from core.analysis.risk_tone import risk_tone
from core.api.routes import ServiceDep
from core.desk.facts import LABELS, STRIP_SYMBOLS
from core.desk.service import DeskService
from core.domain.calendars import CME_GLOBEX, calendar_for
from core.domain.chart import (
    INTERVAL_SECONDS,
    BarsResponse,
    ChartPins,
    ChartRange,
    Holiday,
    Interval,
    KeyLevels,
    RiskTone,
    SessionCalendar,
    Sparklines,
)
from core.domain.instruments import get_instrument, normalize_symbol
from core.domain.market_calendar import holiday_name, is_early_close, is_trading_day
from core.domain.sessions import (
    SessionPhase,
    active_session,
    globex_session,
    next_session_open,
    next_trade_date,
    session_phase,
)
from core.storage.base import StorageError
from core.storage.duckdb_repo import FINE_RETENTION

router = APIRouter(prefix="/api/v1", tags=["chart"])

#: Futures roots that have key levels (they settle and trade the Globex session).
LEVEL_SYMBOLS = frozenset({"NQ", "ES"})
#: Charts the release pins belong on: each pin carries NQ's 15-minute move.
RELEASE_PIN_SYMBOLS = frozenset({"NQ"})
HOLIDAYS_AHEAD = timedelta(days=21)
SPARKLINE_SPAN = timedelta(hours=24)


def _symbol(symbol: str) -> str:
    canonical = normalize_symbol(symbol)
    if get_instrument(canonical) is None:
        raise HTTPException(status_code=404, detail=f"unknown instrument: {symbol}")
    return canonical


def _value(item: object) -> str:
    return str(getattr(item, "value", item))


@router.get("/bars/{symbol}", response_model=BarsResponse,
            responses={404: {"description": "Unknown instrument symbol"},
                       422: {"description": "Interval not offered for the range"},
                       503: {"description": "Storage unavailable"}})
async def bars(
    symbol: str,
    service: ServiceDep,
    interval: Interval = Interval.M5,
    range: Annotated[ChartRange, Query(alias="range")] = ChartRange.DAY,  # noqa: A002
) -> BarsResponse:
    """OHLC bars from stored observations; gaps inside a session stay gaps.

    Sessions come from the instrument's own calendar: Globex for NQ, a UTC day
    for BTC.
    """
    canonical = _symbol(symbol)
    try:
        B.check_interval(range, interval)
    except B.RangeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    state = service.current_state()
    now = state.generated_at
    calendar = calendar_for(canonical)
    sessions = B.range_sessions(range, now, calendar)
    start, end = B.window(sessions, now)
    try:
        built, buckets = await asyncio.to_thread(
            B.read_bars, service.ohlc, canonical, sessions, interval, end=end)
    except StorageError as exc:
        raise HTTPException(status_code=503, detail="storage unavailable") from exc
    inst = state.instruments.get(canonical)
    fine_since = now - FINE_RETENTION
    note = None
    if start < fine_since:
        note = (f"Bars before {fine_since:%b %d} are built from one stored quote a minute, "
                "so a brief spike inside a minute can be missing from their high and low.")
    present = [b for b in built if b.count]
    return BarsResponse(
        symbol=canonical,
        label=LABELS.get(canonical, inst.display_name if inst else canonical),
        unit=inst.unit if inst else "",
        interval=interval, range=range, generated_at=now, start=start, end=end,
        sessions=[B.chart_session(s, calendar) for s in sessions],
        bars=built,
        last_at=max((b.last_at for b in buckets), default=None),
        delayed=any(b.delayed for b in present),
        feed_status=_value(inst.feed_status) if inst else "unavailable",
        status=_value(inst.status) if inst else "unavailable",
        market_closed=bool(getattr(inst, "market_closed", False)),
        sources=sorted({s for b in buckets for s in b.sources}),
        contracts=sorted({c for b in buckets for c in b.contracts}),
        fine_since=fine_since, note=note,
    )


@router.get("/session", response_model=SessionCalendar)
async def session(service: ServiceDep) -> SessionCalendar:
    """The active (or last) session with its segments, the break and holidays ahead."""
    now = service.current_state().generated_at
    current = active_session(now)
    phase = session_phase(now)
    nxt = next_trade_date(current.trade_date)
    # The one-hour daily break, only when the next session opens the same evening.
    daily = nxt == current.trade_date + timedelta(days=1) and not is_early_close(
        current.trade_date)
    maintenance = (current.closes_at, globex_session(nxt).opens_at) if daily else (None, None)
    holidays: list[Holiday] = []
    day: date = now.date()
    while day <= (now + HOLIDAYS_AHEAD).date():
        name = holiday_name(day)
        if name is not None:
            holidays.append(Holiday(date=day, name=name))
        elif is_early_close(day):
            holidays.append(Holiday(date=day, name="Early close (13:00 ET)", early_close=True))
        day += timedelta(days=1)
    return SessionCalendar(
        now=now, phase=phase, text=DeskService.session_text(now),
        session=B.chart_session(current, CME_GLOBEX),
        maintenance_starts_at=maintenance[0], maintenance_ends_at=maintenance[1],
        next_open_at=(next_session_open(now)
                      if phase in (SessionPhase.CLOSED, SessionPhase.MAINTENANCE) else None),
        holidays=holidays,
    )


@router.get("/levels/{symbol}", response_model=KeyLevels,
            responses={404: {"description": "No key levels for this symbol"},
                       422: {"description": "Date is not a trading day, or is in the future"},
                       503: {"description": "Storage unavailable"}})
async def levels(
    symbol: str,
    service: ServiceDep,
    trade_date: Annotated[date | None, Query(alias="date")] = None,
) -> KeyLevels:
    """Overnight, prior-day and prior-week levels, the settle and the gap.

    With ``date`` (a trade date), the levels as they stood at that session's cash
    close, or now while it is still before the close.
    """
    canonical = _symbol(symbol)
    if canonical not in LEVEL_SYMBOLS:
        raise HTTPException(status_code=404, detail=f"no key levels for {canonical}")
    state = service.current_state()
    now, instrument = state.generated_at, state.instruments.get(canonical)
    if trade_date is not None:
        if not is_trading_day(trade_date):
            raise HTTPException(status_code=422, detail=f"{trade_date} is not a trading day")
        if trade_date > active_session(now).trade_date:
            raise HTTPException(status_code=422, detail=f"{trade_date} has not started yet")
    try:
        if trade_date is not None and globex_session(trade_date).rth_closes_at < now:
            now = globex_session(trade_date).rth_closes_at
            instrument = await asyncio.to_thread(_stored_instrument, service, canonical, now)
        return await asyncio.to_thread(key_levels, canonical, instrument, service.ohlc, now=now)
    except StorageError as exc:
        raise HTTPException(status_code=503, detail="storage unavailable") from exc


def _stored_instrument(service, symbol: str, at: datetime) -> SimpleNamespace | None:
    """The last quote we had by ``at`` in its session, shaped like live state.

    A past session's settle and contract come from what the feed sent then, not
    from today's quote. Quotes that arrived after ``at`` or carry only a date
    are skipped: CNBC stamps date-only quotes 16:00 ET and sends them hours
    later with the *next* session's settle.
    """
    session = active_session(at)
    quotes = service.history(symbol, start=session.opens_at,
                             end=at - timedelta(microseconds=1), limit=200)
    for quote in reversed(quotes):
        settle = quote.metadata.get("previous_close")
        if (settle is not None and quote.received_at <= at
                and quote.metadata.get("exchange_time_known") is not False):
            return SimpleNamespace(value=quote.value, previous_close=float(settle),
                                   contract_code=quote.metadata.get("contract_code"),
                                   feed_status=quote.metadata.get("feed_status"))
    return None


@router.get("/risk-tone", response_model=RiskTone)
async def risk(service: ServiceDep) -> RiskTone:
    """Rule-based risk tone from NQ, the 2-year yield, the dollar and VIX."""
    state = service.current_state()
    return risk_tone(state, now=state.generated_at)


@router.get("/chart/pins", response_model=ChartPins,
            responses={503: {"description": "Desk or storage unavailable"}})
async def chart_pins(
    request: Request,
    service: ServiceDep,
    range: Annotated[ChartRange, Query(alias="range")] = ChartRange.DAY,  # noqa: A002
) -> ChartPins:
    """Scheduled releases in the chart window, with results and note links."""
    desk = getattr(request.app.state, "desk", None)
    if desk is None:
        raise HTTPException(status_code=503, detail="Desk is not enabled")
    now = service.current_state().generated_at
    # Release pins sit on the NQ chart, so they follow the Globex sessions.
    sessions = B.range_sessions(range, now, CME_GLOBEX)
    start, end = sessions[0].opens_at, sessions[-1].closes_at
    try:
        found = await asyncio.to_thread(desk.chart_pins, start, end)
    except StorageError as exc:
        raise HTTPException(status_code=503, detail="storage unavailable") from exc
    return ChartPins(range=range, start=start, end=end, pins=found)


@router.get("/sparklines", response_model=Sparklines,
            responses={503: {"description": "Storage unavailable"}})
async def sparklines(service: ServiceDep) -> Sparklines:
    """15-minute closes over the 24 hours to each strip instrument's latest quote."""
    state = service.current_state()
    interval = Interval.M15

    def build() -> list:
        lines = []
        for symbol in STRIP_SYMBOLS:
            inst = state.instruments.get(symbol)
            if inst is None or inst.timestamp is None:
                lines.append(B.sparkline(symbol, [], interval))
                continue
            end = inst.timestamp + timedelta(seconds=1)
            lines.append(B.sparkline(symbol, service.ohlc(
                symbol, start=end - SPARKLINE_SPAN, end=end,
                bucket_seconds=INTERVAL_SECONDS[interval]), interval))
        return lines

    try:
        lines = await asyncio.to_thread(build)
    except StorageError as exc:
        raise HTTPException(status_code=503, detail="storage unavailable") from exc
    return Sparklines(generated_at=state.generated_at, interval=interval, lines=lines)


__all__ = ["router"]
