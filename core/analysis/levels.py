"""Key price levels for a futures symbol (NQ on Today and the Chart page).

Deterministic, from stored observations and the session engine:

* **Overnight high / low** — the highest and lowest price from the Globex open
  (18:00 ET the evening before) to the cash open (09:30 ET), or "so far" before
  09:30 ET.
* **Prior settle** — the previous session's settlement as reported by the quote
  source (the instrument's ``previous_close``); today's change is measured from it.
* **Prior day high / low** — the previous full Globex session (18:00–17:00 ET).
* **Prior week high / low** — every session of the previous Monday–Friday week.
* **Gap vs settle** — before the cash open, the latest price minus the prior
  settle; from 09:30 ET, the first price at or after the cash open minus it.

A level that cannot be measured (no stored quotes, or quotes from a different
futures contract after a roll) is ``None`` with the reason; never zero. The
explainers are rules of thumb, not advice.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime

from core.desk import explainers as X
from core.domain.chart import KeyLevel, KeyLevels
from core.domain.sessions import active_session, previous_session, previous_week_sessions
from core.storage.base import OhlcBucket

OhlcFn = Callable[..., Sequence[OhlcBucket]]

#: Bucket size for reading a window; levels are window-wide so the size only
#: changes how many rows are read.
_BUCKET_SECONDS = 3600

#: Explainer text per level, from the explainer library (``core/desk/explainers.py``).
EXPLAINERS: dict[str, str] = {e.name: e.full_text for e in X.LEVELS}

LABELS: dict[str, str] = {
    "overnight_high": "Overnight high",
    "overnight_low": "Overnight low",
    "prior_settle": "Prior settle",
    "prior_day_high": "Prior day high",
    "prior_day_low": "Prior day low",
    "prior_week_high": "Prior week high",
    "prior_week_low": "Prior week low",
    "gap_vs_settle": "Gap vs settle",
}


def _price(value: float | None) -> str:
    return "unavailable" if value is None else f"{value:,.2f}"


def _signed(value: float) -> str:
    return f"{'+' if value > 0 else '-' if value < 0 else ''}{abs(value):,.2f}"


class _Window:
    """High, low, first and last of a window, or why they are unavailable."""

    def __init__(self, buckets: Sequence[OhlcBucket], contract: str | None) -> None:
        rows = sorted(buckets, key=lambda b: b.start)
        self.reason: str | None = None
        self.high = self.low = self.first = self.last = None
        if not rows:
            self.reason = "no stored quotes for this window"
            return
        contracts = {c for b in rows for c in b.contracts}
        if contract and contracts and contracts != {contract}:
            others = ", ".join(sorted(contracts - {contract})) or "another contract"
            self.reason = f"quotes in this window are from {others} (the contract has rolled)"
            return
        self.high = max(b.high for b in rows)
        self.low = min(b.low for b in rows)
        self.first = rows[0].open
        self.last = rows[-1].close


def _level(key: str, value: float | None, *, start: datetime | None = None,
           end: datetime | None = None, partial: bool = False,
           reason: str | None = None, text: str | None = None) -> KeyLevel:
    return KeyLevel(
        key=key, label=LABELS[key], value=value,
        value_text=text if text is not None and value is not None else _price(value),
        explainer=EXPLAINERS[key], window_start=start, window_end=end, partial=partial,
        unavailable_reason=None if value is not None else (reason or "not available"),
    )


def key_levels(symbol: str, instrument, ohlc: OhlcFn, *, now: datetime) -> KeyLevels:
    """Key levels for ``symbol``; ``instrument`` is its current market state (or None)."""
    contract = getattr(instrument, "contract_code", None)
    settle = getattr(instrument, "previous_close", None)

    def read(start: datetime, end: datetime) -> _Window:
        if end <= start:
            return _Window([], contract)
        return _Window(ohlc(symbol, start=start, end=end, bucket_seconds=_BUCKET_SECONDS),
                       contract)

    session = active_session(now)
    overnight_end = min(now, session.rth_opens_at)
    overnight = read(session.opens_at, overnight_end)
    partial = now < session.rth_opens_at
    prior = previous_session(now)
    day = read(prior.opens_at, prior.closes_at)
    _, _, week_sessions = previous_week_sessions(now)
    week_start = week_sessions[0].opens_at if week_sessions else None
    week_end = week_sessions[-1].closes_at if week_sessions else None
    week = (read(week_start, week_end) if week_sessions
            else _Window([], contract))
    if not week_sessions:
        week.reason = "no trading sessions last week"

    levels = [
        _level("overnight_high", overnight.high, start=session.opens_at, end=overnight_end,
               partial=partial, reason=overnight.reason),
        _level("overnight_low", overnight.low, start=session.opens_at, end=overnight_end,
               partial=partial, reason=overnight.reason),
        _level("prior_settle", settle, reason="the quote source has not sent a previous settle"),
        _level("prior_day_high", day.high, start=prior.opens_at, end=prior.closes_at,
               reason=day.reason),
        _level("prior_day_low", day.low, start=prior.opens_at, end=prior.closes_at,
               reason=day.reason),
        _level("prior_week_high", week.high, start=week_start, end=week_end,
               reason=week.reason),
        _level("prior_week_low", week.low, start=week_start, end=week_end,
               reason=week.reason),
    ]

    # Gap: the cash-open price once it exists, otherwise where it would open now.
    if now >= session.rth_opens_at:
        opening = read(session.rth_opens_at, min(now, session.rth_closes_at))
        price, reason = opening.first, opening.reason
        gap_start, gap_end, gap_partial = session.rth_opens_at, session.rth_opens_at, False
    else:
        price = getattr(instrument, "value", None)
        reason = "no current price"
        gap_start, gap_end, gap_partial = None, now, True
    gap = text = None
    if settle is None:
        reason = "no prior settle"
    elif price is not None:
        gap = round(price - settle, 2)
        text = f"{_signed(gap)} ({_signed(round(gap / settle * 100, 2))}%)"
    levels.append(_level("gap_vs_settle", gap, start=gap_start, end=gap_end,
                         partial=gap_partial, reason=reason, text=text))

    return KeyLevels(
        symbol=symbol, generated_at=now, trade_date=session.trade_date, contract=contract,
        delayed=getattr(getattr(instrument, "feed_status", None), "value",
                        getattr(instrument, "feed_status", None)) == "delayed",
        levels=levels,
    )


__all__ = ["EXPLAINERS", "LABELS", "key_levels"]
