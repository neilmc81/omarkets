"""The Calendar page's view model: a week of release moments. Deterministic; no AI.

Each row is a release moment from :func:`core.desk.facts.release_slots` (the
same grouping the notes, pins and playbook use), with its figures, each
surprise sized against the figure's own history, a plain-English better/worse
read, NQ's move in the first 15 minutes (as on the chart pins), the desk note
and the playbook summary. Rows are grouped by day in the reader's time zone.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from core.desk import explainers as X
from core.desk import facts as F
from core.desk import pins as PN
from core.desk.followup import FEED_CATCH_UP
from core.desk.links import ReleaseNotes
from core.desk.models import CalendarDay, CalendarFigure, CalendarRow, Figure, Playbook

#: Market-moving means medium importance or more; "show all" adds low.
MARKET_MOVING = "medium"
#: Past releases kept per playbook entry on a row (newest first).
PLAYBOOK_RELEASES = 12

#: (reading, text) for above / below forecast, by reading family. Inflation and
#: jobless figures are better for the economy when lower; growth when higher.
#: Fed decisions have no clear good side, so they get no read.
_READS: dict[str, dict[str, tuple[str, str]]] = {
    "inflation": {"above": ("worse", "Hotter than forecast"),
                  "below": ("better", "Cooler than forecast")},
    "jobless": {"above": ("worse", "Weaker jobs data than forecast"),
                "below": ("better", "Stronger jobs data than forecast")},
    "growth": {"above": ("better", "Stronger than forecast"),
               "below": ("worse", "Weaker than forecast")},
}


def read_figure(figure: Figure, code: str | None) -> CalendarFigure:
    """The figure with its better/worse read (none when unscored or no clear side)."""
    family = X.reading_family(code) if code else None
    read = _READS.get(family or "", {}).get(figure.verdict)
    if figure.verdict == "in_line":
        read_text: tuple[str | None, str | None] = (None, "In line with forecast")
    else:
        read_text = read if read else (None, None)
    return CalendarFigure(**figure.model_dump(), reading=read_text[0],
                          reading_text=read_text[1])


def _codes_by_name(rows: Iterable[Any], at: datetime) -> dict[str, str]:
    """Each figure name's event code at one moment (figures are merged by name)."""
    found: dict[str, str] = {}
    for row in rows:
        if getattr(row, "scheduled_at", None) == at:
            found.setdefault(row.name.casefold(), row.event_code)
    return found


def _kind(slot: F.ReleaseSlot) -> str:
    return "speech" if slot.speakers_only else "fed" if slot.is_fed else "economic"


def rows(events: Iterable[Any], history: F.HistoryFn, *, start: datetime, end: datetime,
         now: datetime, notes: ReleaseNotes, playbook: Playbook | None,
         surprise_history: Iterable[Any] = (), show_all: bool = False) -> list[CalendarRow]:
    """Release moments between ``start`` and ``end``, oldest first."""
    events = list(events)
    past = list(surprise_history)
    found: list[CalendarRow] = []
    for slot in F.release_slots(events, start=start, end=end,
                                min_importance="low" if show_all else MARKET_MOVING):
        released = slot.at <= now
        figures = F.size_surprises(slot.figures, past, before=slot.at) if released \
            else slot.figures
        codes = _codes_by_name(events, slot.at)
        if not released:
            status = "upcoming"
        elif now < slot.at + PN.REACTION + FEED_CATCH_UP:
            status = "waiting"
        else:
            status = "measured"
        nq = PN.nq_reaction(history, slot.at, now) if status == "measured" else None
        if status == "measured" and nq is None:
            status = "unavailable"
        key = f"indicator:{'FED_SPEECH' if slot.speakers_only else slot.primary_code}"
        entry = X.LIBRARY.get(key)
        indicator = None
        if playbook is not None and slot.figures and not slot.is_fed:
            indicator = PN.playbook_indicator(slot, playbook)
        if indicator is not None:
            indicator = indicator.model_copy(
                update={"releases": indicator.releases[:PLAYBOOK_RELEASES]})
        found.append(CalendarRow(
            at=slot.at,
            title=slot.title,
            codes=slot.codes,
            importance=slot.importance,
            kind=_kind(slot),
            released=released,
            figures=[read_figure(f, codes.get(f.name.casefold())) for f in figures],
            venue=slot.venue or None,
            nq_15m=nq.change if nq is not None else None,
            nq_15m_text=f"NQ {nq.text} in 15 min" if nq is not None else None,
            nq_15m_status=status,
            note_id=notes.for_slot(slot.at, slot.codes),
            explainer_key=key if entry else None,
            explainer=entry.text if entry else None,
            playbook_text=(PN.playbook_line(slot, playbook)
                           if slot.figures and not slot.is_fed else None),
            playbook=indicator,
        ))
    return found


def by_day(found: Iterable[CalendarRow], tz: ZoneInfo) -> list[CalendarDay]:
    """Rows grouped by their day in ``tz``, in time order."""
    days: dict[str, list[CalendarRow]] = {}
    labels: dict[str, str] = {}
    for row in sorted(found, key=lambda r: r.at):
        local = row.at.astimezone(tz)
        key = local.date().isoformat()
        days.setdefault(key, []).append(row)
        labels[key] = f"{local:%A, %b} {local.day}"
    return [CalendarDay(date=key, label=labels[key], rows=items) for key, items in days.items()]


__all__ = ["MARKET_MOVING", "by_day", "read_figure", "rows"]
