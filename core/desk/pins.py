"""Chart pins and the next high-impact event. Deterministic; no AI.

A pin is a scheduled release moment (from :func:`core.desk.facts.release_slots`)
of medium importance or more, shown on the chart at its release time. Released
pins carry the headline result (higher / lower than forecast), NQ's measured
move in the first 15 minutes and the desk note written for it, if any. Fed
speeches are left out: they are not releases and rarely have one clear moment.

The next high-impact event is the next high or critical release moment, with a
one-line summary of the playbook (how NQ moved in the first 15 minutes after
this figure's earlier releases, counted by result).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from core.desk import facts as F
from core.desk.followup import FEED_CATCH_UP, MAX_QUOTE_GAP
from core.desk.links import ReleaseNotes
from core.desk.models import Move, NextEvent, Playbook, PlaybookIndicator
from core.desk.playbook import VERDICT_WORDS
from core.domain.chart import ChartPin

REACTION = timedelta(minutes=15)
RESULT_WORDS = {
    "above": "Higher than forecast",
    "below": "Lower than forecast",
    "in_line": "In line with forecast",
}
#: How far ahead the next high-impact event is looked for.
NEXT_EVENT_AHEAD = timedelta(days=14)


def _result(slot: F.ReleaseSlot) -> str | None:
    scored = [f for f in slot.figures if f.verdict in RESULT_WORDS]
    if not scored:
        return None
    figure = scored[0]
    text = RESULT_WORDS[figure.verdict]
    return text if len(slot.figures) == 1 else f"{figure.name}: {text.lower()}"


def nq_reaction(history: F.HistoryFn, at: datetime, now: datetime) -> Move | None:
    """NQ's move in the first 15 minutes after ``at``, once the delayed feed has it."""
    if now < at + REACTION + FEED_CATCH_UP:
        return None
    moves = F.measure_moves(history, start=at, end=at + REACTION, symbols=("NQ",))
    return next((m for m in moves if at + REACTION - m.end_at <= MAX_QUOTE_GAP), None)


def pins(events: Iterable[Any], history: F.HistoryFn, *, start: datetime, end: datetime,
         now: datetime, notes: ReleaseNotes) -> list[ChartPin]:
    """Release pins between ``start`` and ``end``, oldest first."""
    found: list[ChartPin] = []
    for slot in F.release_slots(events, start=start, end=end, min_importance="medium"):
        if slot.speakers_only:
            continue
        released = slot.at <= now
        nq = nq_reaction(history, slot.at, now)
        found.append(ChartPin(
            at=slot.at,
            title=slot.title,
            importance="high" if slot.importance in {"high", "critical"} else "medium",
            released=released,
            result_text=_result(slot) if released else None,
            explainer=F.explainer(slot.primary_code),
            nq_15m=nq.change if nq is not None else None,
            nq_15m_text=f"NQ {nq.text} in 15 min" if nq is not None else None,
            note_id=notes.for_slot(slot.at, slot.codes),
        ))
    return found


def playbook_indicator(slot: F.ReleaseSlot, playbook: Playbook) -> PlaybookIndicator | None:
    """The playbook entry for the slot's first figure that has one."""
    indicators = {i.name.casefold(): i for i in playbook.indicators}
    return next((indicators[f.name.casefold()] for f in slot.figures
                 if f.name.casefold() in indicators), None)


def playbook_line(slot: F.ReleaseSlot, playbook: Playbook | None) -> str | None:
    """'Above forecast (3): NQ fell 2, rose 1 in 15 min · …' for the slot's main figure."""
    if playbook is None:
        return None
    indicator = playbook_indicator(slot, playbook)
    if indicator is not None:
        parts = []
        for bucket in indicator.buckets:
            counts = ", ".join(f"{word} {n}" for word, n in (
                ("rose", bucket.first_up), ("fell", bucket.first_down),
                ("barely moved", bucket.first_flat)) if n)
            parts.append(f"{VERDICT_WORDS[bucket.verdict]} ({bucket.count}): NQ {counts}")
        line = f"{indicator.name}, first 15 min: " + "; ".join(parts) + "."
        if not indicator.enough:
            line += f" Only {indicator.count} measured: examples, not a pattern."
        return line
    return "No earlier releases measured yet."


def next_event(events: Iterable[Any], playbook: Playbook | None, *,
               now: datetime) -> NextEvent | None:
    """The next high or critical release moment after ``now``."""
    for slot in F.release_slots(events, start=now, end=now + NEXT_EVENT_AHEAD,
                                min_importance="high"):
        if slot.speakers_only:
            continue
        return NextEvent(
            at=slot.at, title=slot.title, importance=slot.importance,
            kind="fed" if slot.is_fed else "economic",
            explainer=F.explainer(slot.primary_code),
            forecast_text=", ".join(f"{f.name}: forecast {f.forecast_text}"
                                    for f in slot.figures if f.forecast is not None) or None,
            playbook_text=None if slot.is_fed else playbook_line(slot, playbook),
        )
    return None


__all__ = ["NEXT_EVENT_AHEAD", "next_event", "nq_reaction", "pins", "playbook_indicator",
           "playbook_line"]
