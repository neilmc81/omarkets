"""The playbook: how NQ reacted when each indicator beat, missed or matched its forecast.

Deterministic and model-free, recomputed from stored releases and quotes. For
every past release figure with an actual and a forecast it measures, from the
release time:

- NQ and the 2-year yield after 15 minutes (the same window as a release note);
- NQ by the next cash close, and whether the first reaction held (the same
  wording rules as "Did it hold?").

It then counts, per indicator (matched by figure name, like surprise sizing),
how often NQ rose or fell after an above-, below- or in-line print. Several
figures released at the same moment share one market reaction; each release
row lists what else came out with it.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from core.desk import facts as F
from core.desk.followup import FEED_CATCH_UP, FLAT_PCT, MAX_QUOTE_GAP, cash_close_after, classify
from core.desk.links import ReleaseNotes
from core.desk.models import (
    Move,
    Playbook,
    PlaybookBucket,
    PlaybookIndicator,
    PlaybookRelease,
)

#: How far back releases are read. Quotes are only stored since September 2026.
LOOKBACK = timedelta(days=365)
REACTION = timedelta(minutes=15)
#: Fewer releases than this are shown, but not described as a pattern.
MIN_RELEASES = 5
VERDICT_WORDS = {"above": "Above forecast", "below": "Below forecast", "in_line": "In line"}


def _nq(moves: list[Move], symbol: str, at: datetime) -> Move | None:
    # A quote from well before the checkpoint would misstate the move.
    return next((m for m in moves if m.symbol == symbol and at - m.end_at <= MAX_QUOTE_GAP),
                None)


def _direction(change: float) -> str:
    return "up" if change >= FLAT_PCT else "down" if change <= -FLAT_PCT else "flat"


def _counts(parts: list[tuple[str, int]]) -> str:
    return ", ".join(f"{word} {n}" for word, n in parts if n)


def _bucket(verdict: str, rows: list[PlaybookRelease]) -> PlaybookBucket:
    first = [_direction(r.nq_15m.change) for r in rows if r.nq_15m is not None]
    close = [_direction(r.nq_close.change) for r in rows if r.nq_close is not None]
    bucket = {
        "first_up": first.count("up"), "first_down": first.count("down"),
        "first_flat": first.count("flat"),
        "close_up": close.count("up"), "close_down": close.count("down"),
        "close_flat": close.count("flat"), "close_missing": len(rows) - len(close),
    }
    times = "once" if len(rows) == 1 else f"{len(rows)} times"
    text = (f"{VERDICT_WORDS[verdict]} {times}. First 15 min: NQ "
            + _counts([("rose", bucket["first_up"]), ("fell", bucket["first_down"]),
                       ("barely moved", bucket["first_flat"])]) + ".")
    by_close = _counts([("rose", bucket["close_up"]), ("fell", bucket["close_down"]),
                        ("about flat", bucket["close_flat"]),
                        ("not measured yet", bucket["close_missing"])])
    text += f" By the close: {by_close}."
    moves = [r.nq_15m.change for r in rows if r.nq_15m is not None]
    average = round(sum(moves) / len(moves), 2) if moves else None
    return PlaybookBucket(verdict=verdict, count=len(rows), text=text, nq_15m_avg=average,
                          **bucket)


def measure_release(slot: F.ReleaseSlot, history: F.HistoryFn, *, now: datetime,
                    ) -> tuple[Move | None, Move | None, Move | None, bool]:
    """NQ and 2-year after 15 minutes, NQ by the close, and whether the close is pending."""
    at = slot.at
    end = at + REACTION
    first = F.measure_moves(history, start=at, end=end, symbols=("NQ", "US2Y"))
    close_at = cash_close_after(at + timedelta(hours=1))
    pending = now < close_at + FEED_CATCH_UP
    nq_close = None
    if not pending:
        moves = F.measure_moves(history, start=at, end=close_at, symbols=("NQ",))
        nq_close = _nq(moves, "NQ", close_at)
    return _nq(first, "NQ", end), _nq(first, "US2Y", end), nq_close, pending


def build(events: Iterable[Any], history: F.HistoryFn, *, now: datetime,
          notes: ReleaseNotes | None = None) -> Playbook:
    """Every indicator's past releases with a measured NQ reaction, newest indicator first.

    ``notes`` links each release to its desk note, when one was written.
    """
    grouped: dict[str, list[PlaybookRelease]] = {}
    names: dict[str, str] = {}
    for slot in F.release_slots(events, start=now - LOOKBACK, end=now - REACTION - FEED_CATCH_UP,
                                min_importance="medium"):
        if slot.speakers_only:
            continue
        scored = [f for f in slot.figures if f.verdict in ("above", "below", "in_line")]
        if not scored:
            continue
        nq_15m, us2y_15m, nq_close, pending = measure_release(slot, history, now=now)
        if nq_15m is None:  # no stored quotes around this release
            continue
        held = classify(nq_15m.change, nq_close.change) if nq_close is not None else None
        note_id = notes.for_slot(slot.at, slot.codes) if notes is not None else None
        for figure in scored:
            surprise = F.surprise_value(figure)
            key = figure.name.casefold()
            names.setdefault(key, figure.name)
            grouped.setdefault(key, []).append(PlaybookRelease(
                at=slot.at, title=slot.title, figure=figure,
                alongside=[f.name for f in slot.figures if f is not figure],
                nq_15m=nq_15m, us2y_15m=us2y_15m, nq_close=nq_close,
                close_pending=pending, held=held, note_id=note_id, surprise=surprise,
                surprise_text=(F.surprise_text(surprise, figure.unit, figure.decimals)
                               if surprise is not None else None),
            ))

    indicators: list[PlaybookIndicator] = []
    for key, rows in grouped.items():
        rows.sort(key=lambda r: r.at, reverse=True)
        buckets = [_bucket(v, [r for r in rows if r.figure.verdict == v])
                   for v in ("above", "below", "in_line")
                   if any(r.figure.verdict == v for r in rows)]
        count = len(rows)
        enough = count >= MIN_RELEASES
        plural = "release" if count == 1 else "releases"
        text = (f"{count} {plural} measured." if enough else
                f"Only {count} {plural} measured so far: too few to call it a pattern "
                f"(that needs at least {MIN_RELEASES}). Treat these as examples.")
        indicators.append(PlaybookIndicator(name=names[key], count=count, enough=enough,
                                            text=text, buckets=buckets, releases=rows))
    indicators.sort(key=lambda i: i.releases[0].at, reverse=True)
    since = min((i.releases[-1].at for i in indicators), default=None)
    return Playbook(generated_at=now, since=since, min_releases=MIN_RELEASES,
                    indicators=indicators)


__all__ = ["LOOKBACK", "MIN_RELEASES", "build", "measure_release"]
