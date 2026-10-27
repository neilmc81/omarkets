"""Normal move size: typical range, realised volatility, scaled move trigger."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import log, sqrt
from statistics import stdev
from types import SimpleNamespace

import pytest

from core.analysis import move_size as MS
from core.domain.sessions import globex_session, previous_trade_date

NOW = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)  # Thu 11:00 ET, mid-session
HOUR = timedelta(hours=1)


def bucket(start, close, *, high=None, low=None, contract="NQZ26"):
    return SimpleNamespace(start=start, open=close, close=close, high=high or close,
                           low=low or close, contracts=(contract,))


def session_rows(day, *, base=20_000.0, swing=200.0, hours=None, contract="NQZ26"):
    """Hourly buckets for one session alternating around ``base`` (range = 2 x swing)."""
    session = globex_session(day)
    count = hours if hours is not None else int((session.closes_at - session.opens_at) / HOUR)
    return [bucket(session.opens_at + i * HOUR, base + (swing if i % 2 else -swing),
                   contract=contract) for i in range(count)]


def past_days(n, start=None):
    day = (start or globex_session(datetime(2026, 10, 22).date())).trade_date
    days = []
    for _ in range(n):
        day = previous_trade_date(day)
        days.append(day)
    return days


def ohlc_of(rows):
    def ohlc(symbol, *, start, end, bucket_seconds, within=()):
        assert bucket_seconds == 3600
        return [r for r in rows if start <= r.start < end
                and (not within or any(a <= r.start < b for a, b in within))]
    return ohlc


def test_typical_range_is_the_median_of_complete_sessions():
    rows = []
    for i, day in enumerate(past_days(20)):
        rows += session_rows(day, swing=100.0 + 10 * i)
    size = MS.move_size("NQ", ohlc_of(rows), now=NOW)
    assert size.sessions == 20
    swings = sorted(2 * (100.0 + 10 * i) for i in range(20))
    assert size.typical_range == pytest.approx((swings[9] + swings[10]) / 2)
    assert size.typical_range_pct == pytest.approx(size.typical_range / 20_100.0 * 100, rel=0.02)


def test_gappy_and_rolled_sessions_are_skipped_not_zero():
    days = past_days(20)
    rows = []
    for day in days[:12]:
        rows += session_rows(day)
    rows += session_rows(days[12], hours=5)              # feed gap
    rolled = session_rows(days[13])
    rolled[-1] = bucket(rolled[-1].start, 20_000.0, contract="NQH27")
    rows += rolled                                        # contract roll mid-session
    size = MS.move_size("NQ", ohlc_of(rows), now=NOW)
    assert size.sessions == 12
    assert size.typical_range == pytest.approx(400.0)


def test_too_little_history_gives_no_typical_range_and_the_fixed_trigger():
    rows = [r for day in past_days(MS.MIN_SESSIONS - 1) for r in session_rows(day)]
    size = MS.move_size("NQ", ohlc_of(rows), now=NOW)
    assert size.typical_range is None and size.typical_range_pct is None
    assert size.realised_vol_pct is None
    assert size.trigger_pct(0.8) == 0.8


def test_trigger_scales_and_is_clamped():
    def with_pct(pct):
        return MS.MoveSize(trade_date=NOW.date(), sessions=20, typical_range=1.0,
                           typical_range_pct=pct, realised_vol_pct=None, today_range=None,
                           today_range_pct=None, today_partial=True)
    assert with_pct(1.46).trigger_pct(0.8) == 0.8         # 0.55 x 1.46 = 0.803
    assert with_pct(2.0).trigger_pct(0.8) == 1.1
    assert with_pct(0.5).trigger_pct(0.8) == MS.TRIGGER_MIN_PCT
    assert with_pct(4.0).trigger_pct(0.8) == MS.TRIGGER_MAX_PCT


def test_realised_volatility_from_adjacent_hours_annualised():
    rows = [r for day in past_days(20) for r in session_rows(day)]
    size = MS.move_size("NQ", ohlc_of(rows), now=NOW)
    up, down = log(20_200 / 19_800), log(19_800 / 20_200)
    per_session = [up if i % 2 else down for i in range(1, 23)]
    expected = stdev(per_session * 20) * sqrt(MS.HOURS_PER_YEAR) * 100
    assert size.realised_vol_pct == pytest.approx(expected)


def test_hours_across_a_gap_are_not_one_hour_returns():
    rows = session_rows(past_days(1)[0])
    del rows[5]
    assert len(MS._returns(rows)) == len(rows) - 2


def test_today_range_so_far_and_after_the_close():
    today = globex_session(NOW.date())
    rows = [r for day in past_days(20) for r in session_rows(day)]
    rows += [bucket(today.opens_at, 20_000.0, high=20_050.0, low=19_900.0),
             bucket(today.opens_at + HOUR, 20_100.0)]
    size = MS.move_size("NQ", ohlc_of(rows), now=NOW)
    assert size.today_range == 200.0 and size.today_partial is True
    after = MS.move_size("NQ", ohlc_of(rows), now=today.closes_at + timedelta(minutes=30))
    assert after.today_partial is False


def test_no_quotes_today_is_unavailable():
    rows = [r for day in past_days(20) for r in session_rows(day)]
    size = MS.move_size("NQ", ohlc_of(rows), now=NOW)
    assert size.today_range is None and size.today_range_pct is None


def test_quotes_after_an_early_close_are_not_read(memory_repo):
    from tests.analysis.test_levels import obs

    # Fri Nov 27 2026 closes at 13:15 ET; a 13:30 ET quote must not count.
    early = globex_session(datetime(2026, 11, 27).date())
    assert early.closes_at == datetime(2026, 11, 27, 18, 15, tzinfo=UTC)
    memory_repo.store([obs(early.closes_at - timedelta(minutes=30), 20_000.0),
                       obs(early.closes_at + timedelta(minutes=15), 22_000.0)])
    seen = []

    def ohlc(*args, **kwargs):
        rows = memory_repo.ohlc(*args, **kwargs)
        seen.extend(rows)
        return rows

    MS.move_size("NQ", ohlc, now=datetime(2026, 11, 30, 15, 0, tzinfo=UTC))
    assert [r.high for r in seen] == [20_000.0]


def test_labor_day_halt_is_not_counted_as_missing_hours():
    from core.domain.sessions import trading_windows

    labor = globex_session(datetime(2026, 9, 8).date())
    windows = trading_windows(labor)
    hours = sum((b - a) / HOUR for a, b in windows)
    assert len(windows) == 2 and hours == 42
    rows = [bucket(a + i * HOUR, 20_000.0) for a, b in windows
            for i in range(int((b - a) / HOUR))][:34]          # 34 of 42 hours: 81%
    assert MS._complete(labor, rows, labor.closes_at) is True


def test_a_finished_session_with_missing_quotes_is_marked_incomplete():
    today = globex_session(NOW.date())
    rows = [r for day in past_days(20) for r in session_rows(day)]
    rows += [bucket(today.opens_at, 20_000.0), bucket(today.opens_at + HOUR, 20_100.0)]
    after = MS.move_size("NQ", ohlc_of(rows), now=today.closes_at + timedelta(minutes=30))
    assert after.today_partial is False and after.today_complete is False
