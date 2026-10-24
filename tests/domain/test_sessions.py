"""Session engine tests: boundaries, DST, weekends, prior sessions."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from core.domain.sessions import (
    SessionPhase,
    active_session,
    globex_session,
    in_maintenance_break,
    is_trading_weekday,
    previous_session,
    previous_week_sessions,
    session_phase,
    session_range,
    session_segments,
    volatility_index_open,
)

NY = ZoneInfo("America/New_York")


def et_utc(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=NY).astimezone(UTC)


# Wednesday 2026-09-16 is a normal trading weekday.
WEDNESDAY = date(2026, 9, 16)
THURSDAY = date(2026, 9, 17)


def test_globex_session_opens_prior_evening_and_closes_next_afternoon():
    session = globex_session(WEDNESDAY)
    assert session.opens_at == et_utc(date(2026, 9, 15), 18, 0)
    assert session.closes_at == et_utc(WEDNESDAY, 17, 0)
    assert session.rth_opens_at == et_utc(WEDNESDAY, 9, 30)
    assert session.rth_closes_at == et_utc(WEDNESDAY, 16, 0)


def test_monday_session_opens_sunday_evening():
    monday = date(2026, 9, 14)
    session = globex_session(monday)
    assert session.opens_at == et_utc(date(2026, 9, 13), 18, 0)


def test_2026_labor_day_globex_session_and_halt():
    sunday = date(2026, 9, 6)
    monday = date(2026, 9, 7)
    tuesday = date(2026, 9, 8)
    assert globex_session(tuesday).opens_at == et_utc(sunday, 18)
    assert session_phase(et_utc(sunday, 19)) is SessionPhase.OVERNIGHT
    assert session_phase(et_utc(monday, 10)) is SessionPhase.OVERNIGHT
    assert active_session(et_utc(monday, 10)).trade_date == tuesday
    assert session_phase(et_utc(monday, 13)) is SessionPhase.CLOSED
    assert session_phase(et_utc(monday, 17, 59)) is SessionPhase.CLOSED
    assert session_phase(et_utc(monday, 18)) is SessionPhase.OVERNIGHT


def test_phases_across_a_normal_day():
    assert session_phase(et_utc(WEDNESDAY, 19, 0)) is SessionPhase.OVERNIGHT
    assert session_phase(et_utc(WEDNESDAY, 9, 0)) is SessionPhase.OVERNIGHT
    assert session_phase(et_utc(WEDNESDAY, 9, 30)) is SessionPhase.RTH
    assert session_phase(et_utc(WEDNESDAY, 12, 0)) is SessionPhase.RTH
    assert session_phase(et_utc(WEDNESDAY, 16, 0)) is SessionPhase.OVERNIGHT
    assert session_phase(et_utc(WEDNESDAY, 17, 30)) is SessionPhase.MAINTENANCE
    assert session_phase(et_utc(WEDNESDAY, 18, 30)) is SessionPhase.OVERNIGHT


def test_rth_open_boundary_is_inclusive_and_close_exclusive():
    assert (
        session_phase(
            et_utc(
                WEDNESDAY,
                9,
                29,
            )
        )
        is SessionPhase.OVERNIGHT
    )
    assert session_phase(et_utc(WEDNESDAY, 9, 30)) is SessionPhase.RTH
    assert session_phase(et_utc(WEDNESDAY, 15, 59)) is SessionPhase.RTH
    assert session_phase(et_utc(WEDNESDAY, 16, 0)) is SessionPhase.OVERNIGHT


def test_weekend_is_closed():
    saturday = date(2026, 9, 19)
    sunday = date(2026, 9, 20)
    assert session_phase(et_utc(saturday, 12, 0)) is SessionPhase.CLOSED
    assert session_phase(et_utc(sunday, 15, 0)) is SessionPhase.CLOSED
    # Sunday 18:00 opens the Monday session.
    assert session_phase(et_utc(sunday, 18, 0)) is SessionPhase.OVERNIGHT


def test_friday_close_ends_the_week():
    friday = date(2026, 9, 18)
    assert session_phase(et_utc(friday, 16, 59)) is SessionPhase.OVERNIGHT
    assert session_phase(et_utc(friday, 17, 0)) is SessionPhase.CLOSED


def test_active_session_during_rth_and_overnight():
    during_rth = et_utc(WEDNESDAY, 11, 0)
    assert active_session(during_rth).trade_date == WEDNESDAY
    during_overnight = et_utc(WEDNESDAY, 20, 0)
    assert active_session(during_overnight).trade_date == THURSDAY
    assert active_session(during_overnight).opens_at == et_utc(WEDNESDAY, 18, 0)


def test_maintenance_break_returns_most_recent_completed_session():
    # Wednesday 17:30 is between sessions.
    moment = et_utc(WEDNESDAY, 17, 30)
    assert in_maintenance_break(moment)
    assert active_session(moment).trade_date == WEDNESDAY
    assert active_session(moment).closes_at <= moment


def test_previous_session_is_the_prior_trade_date():
    during_thursday = et_utc(THURSDAY, 11, 0)
    assert previous_session(during_thursday).trade_date == WEDNESDAY


def test_saturday_points_back_to_friday():
    saturday = date(2026, 9, 19)
    moment = et_utc(saturday, 12, 0)
    assert active_session(moment).trade_date == date(2026, 9, 18)


def test_dst_spring_forward_shifts_wall_clock_not_offset():
    # 2026-03-08 is the US spring-forward date. The session that trade date
    # opens the evening before (2026-03-07 18:00 ET) — that is still EST.
    dst_day = date(2026, 3, 9)  # Monday after the switch
    session = globex_session(dst_day)
    # RTH 09:30 ET on the DST Monday is EDT (UTC-4) => 13:30 UTC.
    assert session.rth_opens_at == datetime(2026, 3, 9, 13, 30, tzinfo=UTC)
    # The prior 18:00 ET open was the Sunday after the switch too.
    assert session.opens_at == datetime(2026, 3, 8, 22, 0, tzinfo=UTC)


def test_dst_fall_back_uses_standard_time():
    # 2026-11-01 is the US fall-back date. The Monday session's RTH is EST
    # (UTC-5) => 14:30 UTC.
    monday = date(2026, 11, 2)
    session = globex_session(monday)
    assert session.rth_opens_at == datetime(2026, 11, 2, 14, 30, tzinfo=UTC)
    assert session.opens_at == datetime(2026, 11, 1, 23, 0, tzinfo=UTC)


def test_session_phase_uses_wall_clock_across_dst_transition():
    # 01:30 local on fall-back day occurs twice; zoneinfo resolves it. That
    # instant is Sunday, so the market is closed for the weekend — the phase
    # must come from the local wall clock, not a fixed UTC offset.
    ambiguous = datetime(2026, 11, 1, 1, 30, tzinfo=NY).astimezone(UTC)
    assert session_phase(ambiguous) is SessionPhase.CLOSED
    # The following Monday RTH is EST (UTC-5): 09:30 ET = 14:30 UTC.
    monday_rth = datetime(2026, 11, 2, 9, 30, tzinfo=NY).astimezone(UTC)
    assert monday_rth == datetime(2026, 11, 2, 14, 30, tzinfo=UTC)
    assert session_phase(monday_rth) is SessionPhase.RTH


def test_previous_week_sessions_are_mon_to_fri():
    # Active Thursday 2026-09-17 => previous week is Sep 7-11, but Sep 7 2026 is
    # Labor Day, so the trading week is Tue-Fri.
    moment = et_utc(THURSDAY, 11, 0)
    monday, end_exclusive, sessions = previous_week_sessions(moment)
    assert monday == date(2026, 9, 7)
    assert end_exclusive == date(2026, 9, 12)
    assert [session.trade_date for session in sessions] == [
        date(2026, 9, 8),
        date(2026, 9, 9),
        date(2026, 9, 10),
        date(2026, 9, 11),
    ]


def test_session_range_skips_weekends():
    sessions = session_range(date(2026, 9, 11), date(2026, 9, 15))
    assert [session.trade_date for session in sessions] == [
        date(2026, 9, 11),
        date(2026, 9, 14),
    ]


def test_is_trading_weekday():
    assert is_trading_weekday(date(2026, 9, 17))
    assert not is_trading_weekday(date(2026, 9, 19))


def test_session_duration_is_23_hours():
    session = globex_session(WEDNESDAY)
    # 18:00 prior day to 17:00 next day = 23 hours when no DST change.
    assert session.closes_at - session.opens_at == timedelta(hours=23)


def test_naive_datetime_rejected():
    import pytest

    with pytest.raises(ValueError):
        session_phase(datetime(2026, 9, 16, 12, 0))


def test_next_open_weekend_and_daylight_saving():
    from core.domain.sessions import closed_market_delay, next_session_open

    friday_close = datetime(2026, 9, 18, 21, 0, tzinfo=UTC)
    sunday_open = datetime(2026, 9, 20, 22, 0, tzinfo=UTC)
    assert next_session_open(friday_close) == sunday_open
    assert closed_market_delay(sunday_open - timedelta(seconds=30)) == 30
    assert closed_market_delay(sunday_open) == 0
    assert next_session_open(datetime(2026, 10, 31, 12, tzinfo=UTC)) == datetime(
        2026, 11, 1, 23, tzinfo=UTC
    )


def test_next_open_holiday_halt():
    from core.domain.sessions import next_session_open

    assert next_session_open(datetime(2026, 9, 7, 18, tzinfo=UTC)) == datetime(
        2026, 9, 7, 22, tzinfo=UTC
    )


# --------------------------------------------------------------------------- segments


def _segments(day):
    return [(s.name.value, s.starts_at, s.ends_at) for s in session_segments(day)]


def test_segments_cover_the_session_in_order():
    def utc(*args):
        return datetime(*args, tzinfo=UTC)

    assert _segments(date(2026, 10, 22)) == [
        ("asia", utc(2026, 10, 21, 22), utc(2026, 10, 22, 7)),  # 18:00 ET → 08:00 London
        ("europe", utc(2026, 10, 22, 7), utc(2026, 10, 22, 12)),  # → 08:00 ET
        ("premarket", utc(2026, 10, 22, 12), utc(2026, 10, 22, 13, 30)),
        ("cash", utc(2026, 10, 22, 13, 30), utc(2026, 10, 22, 20)),
        ("after_hours", utc(2026, 10, 22, 20), utc(2026, 10, 22, 21)),
    ]


def test_europe_follows_londons_clock_in_the_week_the_clocks_disagree():
    # The UK leaves summer time on Oct 25 2026, the US on Nov 1: London's 08:00
    # is 04:00 ET that week instead of 03:00 ET.
    segments = dict((name, start) for name, start, _ in _segments(date(2026, 10, 27)))
    assert segments["europe"] == datetime(2026, 10, 27, 8, tzinfo=UTC)


def test_early_close_segments_end_at_13_15_et():
    segments = _segments(date(2026, 11, 27))
    assert segments[-2] == ("cash", datetime(2026, 11, 27, 14, 30, tzinfo=UTC),
                            datetime(2026, 11, 27, 18, tzinfo=UTC))
    assert segments[-1] == ("after_hours", datetime(2026, 11, 27, 18, tzinfo=UTC),
                            datetime(2026, 11, 27, 18, 15, tzinfo=UTC))


def test_volatility_index_hours():
    assert volatility_index_open(datetime(2026, 10, 22, 7, 15, tzinfo=UTC))  # 03:15 ET
    assert not volatility_index_open(datetime(2026, 10, 22, 7, 14, tzinfo=UTC))
    assert not volatility_index_open(datetime(2026, 10, 22, 20, 15, tzinfo=UTC))  # 16:15
    assert not volatility_index_open(datetime(2026, 11, 26, 15, 0, tzinfo=UTC))  # holiday
