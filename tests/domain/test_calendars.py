"""Trading calendars: CME wraps the Globex engine unchanged; 24×7 never closes."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from core.domain import sessions as S
from core.domain.calendars import (
    ALWAYS_OPEN,
    CALENDARS,
    CME_GLOBEX,
    calendar_for,
)
from core.domain.instruments import INSTRUMENTS, SessionCalendarId
from core.domain.market_calendar import is_early_close


def hours(start: datetime, end: datetime, step: timedelta = timedelta(hours=1)):
    moment = start
    while moment < end:
        yield moment
        moment += step


# Mid-September to the new year: US DST end, Thanksgiving, its early close,
# Christmas and every weekend and daily break in between.
SPAN = (datetime(2026, 9, 14, tzinfo=UTC), datetime(2027, 1, 4, tzinfo=UTC))


def test_every_calendar_is_registered_by_its_id():
    assert set(CALENDARS) == set(SessionCalendarId)
    assert all(CALENDARS[key].calendar_id is key for key in CALENDARS)


def test_btc_uses_the_24x7_calendar_and_everything_else_cme():
    assert calendar_for("BTC") is ALWAYS_OPEN
    assert calendar_for(INSTRUMENTS["BTC"]) is ALWAYS_OPEN
    for symbol in INSTRUMENTS:
        if symbol != "BTC":
            assert calendar_for(symbol) is CME_GLOBEX, symbol
    with pytest.raises(KeyError):
        calendar_for("NOPE")


def test_cme_calendar_is_the_globex_engine_unchanged():
    for moment in hours(*SPAN, timedelta(minutes=30)):
        assert CME_GLOBEX.market_closed(moment) == S.market_closed(moment)
        assert CME_GLOBEX.active_session(moment) == S.active_session(moment)
        stamp = moment - timedelta(hours=5)
        assert (CME_GLOBEX.closed_since_last_session(stamp, moment)
                == S.closed_since_last_session(stamp, moment))
    day = SPAN[0].date()
    while day < SPAN[1].date():
        assert CME_GLOBEX.session(day) == S.globex_session(day)
        assert CME_GLOBEX.previous_trade_date(day) == S.previous_trade_date(day)
        assert CME_GLOBEX.early_close(day) == is_early_close(day)
        assert CME_GLOBEX.segments(day) == S.session_segments(day)
        day += timedelta(days=1)


def test_24x7_is_never_closed_at_any_hour():
    for moment in hours(*SPAN, timedelta(minutes=15)):
        assert not ALWAYS_OPEN.market_closed(moment)
        assert not ALWAYS_OPEN.closed_since_last_session(moment - timedelta(days=3), moment)
        session = ALWAYS_OPEN.active_session(moment)
        assert session.contains(moment)


def test_24x7_sessions_are_whole_utc_days_without_cash_hours():
    saturday = date(2026, 10, 24)
    session = ALWAYS_OPEN.session(saturday)
    assert session.opens_at == datetime(2026, 10, 24, tzinfo=UTC)
    assert session.closes_at == datetime(2026, 10, 25, tzinfo=UTC)
    assert session.rth_opens_at is None and session.rth_closes_at is None
    assert ALWAYS_OPEN.previous_trade_date(saturday) == date(2026, 10, 23)
    assert not ALWAYS_OPEN.early_close(date(2026, 11, 27))
    assert ALWAYS_OPEN.segments(saturday) == []
    # 23:59 New York on Friday is already Saturday in UTC.
    late_friday = datetime(2026, 10, 24, 3, 59, tzinfo=UTC)
    assert ALWAYS_OPEN.active_session(late_friday).trade_date == saturday
    with pytest.raises(ValueError):
        ALWAYS_OPEN.active_session(datetime(2026, 10, 24, 12, 0))
