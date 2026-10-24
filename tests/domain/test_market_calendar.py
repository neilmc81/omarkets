"""Tests for the deterministic US market calendar (holidays + early closes).

The rules are computed from the calendar, so these tests pin known dates across
several years rather than a hand-maintained list.
"""

from __future__ import annotations

from datetime import date, time

import pytest

from core.domain import market_calendar as cal
from core.domain.sessions import (
    SessionPhase,
    globex_session,
    is_trading_weekday,
    next_trade_date,
    previous_trade_date,
    session_phase,
    session_range,
)


def test_fixed_holidays_move_to_the_nearest_weekday():
    # 2027-01-01 is a Friday: observed on the day.
    assert cal.holiday_name(date(2027, 1, 1)) == "New Year's Day"
    # 2022-01-01 is a Saturday: observed Friday 2021-12-31.
    assert cal.holiday_name(date(2021, 12, 31)) == "New Year's Day"
    # 2023-01-01 is a Sunday: observed Monday 2023-01-02.
    assert cal.holiday_name(date(2023, 1, 2)) == "New Year's Day"
    # 2021-07-04 is a Sunday: observed Monday 2021-07-05.
    assert cal.holiday_name(date(2021, 7, 5)) == "Independence Day"
    # 2020-12-25 is a Friday: observed on the day.
    assert cal.holiday_name(date(2020, 12, 25)) == "Christmas Day"
    # 2021-12-25 is a Saturday: observed Friday 2021-12-24.
    assert cal.holiday_name(date(2021, 12, 24)) == "Christmas Day"


def test_floating_holidays():
    assert cal.holiday_name(date(2026, 1, 19)) == "Martin Luther King Jr. Day"
    assert cal.holiday_name(date(2026, 2, 16)) == "Washington's Birthday"
    assert cal.holiday_name(date(2026, 5, 25)) == "Memorial Day"
    assert cal.holiday_name(date(2026, 9, 7)) == "Labor Day"
    assert cal.holiday_name(date(2026, 11, 26)) == "Thanksgiving Day"
    # Good Friday 2026 is April 3.
    assert cal.good_friday(2026) == date(2026, 4, 3)
    assert cal.holiday_name(date(2026, 4, 3)) == "Good Friday"
    # 2024 Easter was March 31, so Good Friday was March 29.
    assert cal.good_friday(2024) == date(2024, 3, 29)


def test_juneteenth_only_from_2022():
    # 2022-06-19 is a Sunday: observed Monday 2022-06-20.
    assert cal.holiday_name(date(2022, 6, 20)) == "Juneteenth"
    # Before 2022 it was a normal trading day.
    assert cal.is_trading_day(date(2021, 6, 18))


def test_early_close_days():
    # Friday after Thanksgiving 2026 is Nov 27.
    assert cal.is_early_close(date(2026, 11, 27))
    assert cal.trading_day_kind(date(2026, 11, 27)) is cal.TradingDayKind.EARLY_CLOSE
    # Christmas Eve 2026 is a Thursday: early close.
    assert cal.is_early_close(date(2026, 12, 24))
    # July 3 2024 is a Wednesday and July 4 a Thursday: July 3 early-closes.
    assert cal.is_early_close(date(2024, 7, 3))
    # July 3 2026 is a Friday and July 4 a Saturday, so July 4 is observed on
    # Friday July 3 as the holiday (not an early close).
    assert cal.is_holiday(date(2026, 7, 3))
    assert not cal.is_early_close(date(2026, 7, 3))


def test_rth_close_time_on_half_day():
    assert cal.rth_close_time(date(2026, 11, 27)) == time(13, 0)
    assert cal.rth_close_time(date(2026, 11, 25)) == time(16, 0)


def test_holiday_session_phase_respects_globex_hours():
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    # Labor Day 2026-09-07 at noon ET.
    noon = datetime(2026, 9, 7, 12, 0, tzinfo=ny).astimezone(UTC)
    assert session_phase(noon) is SessionPhase.OVERNIGHT


def test_early_close_session_phase():
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    # Friday after Thanksgiving 2026-11-27: the RTH window ends at 13:00 ET and
    # the futures session closes at 13:15 ET. Because the next trading day is
    # Monday (not tomorrow), the evening is CLOSED, not a maintenance halt.
    midday = datetime(2026, 11, 27, 12, 0, tzinfo=ny).astimezone(UTC)
    assert session_phase(midday) is SessionPhase.RTH
    after_close = datetime(2026, 11, 27, 14, 0, tzinfo=ny).astimezone(UTC)
    assert session_phase(after_close) is SessionPhase.CLOSED


def test_early_close_before_holiday_is_closed_not_maintenance():
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    # Christmas Eve 2026-12-24 (early close); next trading day is Monday Dec 28,
    # so the evening is CLOSED, not a maintenance halt.
    evening = datetime(2026, 12, 24, 20, 0, tzinfo=ny).astimezone(UTC)
    assert session_phase(evening) is SessionPhase.CLOSED


def test_trading_day_navigation_skips_holidays():
    # The Friday before the 2026-09-07 Labor Day holiday is 2026-09-04.
    assert next_trade_date(date(2026, 9, 4)) == date(2026, 9, 8)
    assert previous_trade_date(date(2026, 9, 8)) == date(2026, 9, 4)
    # Thanksgiving week 2026: Wed 25 -> Fri 27 (early close) -> Mon 30.
    assert next_trade_date(date(2026, 11, 25)) == date(2026, 11, 27)
    assert next_trade_date(date(2026, 11, 27)) == date(2026, 11, 30)


def test_session_range_skips_holidays():
    # A week containing Labor Day 2026 (Mon Sep 7).
    sessions = session_range(date(2026, 9, 7), date(2026, 9, 12))
    assert [s.trade_date for s in sessions] == [
        date(2026, 9, 8),
        date(2026, 9, 9),
        date(2026, 9, 10),
        date(2026, 9, 11),
    ]


def test_holiday_globex_session_is_short_closed():
    # On an early-close trade date the generated session closes at 13:15 ET.
    session = globex_session(date(2026, 11, 27))
    from zoneinfo import ZoneInfo

    assert session.closes_at.astimezone(ZoneInfo("America/New_York")).hour == 13
    assert session.closes_at.astimezone(ZoneInfo("America/New_York")).minute == 15


def test_is_trading_weekday_stays_weekday_only():
    # The narrow helper remains weekday-only by design; is_trading_day is the
    # holiday-aware one.
    assert is_trading_weekday(date(2026, 9, 7)) is True
    assert cal.is_trading_day(date(2026, 9, 7)) is False


@pytest.mark.parametrize(
    "day",
    [
        date(2026, 1, 1),
        date(2026, 1, 19),
        date(2026, 2, 16),
        date(2026, 4, 3),
        date(2026, 5, 25),
        date(2026, 6, 19),
        date(2026, 7, 3),
        date(2026, 9, 7),
        date(2026, 11, 26),
        date(2026, 12, 25),
    ],
)
def test_2026_holidays(day):
    assert cal.is_holiday(day)
