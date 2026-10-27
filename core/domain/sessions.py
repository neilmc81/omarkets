"""Deterministic US equity-index futures session engine.

NQ and ES trade on CME Globex far beyond US cash-equity hours. This module is
the single definition of what a "session" is, so bar bucketing, session levels,
VWAP anchoring, and the opening range all agree.

Definitions (all times ``America/New_York``, converted to UTC):

* **Globex session for trade date D** — opens 18:00 ET on the previous calendar
  day and closes 17:00 ET on D. The Monday session therefore opens Sunday
  18:00 ET; the Friday session closes Friday 17:00 ET for the weekend.
* **RTH (regular trading hours)** for trade date D — 09:30–16:00 ET on D. This
  is the US cash-equity window and is *not* the whole futures session.
* **Maintenance break** — 17:00–18:00 ET on trading weekdays, between the close
  of one Globex session and the open of the next.
* **Weekend close** — Friday 17:00 ET through Sunday 18:00 ET.
* **Segments** of a session, for display (:func:`session_segments`): *Asia* from
  the Globex open to the London open, *Europe* from the London open (08:00
  Europe/London, so the UK's own clock changes are respected) to 08:00 ET,
  *pre-market* 08:00 ET to the cash open (it holds the 08:30 ET data releases),
  *US cash* (RTH) and *after the cash close* until the Globex close.
* **Volatility-index hours** — VIX and VXN are Cboe cash indexes computed
  03:15–16:15 ET on trading days; outside those hours their last value is a
  close, not a stale feed (:func:`volatility_index_open`).

Rules:

* All wall-clock times are localized per calendar date with ``zoneinfo``, so DST
  transitions are handled by the tz database and never by a fixed UTC offset.
* Exchange holidays and early-close (half) days are modeled by
  :mod:`core.domain.market_calendar` and consulted here. CME's 2026 Labor Day
  exception has a Sunday pre-open and Monday midday halt; other ad-hoc exchange
  schedules are not guessed from the NYSE calendar.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from core.domain.market_calendar import (
    EARLY_CLOSE_GLOBEX_TIME,
    is_early_close,
    is_trading_day,
    previous_trading_day,
    rth_close_time,
)

NEW_YORK = ZoneInfo("America/New_York")
LONDON = ZoneInfo("Europe/London")

#: RTH wall-clock bounds (US cash equities).
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)
#: Globex session wall-clock open on the prior calendar day and close on D.
GLOBEX_OPEN = time(18, 0)
GLOBEX_CLOSE = time(17, 0)
#: The European segment starts at the London open, in London time.
LONDON_OPEN = time(8, 0)
#: The US pre-market segment starts 08:00 ET, before the 08:30 ET data releases.
PREMARKET_OPEN = time(8, 0)
#: Cboe computes VIX and VXN from 03:15 to 16:15 ET (global and regular hours).
VOLATILITY_INDEX_HOURS = (time(3, 15), time(16, 15))

# CME lists the 2026 Labor Day Sunday pre-open under Tuesday's trade date.
# Index futures trade Sunday evening through Monday 13:00 ET, then reopen
# Monday 18:00 ET. Keep this exchange schedule separate from the NYSE calendar.
# Source: https://www.cmegroup.com/trading-hours.html (2026 Globex schedule).
_LABOR_DAY_2026 = date(2026, 9, 7)
_LABOR_DAY_TRADE_DATE = date(2026, 9, 8)
_LABOR_DAY_HALT = time(13, 0)


class SessionPhase(StrEnum):
    """Where an instant sits relative to the futures trading cycle."""

    RTH = "rth"
    OVERNIGHT = "overnight"
    MAINTENANCE = "maintenance"
    CLOSED = "closed"


def _et_utc(day: date, moment: time) -> datetime:
    """Localize a wall-clock ET time on ``day`` and return it in UTC."""
    return datetime.combine(day, moment, tzinfo=NEW_YORK).astimezone(UTC)


class TradingSession(BaseModel):
    """One trading session of any calendar (:mod:`core.domain.calendars`), in UTC.

    ``rth_*`` is the regular (cash) window inside the session, or ``None`` for a
    market that has none (crypto).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    trade_date: date
    opens_at: datetime
    closes_at: datetime
    rth_opens_at: datetime | None = None
    rth_closes_at: datetime | None = None

    def contains(self, moment: datetime) -> bool:
        return self.opens_at <= moment < self.closes_at


