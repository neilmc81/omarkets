"""The weekly review: deterministic facts for the note written after the week's last close.

Python works out the week (Monday to its last trading day, so a holiday-shortened
week ends on Thursday), how far each market moved from the previous week's last
close to this week's, NQ's change each day, and which of the week's notes saw
the biggest NQ moves. The writer only explains these facts.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from core.desk.facts import HistoryFn, measure_moves
from core.desk.models import DeskNote, Move, NoteKind
from core.domain.market_calendar import is_trading_day, previous_trading_day, rth_close_time

NEW_YORK = ZoneInfo("America/New_York")

#: Markets in the weekly review, in display order.
REVIEW_SYMBOLS = ("NQ", "ES", "US2Y", "US10Y", "DXY", "VIX", "WTI", "GOLD")
#: Write once the delayed feed has delivered the closing quotes (10 min + margin).
REVIEW_DELAY = timedelta(minutes=15)
#: Keep trying for this long after the close, so a weekend start still catches up.
REVIEW_WINDOW = timedelta(days=2)
#: Notes whose NQ moves are ranked for "what moved NQ this week".
EVENT_KINDS = frozenset({NoteKind.RELEASE, NoteKind.FED, NoteKind.EARNINGS, NoteKind.MOVE})
MAX_EVENTS = 6


def cash_close(day: date) -> datetime:
    return datetime.combine(day, rth_close_time(day), tzinfo=NEW_YORK).astimezone(UTC)


@dataclass(frozen=True)
class ReviewWeek:
    monday: date
    trading_days: tuple[date, ...]
    #: The previous week's last cash close, where the week's changes start.
    previous_close: datetime
    #: This week's last cash close.
    last_close: datetime

    @property
    def note_id(self) -> str:
        return f"review:{self.monday.isoformat()}"


def review_week(now: datetime) -> ReviewWeek | None:
    """The New York week containing ``now``, or ``None`` if it had no trading day."""
    local = now.astimezone(NEW_YORK).date()
    monday = local - timedelta(days=local.weekday())
    days = tuple(d for d in (monday + timedelta(days=i) for i in range(5)) if is_trading_day(d))
    if not days:
        return None
    return ReviewWeek(
        monday=monday,
        trading_days=days,
        previous_close=cash_close(previous_trading_day(days[0])),
        last_close=cash_close(days[-1]),
    )


def review_due(now: datetime) -> ReviewWeek | None:
    week = review_week(now)
    if week is None:
        return None
    if week.last_close + REVIEW_DELAY <= now < week.last_close + REVIEW_WINDOW:
        return week
    return None


def week_moves(history: HistoryFn, week: ReviewWeek, *,
               delayed: frozenset[str] = frozenset()) -> list[Move]:
    return measure_moves(history, start=week.previous_close, end=week.last_close,
                         delayed=delayed, symbols=REVIEW_SYMBOLS,
                         min_window=timedelta(hours=12))


def daily_nq(history: HistoryFn, week: ReviewWeek) -> list[dict[str, Any]]:
    """NQ's change each trading day, close to close. A missing day is left out."""
    rows: list[dict[str, Any]] = []
    for day in week.trading_days:
        start = cash_close(previous_trading_day(day))
        moves = measure_moves(history, start=start, end=cash_close(day), symbols=("NQ",),
                              min_window=timedelta(hours=1))
        if moves:
            rows.append({"day": f"{day:%A}", "nq_change": moves[0].text})
    return rows


def _nq_change(note: DeskNote) -> tuple[float | None, str]:
    """The NQ move a note is best judged by: by the close if measured, else the first."""
    if note.follow_up is not None:
        for point in reversed(note.follow_up.checkpoints):
            nq = next((m for m in point.moves if m.symbol == "NQ"), None)
            if nq is not None:
                when = "by the close" if point.label == "close" else "after 1 hour"
                return nq.change, f"{nq.text} {when}"
    first = next((m for m in note.moves if m.symbol == "NQ"), None)
    if first is not None:
        minutes = round((first.end_at - first.start_at).total_seconds() / 60)
        return first.change, f"{first.text} in the first {minutes} min"
    return None, "not measured"


def ranked_events(notes: Sequence[DeskNote], week: ReviewWeek) -> list[dict[str, Any]]:
    """The week's notes about events, biggest NQ move first."""
    start = datetime.combine(week.monday, datetime.min.time(), tzinfo=NEW_YORK)
    rows: list[tuple[float, dict[str, Any]]] = []
    for note in notes:
        at = note.event_at or note.created_at
        if note.kind not in EVENT_KINDS or note.draft is None:
            continue
        if not (start <= at <= week.last_close):
            continue
        change, text = _nq_change(note)
        rows.append((abs(change) if change is not None else -1.0, {
            "event": note.title,
            "when": at.astimezone(NEW_YORK).strftime("%a %H:%M ET"),
            "headline": note.draft.headline,
            "nq_move": text,
            "did_it_hold": note.follow_up.text if note.follow_up else None,
            "surprises": [f.surprise.text for f in note.figures if f.surprise] or None,
        }))
    rows.sort(key=lambda item: item[0], reverse=True)
    return [row for _, row in rows[:MAX_EVENTS]]


__all__ = ["REVIEW_SYMBOLS", "ReviewWeek", "daily_nq", "ranked_events", "review_due",
           "review_week", "week_moves"]
