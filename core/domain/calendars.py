"""Trading calendars: when each instrument's market is open.

Every instrument names a calendar (``Instrument.session_calendar``) and code
that needs "is this market open?" or "which sessions does this chart cover?"
asks that calendar through :func:`calendar_for`, never the CME functions
directly. Two calendars exist:

* :data:`CME_GLOBEX` wraps the existing CME Globex engine in
  :mod:`core.domain.sessions` unchanged (sessions, holidays, early closes, the
  daily break). Every instrument except BTC uses it, as before.
* :data:`ALWAYS_OPEN` is the 24×7 calendar for crypto: never closed, and a
  "day" (a chart session) is 00:00–24:00 UTC.

Other venues (US stocks, forex, Europe) add a calendar in their own phase of the
multi-asset plan (``docs/PLAN.md`` §6). Code that is about the NQ day
specifically (the session timeline, levels, desk scheduling) still calls
:mod:`core.domain.sessions` and moves over one slice at a time.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from types import MappingProxyType

from core.domain import sessions as S
from core.domain.instruments import Instrument, SessionCalendarId, get_instrument
from core.domain.market_calendar import is_early_close
from core.domain.sessions import SessionSegment, TradingSession


class SessionCalendar(ABC):
    """When one family of markets trades. All instants are timezone-aware UTC."""

    calendar_id: SessionCalendarId
    #: Short plain-English name for tables and hovers.
    label: str

    @abstractmethod
    def session(self, trade_date: date) -> TradingSession:
        """The session for a trade date (callers skip non-trading dates)."""

    @abstractmethod
    def active_session(self, moment: datetime) -> TradingSession:
        """The session containing ``moment``, else the most recently completed one."""

    @abstractmethod
    def previous_trade_date(self, day: date) -> date:
        """The trading day before ``day``."""

    @abstractmethod
    def early_close(self, trade_date: date) -> bool:
        """Whether the session closes early (a half day)."""

    @abstractmethod
    def segments(self, trade_date: date) -> list[SessionSegment]:
        """Display segments of the session; empty when it has none."""

    @abstractmethod
    def market_closed(self, moment: datetime) -> bool:
        """Whether the market is shut at ``moment``."""

    @abstractmethod
    def closed_since_last_session(self, timestamp: datetime, moment: datetime) -> bool:
        """Whether a quote stamped ``timestamp`` is old only because the market shut."""


class CmeGlobexCalendar(SessionCalendar):
    """CME Globex equity-index futures hours: :mod:`core.domain.sessions`, unchanged."""

    calendar_id = SessionCalendarId.CME_GLOBEX
    label = "CME Globex (Sun–Fri 18:00–17:00 ET)"

    def session(self, trade_date: date) -> TradingSession:
        return S.globex_session(trade_date)

    def active_session(self, moment: datetime) -> TradingSession:
        return S.active_session(moment)

    def previous_trade_date(self, day: date) -> date:
        return S.previous_trade_date(day)

    def early_close(self, trade_date: date) -> bool:
        return is_early_close(trade_date)

    def segments(self, trade_date: date) -> list[SessionSegment]:
        return S.session_segments(trade_date)

    def market_closed(self, moment: datetime) -> bool:
        return S.market_closed(moment)

    def closed_since_last_session(self, timestamp: datetime, moment: datetime) -> bool:
        return S.closed_since_last_session(timestamp, moment)


class AlwaysOpenCalendar(SessionCalendar):
    """24×7 trading (crypto). Never closed; each chart session is one UTC day."""

    calendar_id = SessionCalendarId.ALWAYS_OPEN
    label = "24×7 (day = 00:00–24:00 UTC)"

    def session(self, trade_date: date) -> TradingSession:
        opens = datetime.combine(trade_date, time(0), tzinfo=UTC)
        return TradingSession(trade_date=trade_date, opens_at=opens,
                              closes_at=opens + timedelta(days=1))

    def active_session(self, moment: datetime) -> TradingSession:
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("session calculations require a timezone-aware datetime")
        return self.session(moment.astimezone(UTC).date())

    def previous_trade_date(self, day: date) -> date:
        return day - timedelta(days=1)

    def early_close(self, trade_date: date) -> bool:
        return False

    def segments(self, trade_date: date) -> list[SessionSegment]:
        return []

    def market_closed(self, moment: datetime) -> bool:
        return False

    def closed_since_last_session(self, timestamp: datetime, moment: datetime) -> bool:
        return False


CME_GLOBEX: SessionCalendar = CmeGlobexCalendar()
ALWAYS_OPEN: SessionCalendar = AlwaysOpenCalendar()

#: Every calendar by id. Read-only.
CALENDARS: Mapping[SessionCalendarId, SessionCalendar] = MappingProxyType(
    {calendar.calendar_id: calendar for calendar in (CME_GLOBEX, ALWAYS_OPEN)}
)


def calendar_for(instrument: Instrument | str) -> SessionCalendar:
    """The calendar an instrument (or canonical symbol) trades on."""
    if isinstance(instrument, str):
        found = get_instrument(instrument)
        if found is None:
            raise KeyError(f"unknown instrument symbol: {instrument!r}")
        instrument = found
    return CALENDARS[instrument.session_calendar]


__all__ = [
    "ALWAYS_OPEN",
    "AlwaysOpenCalendar",
    "CALENDARS",
    "CME_GLOBEX",
    "CmeGlobexCalendar",
    "SessionCalendar",
    "calendar_for",
]
