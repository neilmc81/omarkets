"""Chart bars from stored observations: gaps, ordering, sessions, DST, holidays, 1M."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from core.analysis import bars as B
from core.domain.chart import ChartRange, Interval
from core.domain.models import MarketObservation


def obs(ts: datetime, value: float, *, received: timedelta = timedelta(minutes=10),
        feed: str = "delayed", contract: str = "NQZ26", source: str = "cnbc",
        symbol: str = "NQ") -> MarketObservation:
    return MarketObservation(symbol=symbol, timestamp=ts, value=value, unit="index points",
                             source=source, received_at=ts + received,
                             metadata={"feed_status": feed, "contract_code": contract})


def at(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def read(repo, chart_range: ChartRange, interval: Interval, now: datetime, symbol="NQ"):
    sessions = B.range_sessions(chart_range, now)
    _, end = B.window(sessions, now)
    built, _ = B.read_bars(repo.ohlc, symbol, sessions, interval, end=end)
    return sessions, built


# Thursday Oct 22 2026 session: opens Wed 18:00 EDT (22:00 UTC), closes Thu 21:00 UTC.
NOW = at(2026, 10, 22, 15, 0)


def test_ohlc_uses_exchange_time_order_not_arrival_order(memory_repo):
    t = at(2026, 10, 22, 13, 30)
    # Stored out of order: the 13:34 quote arrives before the 13:30 one.
    memory_repo.store([obs(t + timedelta(minutes=4), 90), obs(t + timedelta(minutes=2), 105)])
    memory_repo.store([obs(t, 100), obs(t + timedelta(minutes=1), 110)])
    [bar] = memory_repo.ohlc("NQ", start=t, end=t + timedelta(minutes=5), bucket_seconds=300)
    assert (bar.open, bar.high, bar.low, bar.close, bar.count) == (100, 110, 90, 90, 4)
    assert bar.first_at == t and bar.last_at == t + timedelta(minutes=4)
    assert bar.sources == ("cnbc",) and bar.contracts == ("NQZ26",) and bar.delayed


def test_a_revised_quote_for_the_same_exchange_time_replaces_the_first(memory_repo):
    t = at(2026, 10, 22, 13, 30)
    memory_repo.store([obs(t, 100), obs(t + timedelta(minutes=1), 101)])
    memory_repo.store([obs(t + timedelta(minutes=1), 120, received=timedelta(minutes=11))])
    [bar] = memory_repo.ohlc("NQ", start=t, end=t + timedelta(minutes=5), bucket_seconds=300)
    assert bar.count == 2 and bar.high == 120 and bar.close == 120


def test_a_quote_stamped_after_it_was_received_is_left_out(memory_repo):
    # A quote stamped seven hours after it arrived cannot be placed in time.
    t = at(2026, 10, 22, 5, 0)
    memory_repo.store([obs(t, 29_142.0, received=-timedelta(hours=7)),
                       obs(t + timedelta(seconds=15), 29_616.0)])
    [bar] = memory_repo.ohlc("NQ", start=t, end=t + timedelta(minutes=5), bucket_seconds=300)
    assert (bar.open, bar.low, bar.count) == (29_616.0, 29_616.0, 1)


def test_a_quote_received_hours_after_its_exchange_time_is_left_out(memory_repo):
    # Seen live: at 18:00 ET CNBC re-sends the settle stamped 01:00 ET that day.
    t = at(2026, 10, 22, 5, 0)
    memory_repo.store([obs(t, 29_142.0, received=timedelta(hours=17)),
                       obs(t + timedelta(seconds=15), 29_616.0)])
    [bar] = memory_repo.ohlc("NQ", start=t, end=t + timedelta(minutes=5), bucket_seconds=300)
    assert (bar.open, bar.low, bar.count) == (29_616.0, 29_616.0, 1)


def test_gaps_inside_a_session_stay_gaps(memory_repo):
    t = at(2026, 10, 22, 13, 30)
    memory_repo.store([obs(t, 100), obs(t + timedelta(minutes=17), 104)])
    _, built = read(memory_repo, ChartRange.DAY, Interval.M5, NOW)
    assert [b.time for b in built] == [t + timedelta(minutes=m) for m in (0, 5, 10, 15)]
    assert [b.count for b in built] == [1, 0, 0, 1]
    gap = built[1]
    assert gap.open is None and gap.close is None and gap.change_pct is None
    # Nothing is invented before the first quote or after the latest one.
    assert built[0].close == 100 and built[-1].close == 104


def test_change_pct_is_measured_from_the_first_bar_open(memory_repo):
    t = at(2026, 10, 22, 13, 30)
    memory_repo.store([obs(t, 100), obs(t + timedelta(minutes=5), 102)])
    _, built = read(memory_repo, ChartRange.DAY, Interval.M5, NOW)
    assert [b.change_pct for b in built] == [0.0, 2.0]


def test_maintenance_break_quotes_are_left_out(memory_repo):
    # Wed Oct 21: Globex closes 17:00 EDT (21:00 UTC), reopens 18:00 EDT.
    memory_repo.store([
        obs(at(2026, 10, 21, 20, 55), 100),  # Wed session, last bar
        obs(at(2026, 10, 21, 21, 30), 999),  # maintenance break
        obs(at(2026, 10, 21, 22, 0), 101),  # Thu session opens
    ])
    sessions, built = read(memory_repo, ChartRange.FIVE_DAYS, Interval.H1, NOW)
    assert all(b.close != 999 for b in built)
    assert [b.time for b in built] == [at(2026, 10, 21, 20), at(2026, 10, 21, 22)]
    assert sessions[-1].opens_at == at(2026, 10, 21, 22)


def test_early_close_session_ends_at_13_15_et(memory_repo):
    # Fri Nov 27 2026 (after Thanksgiving): Globex closes 13:15 EST = 18:15 UTC.
    now = at(2026, 11, 27, 23, 0)
    memory_repo.store([obs(at(2026, 11, 27, 18, 10), 100), obs(at(2026, 11, 27, 18, 40), 999)])
    sessions, built = read(memory_repo, ChartRange.DAY, Interval.H1, now)
    assert sessions[0].closes_at == at(2026, 11, 27, 18, 15)
    assert [(b.time, b.close) for b in built] == [(at(2026, 11, 27, 18), 100)]


def test_dst_change_moves_the_globex_open_by_an_hour_in_utc(memory_repo):
    # DST ends Sun Nov 1 2026: the Monday session opens 18:00 EST = 23:00 UTC,
    # while Friday's opened Thursday 18:00 EDT = 22:00 UTC.
    now = at(2026, 11, 2, 15, 0)
    memory_repo.store([
        obs(at(2026, 10, 29, 22, 0), 100),
        obs(at(2026, 11, 1, 22, 30), 999),  # 17:30 EST Sunday: still closed
        obs(at(2026, 11, 1, 23, 0), 101),
    ])
    sessions, built = read(memory_repo, ChartRange.FIVE_DAYS, Interval.H1, now)
    opens = {s.trade_date: s.opens_at for s in sessions}
    assert opens[date(2026, 10, 30)] == at(2026, 10, 29, 22)
    assert opens[date(2026, 11, 2)] == at(2026, 11, 1, 23)
    assert [b.close for b in built] == [100, 101]


def test_holidays_are_skipped_and_their_quotes_left_out(memory_repo):
    # Thanksgiving (Thu Nov 26 2026) is not a session in the exchange calendar.
    now = at(2026, 11, 30, 15, 0)
    memory_repo.store([obs(at(2026, 11, 26, 15, 0), 999), obs(at(2026, 11, 25, 15, 0), 100)])
    sessions, built = read(memory_repo, ChartRange.FIVE_DAYS, Interval.H1, now)
    assert [s.trade_date.day for s in sessions] == [23, 24, 25, 27, 30]
    assert [b.close for b in built] == [100]


def test_one_day_range_during_the_weekend_is_fridays_session():
    sessions = B.range_sessions(ChartRange.DAY, at(2026, 10, 24, 12, 0))
    assert [s.trade_date for s in sessions] == [date(2026, 10, 23)]


def test_month_range_covers_30_days_of_trading_sessions():
    sessions = B.range_sessions(ChartRange.MONTH, NOW)
    assert sessions[-1].trade_date == date(2026, 10, 22)
    assert sessions[0].trade_date > date(2026, 9, 22)
    assert len(sessions) == 22  # weekdays Sep 23 – Oct 22, no holidays


def test_month_range_offers_15_minute_and_hourly_bars_only():
    with pytest.raises(B.RangeError, match="15m, 1h"):
        B.check_interval(ChartRange.MONTH, Interval.M5)
    B.check_interval(ChartRange.MONTH, Interval.M15)
    B.check_interval(ChartRange.DAY, Interval.M5)


def test_month_range_reads_the_thinned_one_point_a_minute_history(memory_repo):
    # Five quotes a minute, 40 days ago; compaction keeps the last per minute.
    old = at(2026, 9, 14, 14, 0)
    memory_repo.store([obs(old + timedelta(seconds=10 * i), 100 + i) for i in range(12)])
    removed = memory_repo.compact(now=NOW)
    assert removed == 10  # 12 quotes over two minutes -> 2 kept
    sessions = B.range_sessions(ChartRange.MONTH, at(2026, 10, 12, 15, 0))
    assert sessions[0].trade_date <= date(2026, 9, 14)
    _, end = B.window(sessions, at(2026, 10, 12, 15, 0))
    built, _ = B.read_bars(memory_repo.ohlc, "NQ", sessions, Interval.M15, end=end)
    [bar] = built
    # Only the last quote of each minute survives: 105 (14:00) and 111 (14:01).
    assert (bar.open, bar.high, bar.low, bar.close, bar.count) == (105, 111, 105, 111, 2)


def test_sparkline_marks_missing_buckets():
    t = at(2026, 10, 22, 13, 0)
    from core.storage.base import OhlcBucket

    def bucket(start, close):
        return OhlcBucket(start=start, open=close, high=close, low=close, close=close, count=1,
                          first_at=start, last_at=start, sources=("cnbc",), contracts=(),
                          delayed=False)

    line = B.sparkline("NQ", [bucket(t + timedelta(minutes=30), 2), bucket(t, 1)], Interval.M15)
    assert [p.value for p in line.points] == [1, None, 2]
