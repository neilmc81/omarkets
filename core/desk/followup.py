""""Did it hold?" follow-ups: how the first market reaction to an event aged.

Deterministic and model-free. For a release, Fed or earnings note the desk
measures NQ, ES and the 2- and 10-year yields from the event time to two later
checkpoints, then describes NQ's path in one plain line:

    First 15 min: NQ -0.40% · after 1 hour: -0.20% · by the close: +0.60%
    — the first reaction reversed.

Every change runs from the event time (the same starting quote as the note's
first reaction) and uses exchange timestamps. Because NQ/ES arrive about ten
minutes late, a checkpoint is only measured once the feed has caught up.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from core.desk.facts import HistoryFn, measure_moves
from core.desk.models import Checkpoint, DeskNote, FollowUp, FollowUpVerdict, Move, NoteKind
from core.domain.market_calendar import is_trading_day, next_trading_day, rth_close_time
from core.domain.sessions import SessionPhase, session_phase

NEW_YORK = ZoneInfo("America/New_York")

FOLLOWUP_KINDS = frozenset({NoteKind.RELEASE, NoteKind.FED, NoteKind.EARNINGS})
FOLLOWUP_SYMBOLS = ("NQ", "ES", "US2Y", "US10Y")
HOUR = timedelta(hours=1)
#: The free NQ/ES feed is about ten minutes behind, plus a minute of margin.
FEED_CATCH_UP = timedelta(minutes=11)
#: Stop waiting for a missing NQ quote this long after a checkpoint was due.
GIVE_UP_AFTER = timedelta(hours=2)
#: A checkpoint quote must be at most this far before the checkpoint time.
MAX_QUOTE_GAP = timedelta(minutes=5)
#: Notes older than this get no new follow-ups.
FOLLOWUP_HORIZON = timedelta(days=4)
#: An NQ move smaller than this (percent) counts as no real move.
FLAT_PCT = 0.10
LABELS = {"1h": "after 1 hour", "close": "by the close"}


def cash_close_after(moment: datetime) -> datetime:
    """The first US cash-session close (16:00 ET, 13:00 on early-close days) after ``moment``."""
    day = moment.astimezone(NEW_YORK).date()
    for _ in range(15):
        if is_trading_day(day):
            close = datetime.combine(day, rth_close_time(day), tzinfo=NEW_YORK).astimezone(UTC)
            if close > moment:
                return close
        day = next_trading_day(day)
    raise ValueError(f"no trading day found after {moment.isoformat()}")  # pragma: no cover


def first_reaction(note: DeskNote) -> Move | None:
    return next((m for m in note.moves if m.symbol == "NQ"), None)


def planned_checkpoints(note: DeskNote) -> list[tuple[str, datetime]]:
    """Which checkpoints this note gets, and their exchange times."""
    if note.kind not in FOLLOWUP_KINDS or note.event_at is None or note.status != "ok":
        return []
    event = note.event_at.astimezone(UTC)
    first = first_reaction(note)
    plans: list[tuple[str, datetime]] = []
    one_hour = event + HOUR
    window = (first.end_at - first.start_at) if first is not None else timedelta(0)
    # The hour mark adds nothing when the note already measured an hour, and
    # means nothing when the market is shut at that moment.
    if window < HOUR and session_phase(one_hour) in (SessionPhase.RTH, SessionPhase.OVERNIGHT):
        plans.append(("1h", one_hour))
    plans.append(("close", cash_close_after(max(one_hour, event + window))))
    return plans


def classify(first: float | None, later: float | None) -> FollowUpVerdict | None:
    """How NQ's later move (from the event) compares with its first reaction."""
    if first is None or later is None:
        return None
    if abs(first) < FLAT_PCT:
        return "quiet"
    same_direction = later * first > 0
    if same_direction and abs(later) >= 1.5 * abs(first):
        return "grew"
    if same_direction and abs(later) >= 0.5 * abs(first):
        return "held"
    if not same_direction and abs(later) >= FLAT_PCT:
        return "reversed"
    return "faded"


def _pct(value: float) -> str:
    sign = "+" if value > 0 else "−" if value < 0 else ""
    return f"{sign}{abs(value):.2f}%"


def _verdict_text(verdict: FollowUpVerdict, later: float, *, final: bool) -> str:
    if verdict == "quiet":
        direction = "rose" if later >= FLAT_PCT else "fell" if later <= -FLAT_PCT else "stayed flat"
        return f"little first reaction; NQ then {direction}"
    words = {
        "grew": ("the move grew", "the move is growing so far"),
        "held": ("the first reaction held", "the first reaction is holding so far"),
        "faded": ("the first reaction faded", "the first reaction is fading so far"),
        "reversed": ("the first reaction reversed", "the first reaction has reversed so far"),
    }
    return words[verdict][0 if final else 1]


def describe(note: DeskNote, checkpoints: Sequence[Checkpoint], *, final: bool) -> FollowUp:
    first = first_reaction(note)
    parts: list[str] = []
    if first is not None:
        minutes = round((first.end_at - first.start_at).total_seconds() / 60)
        parts.append(f"First {minutes} min: NQ {_pct(first.change)}")
    later: float | None = None
    named = first is not None  # say "NQ" once, on the first number shown
    for point in checkpoints:
        nq = next((m for m in point.moves if m.symbol == "NQ"), None)
        label = LABELS[point.label]
        if nq is None:
            parts.append(f"{label}: not available")
            continue
        later = nq.change
        parts.append(f"{label}: {'' if named else 'NQ '}{_pct(nq.change)}")
        named = True
    verdict = classify(first.change if first is not None else None, later)
    text = " · ".join(parts)
    if verdict is not None and later is not None:
        text += f" — {_verdict_text(verdict, later, final=final)}."
    return FollowUp(
        checkpoints=list(checkpoints), verdict=verdict, final=final, text=text,
    )


def update(
    note: DeskNote,
    existing: FollowUp | None,
    history: HistoryFn,
    *,
    now: datetime,
    delayed: frozenset[str] = frozenset(),
) -> FollowUp | None:
    """The note's follow-up after measuring any checkpoint that is now due.

    Returns ``None`` when nothing changed. A checkpoint whose NQ quote is still
    missing is retried until :data:`GIVE_UP_AFTER`, then recorded without NQ.
    """
    if note.event_at is None or now - note.event_at > FOLLOWUP_HORIZON:
        return None
    plans = planned_checkpoints(note)
    if not plans or (existing is not None and existing.final):
        return None
    done = {point.label: point for point in (existing.checkpoints if existing else [])}
    changed = False
    for label, at in plans:
        if label in done or now < at + FEED_CATCH_UP:
            continue
        moves = [
            m for m in measure_moves(history, start=note.event_at, end=at, delayed=delayed,
                                     symbols=FOLLOWUP_SYMBOLS)
            if at - m.end_at <= MAX_QUOTE_GAP  # an old quote would misstate the move
        ]
        if not any(m.symbol == "NQ" for m in moves) and now < at + FEED_CATCH_UP + GIVE_UP_AFTER:
            continue  # the quote may still arrive
        done[label] = Checkpoint(label=label, at=at, moves=moves)
        changed = True
    if not changed:
        return None
    ordered = [done[label] for label, _ in plans if label in done]
    return describe(note, ordered, final=len(ordered) == len(plans))


__all__ = [
    "FOLLOWUP_KINDS",
    "cash_close_after",
    "classify",
    "describe",
    "planned_checkpoints",
    "update",
]