class GlobexSession(TradingSession):
    """A single CME Globex trading session, in UTC."""

    rth_opens_at: datetime
    rth_closes_at: datetime

    def in_rth(self, moment: datetime) -> bool:
        return self.rth_opens_at <= moment < self.rth_closes_at


class SegmentName(StrEnum):
    """Display segments of a Globex session, in time order."""

    ASIA = "asia"
    EUROPE = "europe"
    PREMARKET = "premarket"
    CASH = "cash"
    AFTER_HOURS = "after_hours"


SEGMENT_LABELS: dict[SegmentName, str] = {
    SegmentName.ASIA: "Asia",
    SegmentName.EUROPE: "Europe",
    SegmentName.PREMARKET: "Pre-market",
    SegmentName.CASH: "US cash",
    SegmentName.AFTER_HOURS: "After the cash close",
}


class SessionSegment(BaseModel):
    """One display segment of a session, in UTC; never empty."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: SegmentName
    label: str
    starts_at: datetime
    ends_at: datetime
    #: Plain-English text from the explainer library (set by the chart API).
    explainer: str | None = None


def is_trading_weekday(day: date) -> bool:
    """True for Monday–Friday, regardless of exchange holidays.

    Prefer :func:`is_trading_day` when holiday correctness matters (session
    generation, historical ranges); this stays deliberately weekday-only for
    callers that need "a weekday" as distinct from "a market day".
    """
    return day.weekday() < 5


def globex_close_time(day: date) -> time:
    """CME Globex close for a trade date (13:15 ET on early-close days)."""
    return EARLY_CLOSE_GLOBEX_TIME if is_early_close(day) else GLOBEX_CLOSE


#: ``globex_session`` is pure and heavily reused (bar bucketing, session
#: levels, VWAP, active-session lookup), so its result is cached per trade date.
#: The tz-database localization is the expensive part; the value is immutable.
_SESSION_CACHE: dict[date, GlobexSession] = {}


def globex_session(trade_date: date) -> GlobexSession:
    """The Globex session for a trade date.

    The session opens 18:00 ET the prior calendar day, so the Monday session
    opens Sunday evening. On an exchange holiday the session is still generated
    (frozen, cached) but marks the holiday trade date; callers must consult
    :func:`is_trading_day` before treating it as tradable. Results are cached.
    """
    cached = _SESSION_CACHE.get(trade_date)
    if cached is not None:
        return cached
    prior = (
        date(2026, 9, 6) if trade_date == _LABOR_DAY_TRADE_DATE else trade_date - timedelta(days=1)
    )
    session = GlobexSession(
        trade_date=trade_date,
        opens_at=_et_utc(prior, GLOBEX_OPEN),
        closes_at=_et_utc(trade_date, globex_close_time(trade_date)),
        rth_opens_at=_et_utc(trade_date, RTH_OPEN),
        rth_closes_at=_et_utc(trade_date, rth_close_time(trade_date)),
    )
    # Bound the cache: a long-running process only ever works near "now".
    if len(_SESSION_CACHE) > 64:
        for stale in sorted(_SESSION_CACHE)[:-32]:
            _SESSION_CACHE.pop(stale, None)
    _SESSION_CACHE[trade_date] = session
    return session


def session_segments(trade_date: date) -> list[SessionSegment]:
    """The Asia / Europe / pre-market / cash / after-hours segments of a session.

    Boundaries are clipped to the session, so an early-close day ends with a
    short after-hours segment (13:00–13:15 ET) and a segment that would be empty
    is left out. The 2026 Labor Day session keeps its long Asia segment (Sunday
    18:00 ET onward) and its Monday halt is not shown as a segment.
    """
    session = globex_session(trade_date)
    london = datetime.combine(trade_date, LONDON_OPEN, tzinfo=LONDON).astimezone(UTC)
    premarket = _et_utc(trade_date, PREMARKET_OPEN)
    bounds = [
        (SegmentName.ASIA, session.opens_at, london),
        (SegmentName.EUROPE, london, premarket),
        (SegmentName.PREMARKET, premarket, session.rth_opens_at),
        (SegmentName.CASH, session.rth_opens_at, session.rth_closes_at),
        (SegmentName.AFTER_HOURS, session.rth_closes_at, session.closes_at),
    ]
    segments: list[SessionSegment] = []
    for name, start, end in bounds:
        start = min(max(start, session.opens_at), session.closes_at)
        end = min(max(end, start), session.closes_at)
        if end > start:
            segments.append(SessionSegment(name=name, label=SEGMENT_LABELS[name],
                                           starts_at=start, ends_at=end))
    return segments


def volatility_index_open(moment: datetime) -> bool:
    """Whether Cboe is computing VIX/VXN now (03:15–16:15 ET on trading days)."""
    day, local = local_parts(moment)
    opens, closes = VOLATILITY_INDEX_HOURS
    return is_trading_day(day) and opens <= local < closes


def previous_trade_date(day: date) -> date:
    """The previous trading day (weekends and exchange holidays skipped)."""
    return previous_trading_day(day)


def next_trade_date(day: date) -> date:
    """The next trading day (weekends and exchange holidays skipped)."""
    from core.domain.market_calendar import next_trading_day

    return next_trading_day(day)


def _require_aware(moment: datetime) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("session calculations require a timezone-aware datetime")
    return moment.astimezone(UTC)


def local_parts(moment: datetime) -> tuple[date, time]:
    """Return the ``(local date, local time)`` in ``America/New_York``."""
    local = _require_aware(moment).astimezone(NEW_YORK)
    return local.date(), local.time()


def session_phase(moment: datetime) -> SessionPhase:
    """Classify an instant into RTH, overnight, maintenance, or closed.

    A session for trade date T opens 18:00 ET on T−1, so Sunday evening is
    already the Monday session's overnight phase. Holidays and weekends are
    normally closed all day, except the 2026 Labor Day futures schedule; on an
    early-close day RTH ends at 13:00 ET and futures close at 13:15 ET. The
    17:00–18:00 ET maintenance halt is reported only when the next trading day
    is the next calendar day.
    """
    now = _require_aware(moment)
    day, local = local_parts(now)
    if day == _LABOR_DAY_2026 and _LABOR_DAY_HALT <= local < GLOBEX_OPEN:
        return SessionPhase.CLOSED
    for trade_date in (day, day + timedelta(days=1), day + timedelta(days=2)):
        if not is_trading_day(trade_date):
            continue
        session = globex_session(trade_date)
        if session.contains(now):
            return SessionPhase.RTH if session.in_rth(now) else SessionPhase.OVERNIGHT
    # Not inside any session: either the weekday maintenance halt or a closure.
    if is_trading_day(day) and local >= globex_close_time(day):
        if next_trade_date(day) == day + timedelta(days=1) and local < GLOBEX_OPEN:
            return SessionPhase.MAINTENANCE
    return SessionPhase.CLOSED


def trading_windows(session: GlobexSession) -> list[tuple[datetime, datetime]]:
    """The spans of ``session`` when futures trade, in UTC: the whole session,
    except the 2026 Labor Day session, which leaves out its Monday halt."""
    if session.trade_date != _LABOR_DAY_TRADE_DATE:
        return [(session.opens_at, session.closes_at)]
    return [(session.opens_at, _et_utc(_LABOR_DAY_2026, _LABOR_DAY_HALT)),
            (_et_utc(_LABOR_DAY_2026, GLOBEX_OPEN), session.closes_at)]


def market_closed(moment: datetime) -> bool:
    """True on a weekend, holiday or the daily maintenance break."""
    return session_phase(moment) in (SessionPhase.CLOSED, SessionPhase.MAINTENANCE)


def closed_since_last_session(timestamp: datetime, moment: datetime) -> bool:
    """Whether a quote stamped ``timestamp`` is the last print before a closure.

    True only while the market is shut at ``moment`` and the quote is from the
    most recently completed session, so a feed that died earlier in the week
    is still reported as stale rather than excused by the weekend.
    """
    if not market_closed(moment):
        return False
    last = active_session(moment)
    return last.opens_at <= _require_aware(timestamp) <= _require_aware(moment)


def in_maintenance_break(moment: datetime) -> bool:
    return session_phase(moment) is SessionPhase.MAINTENANCE


def next_session_open(moment: datetime) -> datetime:
    """Next tradable Globex open after an instant, in UTC.

    A bounded calendar search also covers holiday closures and the documented
    Labor Day halt without guessing a fixed Sunday or weekday opening time.
    """
    now = _require_aware(moment)
    day, local = local_parts(now)
    if day == _LABOR_DAY_2026 and _LABOR_DAY_HALT <= local < GLOBEX_OPEN:
        return _et_utc(day, GLOBEX_OPEN)
    for offset in range(0, 15):
        trade_date = day + timedelta(days=offset)
        if not is_trading_day(trade_date):
            continue
        opening = globex_session(trade_date).opens_at
        if opening > now and session_phase(opening) in (SessionPhase.RTH, SessionPhase.OVERNIGHT):
            return opening
    raise RuntimeError("no upcoming Globex session in the exchange calendar")


#: Longest pause between closed-market checks (see :func:`closed_market_delay`).
CLOSED_MARKET_MAX_DELAY = 3600.0


def closed_market_delay(moment: datetime, *,
                        maximum_seconds: float = CLOSED_MARKET_MAX_DELAY) -> float:
    """Seconds to the next closed-market check, never past the next open."""
    if session_phase(moment) not in (SessionPhase.CLOSED, SessionPhase.MAINTENANCE):
        return 0.0
    until_open = (next_session_open(moment) - moment.astimezone(UTC)).total_seconds()
    return max(0.0, min(maximum_seconds, until_open))


def active_session(moment: datetime) -> GlobexSession:
    """The Globex session containing ``moment``.

    During an ordinary maintenance break, holiday, or weekend close, the most
    recently completed trading session is returned. The 2026 Labor Day midday
    halt remains assigned to Tuesday's trade date, which already traded during
    its Sunday/Monday pre-halt segment.
    """
    now = _require_aware(moment)
    local_day, _ = local_parts(now)
    candidates = [
        globex_session(local_day),
        globex_session(local_day + timedelta(days=1)),
        globex_session(local_day + timedelta(days=2)),
    ]
    candidates = [session for session in candidates if is_trading_day(session.trade_date)]
    for session in candidates:
        if session.contains(now):
            return session
    # Closed: return the latest session that has already closed.
    completed = [session for session in candidates if session.closes_at <= now]
    if completed:
        return max(completed, key=lambda session: session.closes_at)
    # Weekend / holiday / early Sunday: fall back to the prior trading session.
    return globex_session(previous_trade_date(local_day))


def current_trade_date(moment: datetime) -> date:
    """The trade date of the active (or most recently completed) session."""
    return active_session(moment).trade_date


def previous_session(moment: datetime) -> GlobexSession:
    """The session immediately before the active one."""
    return globex_session(previous_trade_date(active_session(moment).trade_date))


def week_start(day: date) -> date:
    """Monday of the trading week containing ``day``."""
    return day - timedelta(days=day.weekday())


def session_range(start: date, end: date) -> list[GlobexSession]:
    """Globex sessions for every trading day in ``[start, end)``."""
    sessions: list[GlobexSession] = []
    cursor = start
    while cursor < end:
        if is_trading_day(cursor):
            sessions.append(globex_session(cursor))
        cursor += timedelta(days=1)
    return sessions


def previous_week_sessions(moment: datetime) -> tuple[date, date, list[GlobexSession]]:
    """Trading sessions of the previous calendar week.

    Returns ``(week_start, week_end_exclusive, sessions)`` for the Mon–Fri week
    before the active session's week.
    """
    trade_date = active_session(moment).trade_date
    this_monday = week_start(trade_date)
    prev_monday = this_monday - timedelta(days=7)
    prev_friday_exclusive = prev_monday + timedelta(days=5)
    return prev_monday, prev_friday_exclusive, session_range(prev_monday, prev_friday_exclusive)


__all__ = [
    "CLOSED_MARKET_MAX_DELAY",
    "GLOBEX_CLOSE",
    "GLOBEX_OPEN",
    "GlobexSession",
    "LONDON",
    "NEW_YORK",
    "RTH_CLOSE",
    "RTH_OPEN",
    "SEGMENT_LABELS",
    "SegmentName",
    "SessionPhase",
    "SessionSegment",
    "TradingSession",
    "active_session",
    "current_trade_date",
    "globex_session",
    "in_maintenance_break",
    "is_trading_weekday",
    "closed_since_last_session",
    "local_parts",
    "market_closed",
    "next_trade_date",
    "previous_session",
    "previous_trade_date",
    "previous_week_sessions",
    "session_phase",
    "session_range",
    "session_segments",
    "trading_windows",
    "volatility_index_open",
    "week_start",
]
