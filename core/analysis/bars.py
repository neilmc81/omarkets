"""OHLC bars for the chart, built deterministically from stored observations.

Rules:

* A range is a set of whole sessions from the instrument's trading calendar
  (:mod:`core.domain.calendars`; CME Globex unless the instrument says
  otherwise): ``1d`` is the active (or most recently completed) session, ``5d``
  the last five trading sessions, ``1m`` the trading sessions of the last 30
  calendar days. On CME, weekends, holidays and the daily maintenance break are
  not part of any session, so they never get bars. On the 24×7 calendar every
  UTC day is a session.
* Bars are aligned to the interval in UTC. ET is a whole number of hours from
  UTC, so 5-, 15- and 60-minute bars also start on whole ET minutes and hours.
* A bar exists only where observations exist. Between a session's first and
  last observed bar, a missing bar is returned as a gap (``count == 0``, no
  prices), never filled in. Observations outside every session are left out
  and never mixed into a bar: each session is read with its exact bounds.
* ``1m`` allows 15-minute and hourly bars only. Observations older than the
  storage's fine retention (30 days) are thinned to one per minute, so bars
  before that point can miss brief spikes inside a minute; the response says so.
* Source, contract and the delayed flag are kept per bar.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

from core.desk import explainers as X
from core.domain.calendars import CME_GLOBEX, SessionCalendar
from core.domain.chart import (
    INTERVAL_SECONDS,
    Bar,
    ChartRange,
    ChartSession,
    Interval,
    Sparkline,
    SparklinePoint,
)
from core.domain.sessions import TradingSession
from core.storage.base import OhlcBucket

OhlcFn = Callable[..., Sequence[OhlcBucket]]

#: Intervals allowed per range. A month of 5-minute bars is not offered.
ALLOWED_INTERVALS: dict[ChartRange, frozenset[Interval]] = {
    ChartRange.DAY: frozenset(Interval),
    ChartRange.FIVE_DAYS: frozenset(Interval),
    ChartRange.MONTH: frozenset({Interval.M15, Interval.H1}),
}
FIVE_DAY_SESSIONS = 5
MONTH_DAYS = 30


class RangeError(ValueError):
    """An interval that the range does not allow."""


def range_sessions(chart_range: ChartRange, now: datetime,
                   calendar: SessionCalendar = CME_GLOBEX) -> list[TradingSession]:
    """The trading sessions a range covers on ``calendar``, oldest first."""
    latest = calendar.active_session(now)
    if chart_range is ChartRange.DAY:
        return [latest]
    if chart_range is ChartRange.FIVE_DAYS:
        dates = [latest.trade_date]
        while len(dates) < FIVE_DAY_SESSIONS:
            dates.append(calendar.previous_trade_date(dates[-1]))
        return [calendar.session(day) for day in reversed(dates)]
    earliest = latest.trade_date - timedelta(days=MONTH_DAYS)
    dates = [latest.trade_date]
    while (before := calendar.previous_trade_date(dates[-1])) > earliest:
        dates.append(before)
    return [calendar.session(day) for day in reversed(dates)]


def chart_session(session: TradingSession,
                  calendar: SessionCalendar = CME_GLOBEX) -> ChartSession:
    return ChartSession(
        trade_date=session.trade_date,
        opens_at=session.opens_at,
        closes_at=session.closes_at,
        rth_opens_at=session.rth_opens_at,
        rth_closes_at=session.rth_closes_at,
        early_close=calendar.early_close(session.trade_date),
        segments=[s.model_copy(update={"explainer": X.segment(s.name).text})
                  for s in calendar.segments(session.trade_date)],
    )


def window(sessions: Sequence[TradingSession], now: datetime) -> tuple[datetime, datetime]:
    """The chart window: the first session's open to now or the last close."""
    return sessions[0].opens_at, min(now.astimezone(UTC), sessions[-1].closes_at)


def check_interval(chart_range: ChartRange, interval: Interval) -> None:
    if interval not in ALLOWED_INTERVALS[chart_range]:
        allowed = ", ".join(sorted(i.value for i in ALLOWED_INTERVALS[chart_range]))
        raise RangeError(f"the {chart_range.value} range allows {allowed} bars")


def _bar(bucket: OhlcBucket, base: float | None) -> Bar:
    change = None
    if base:
        change = round((bucket.close - base) / base * 100.0, 4)
    return Bar(
        time=bucket.start, open=bucket.open, high=bucket.high, low=bucket.low,
        close=bucket.close, count=bucket.count, change_pct=change, delayed=bucket.delayed,
        source="+".join(bucket.sources) or None, contract=",".join(bucket.contracts) or None,
    )


def _gap(start: datetime) -> Bar:
    return Bar(time=start, open=None, high=None, low=None, close=None, count=0,
               change_pct=None, delayed=False, source=None, contract=None)


def read_bars(
    ohlc: OhlcFn,
    symbol: str,
    sessions: Sequence[TradingSession],
    interval: Interval,
    *,
    end: datetime,
) -> tuple[list[Bar], list[OhlcBucket]]:
    """Bars for the sessions, read with each session's exact bounds.

    Only quotes inside a session (and before ``end``) are read, so a bar never
    mixes in quotes from the maintenance break, a weekend or the time after an
    early close. Returns the bars and the raw buckets.
    """
    within = [(s.opens_at, min(s.closes_at, end)) for s in sessions if s.opens_at < end]
    if not within:
        return [], []
    buckets = list(ohlc(symbol, start=within[0][0], end=within[-1][1],
                        bucket_seconds=INTERVAL_SECONDS[interval], within=within))
    # Every bucket starts inside its session: sessions open on whole hours.
    per_session = [[b for b in buckets if s.opens_at <= b.start < s.closes_at]
                   for s in sessions]
    return build_bars(per_session, interval), buckets


def build_bars(per_session: Sequence[Sequence[OhlcBucket]], interval: Interval) -> list[Bar]:
    """Bars in time order, with a gap bar for each missing bucket inside a session.

    Gaps are only marked between a session's first and last observed bucket;
    nothing is added before the first quote, after the latest one, or between
    sessions.
    """
    step = timedelta(seconds=INTERVAL_SECONDS[interval])
    first = next((b for group in per_session for b in sorted(group, key=lambda b: b.start)),
                 None)
    base = first.open if first is not None else None
    bars: list[Bar] = []
    for group in per_session:
        expected: datetime | None = None
        for bucket in sorted(group, key=lambda b: b.start):
            while expected is not None and expected < bucket.start:
                bars.append(_gap(expected))
                expected += step
            bars.append(_bar(bucket, base))
            expected = bucket.start + step
    return bars


def sparkline(symbol: str, buckets: Sequence[OhlcBucket], interval: Interval) -> Sparkline:
    """Bucket closes, oldest first, with ``None`` for a missing bucket in between."""
    step = timedelta(seconds=INTERVAL_SECONDS[interval])
    points: list[SparklinePoint] = []
    expected: datetime | None = None
    for bucket in sorted(buckets, key=lambda b: b.start):
        while expected is not None and expected < bucket.start:
            points.append(SparklinePoint(time=expected, value=None))
            expected += step
        points.append(SparklinePoint(time=bucket.start, value=bucket.close))
        expected = bucket.start + step
    return Sparkline(symbol=symbol, points=points)


__all__ = [
    "ALLOWED_INTERVALS",
    "RangeError",
    "build_bars",
    "read_bars",
    "chart_session",
    "check_interval",
    "range_sessions",
    "sparkline",
    "window",
]
