"""How big NQ's moves normally are, so a move can be judged against normal.

Deterministic, from stored hourly OHLC buckets and the session engine:

* **Typical daily range** — the median high-minus-low of the last
  ``LOOKBACK_SESSIONS`` complete Globex sessions, in points and as a share of
  the session's last price. A session is complete when quotes cover at least
  ``MIN_COVERAGE`` of its hours from one futures contract; others (feed gaps,
  a contract roll) are skipped, never counted as a zero range.
* **Today's range so far** — the active session's high minus low.
* **Realised volatility** — the standard deviation of one-hour log returns
  inside those sessions (adjacent hours, same contract), annualised.
* **Large-move trigger** — a one-hour move of ``TRIGGER_SHARE`` of the typical
  daily range, kept between ``TRIGGER_MIN_PCT`` and ``TRIGGER_MAX_PCT``. With
  fewer than ``MIN_SESSIONS`` complete sessions there is no typical range and
  the caller's fixed threshold is used.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from math import log, sqrt
from statistics import median, stdev

from core.domain.sessions import GlobexSession, active_session, globex_session, previous_trade_date
from core.storage.base import OhlcBucket

OhlcFn = Callable[..., Sequence[OhlcBucket]]

LOOKBACK_SESSIONS = 20
MIN_SESSIONS = 10
MIN_COVERAGE = 0.8
#: Hourly returns needed before realised volatility is stated.
MIN_RETURNS = 100
#: Trading hours in a year: 252 sessions of about 23 hours.
HOURS_PER_YEAR = 252 * 23
TRIGGER_SHARE = 0.55
TRIGGER_MIN_PCT = 0.6
TRIGGER_MAX_PCT = 1.5

_HOUR = timedelta(hours=1)


@dataclass(frozen=True)
class MoveSize:
    """Normal move size for one symbol; ``None`` means not measurable."""

    trade_date: date
    sessions: int
    typical_range: float | None
    typical_range_pct: float | None
    realised_vol_pct: float | None
    today_range: float | None
    today_range_pct: float | None
    #: True while the active session is still trading.
    today_partial: bool

    def trigger_pct(self, fallback: float) -> float:
        """The one-hour move that counts as large, or ``fallback`` without history."""
        if self.typical_range_pct is None:
            return fallback
        return min(TRIGGER_MAX_PCT, max(TRIGGER_MIN_PCT,
                                        round(TRIGGER_SHARE * self.typical_range_pct, 2)))


def _lookback(session: GlobexSession) -> list[GlobexSession]:
    sessions: list[GlobexSession] = []
    day = session.trade_date
    for _ in range(LOOKBACK_SESSIONS):
        day = previous_trade_date(day)
        sessions.append(globex_session(day))
    return sessions


def _complete(session: GlobexSession, rows: list[OhlcBucket]) -> bool:
    hours = (session.closes_at - session.opens_at) / _HOUR
    contracts = {c for row in rows for c in row.contracts}
    return len(rows) >= MIN_COVERAGE * hours and len(contracts) <= 1


def _returns(rows: list[OhlcBucket]) -> list[float]:
    return [log(b.close / a.close) for a, b in zip(rows, rows[1:], strict=False)
            if b.start - a.start == _HOUR and a.close > 0 and b.close > 0]


def move_size(symbol: str, ohlc: OhlcFn, *, now: datetime) -> MoveSize:
    session = active_session(now)
    past = _lookback(session)
    rows = sorted(ohlc(symbol, start=past[-1].opens_at, end=min(now, session.closes_at),
                       bucket_seconds=3600), key=lambda b: b.start)
    by_session: dict[date, list[OhlcBucket]] = {}
    for row in rows:
        for s in (session, *past):
            if s.contains(row.start):
                by_session.setdefault(s.trade_date, []).append(row)
                break

    ranges: list[tuple[float, float]] = []
    returns: list[float] = []
    for s in past:
        bucket = by_session.get(s.trade_date, [])
        if not _complete(s, bucket):
            continue
        high, low, last = max(b.high for b in bucket), min(b.low for b in bucket), bucket[-1].close
        ranges.append((high - low, (high - low) / last * 100))
        returns.extend(_returns(bucket))

    typical = typical_pct = None
    if len(ranges) >= MIN_SESSIONS:
        typical = median(r for r, _ in ranges)
        typical_pct = median(p for _, p in ranges)
    vol = (stdev(returns) * sqrt(HOURS_PER_YEAR) * 100
           if len(ranges) >= MIN_SESSIONS and len(returns) >= MIN_RETURNS else None)

    today = by_session.get(session.trade_date, [])
    today_range = today_pct = None
    if today and len({c for b in today for c in b.contracts}) <= 1:
        today_range = max(b.high for b in today) - min(b.low for b in today)
        today_pct = today_range / today[-1].close * 100
    return MoveSize(
        trade_date=session.trade_date, sessions=len(ranges), typical_range=typical,
        typical_range_pct=typical_pct, realised_vol_pct=vol, today_range=today_range,
        today_range_pct=today_pct, today_partial=session.contains(now),
    )


__all__ = ["MoveSize", "move_size"]
