""""Did it hold?" follow-ups: how the first market reaction to an event aged.

Deterministic and model-free. For a release, Fed or earnings note the desk
measures NQ, ES and the 2- and 10-year yields from the event time to two later
checkpoints, then describes NQ's path in one plain line:

    First 15 min: NQ -0.40% · after 1 hour: -0.20% · by the close: +0.60%
    — the first reaction reversed.

Every change runs from the event time (the same starting quote as the note's
first reaction) and uses exchange timestamps. Because NQ/ES arrive about ten
minutes late, a checkpoint is only measured once the feed has caught up.

A move note (single-instrument or cross-market) gets the same treatment for its
OWN instrument, the lead of an event note: from the move's start to one hour
after the move's end and to the next US cash close. A market that is shut at a
checkpoint (a volatility index outside Cboe's hours, a future in its break or
over a holiday) records "not available" there, never a guess.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from core.analysis.move_size import volatility_index
from core.desk.facts import HistoryFn, measure_moves
from core.desk.models import Checkpoint, DeskNote, FollowUp, FollowUpVerdict, Move, NoteKind
from core.domain.market_calendar import is_trading_day, next_trading_day, rth_close_time
from core.domain.sessions import SessionPhase, session_phase, volatility_index_window

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
#: A move smaller than this (percent) counts as no real move. The desk's large-move
#: triggers are 0.6-1.5%, so this is roughly a sixth to a fifteenth of one.
FLAT_PCT = 0.10
#: The same, for a Treasury yield measured in basis points (triggers are 4-10 bp).
FLAT_BP = 1.0
#: The same, for a volatility index, whose moves are a percent of its level
#: (triggers are 4-10%).
FLAT_VOL_PCT = 1.0
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


def flat_size(symbol: str, unit: str) -> float:
    """The size below which a change in ``symbol`` counts as no real move."""
    if unit == "bp":
        return FLAT_BP
    return FLAT_VOL_PCT if volatility_index(symbol) else FLAT_PCT


def follows_up(note: DeskNote) -> bool:
    """Whether the note is one that gets a "did it hold?" follow-up."""
    return note.kind in FOLLOWUP_KINDS or note.kind is NoteKind.MOVE


def subject_symbol(note: DeskNote) -> str:
    """The market a follow-up measures: a move note's own instrument (an event
    note's lead), NQ for a release, Fed or earnings note."""
    if note.kind is NoteKind.MOVE:
        return note.instrument or "NQ"
    return "NQ"


def first_reaction(note: DeskNote) -> Move | None:
    """The move the follow-up compares later changes with: the subject's own."""
    symbol = subject_symbol(note)
    return next((m for m in note.moves if m.symbol == symbol), None)


def market_open(symbol: str, moment: datetime) -> bool:
    """Whether ``symbol``'s market is trading at ``moment``: a volatility index in
    Cboe's hours (no grace: a checkpoint after the close has no quote of its own),
    anything else while its futures session is in its regular or overnight phase."""
    if volatility_index(symbol):
        day = moment.astimezone(NEW_YORK).date()
        if not is_trading_day(day):
            return False
        opens, closes = volatility_index_window(day)
        return opens <= moment <= closes
    return session_phase(moment) in (SessionPhase.RTH, SessionPhase.OVERNIGHT)


def planned_checkpoints(note: DeskNote) -> list[tuple[str, datetime]]:
    """Which checkpoints this note gets, and their exchange times."""
    if not follows_up(note) or note.event_at is None or note.status != "ok":
        return []
    if note.kind is NoteKind.MOVE:
        move = first_reaction(note)
        if move is None:
            return []
        # An hour after the move ended, and the first cash close after that. Always
        # both: a market that is shut then is recorded as "not available".
        one_hour = move.end_at.astimezone(UTC) + HOUR
        return [("1h", one_hour), ("close", cash_close_after(one_hour))]
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


def classify(first: float | None, later: float | None,
             flat: float = FLAT_PCT) -> FollowUpVerdict | None:
    """How a later move (from the event) compares with the first reaction. ``flat``
    is the size under which a change counts as no real move, in the change's unit."""
    if first is None or later is None:
        return None
    if abs(first) < flat:
        return "quiet"
    same_direction = later * first > 0
    if same_direction and abs(later) >= 1.5 * abs(first):
        return "grew"
    if same_direction and abs(later) >= 0.5 * abs(first):
        return "held"
    if not same_direction and abs(later) >= flat:
        return "reversed"
    return "faded"


def _pct(value: float) -> str:
    sign = "+" if value > 0 else "−" if value < 0 else ""
    return f"{sign}{abs(value):.2f}%"


def _change_text(value: float, unit: str) -> str:
    if unit != "bp":
        return _pct(value)
    sign = "+" if value > 0 else "−" if value < 0 else ""
    return f"{sign}{abs(value):.1f} bp"


def _verdict_text(verdict: FollowUpVerdict, later: float, *, final: bool, name: str = "NQ",
                  flat: float = FLAT_PCT, what: str = "the first reaction") -> str:
    if verdict == "quiet":
        direction = "rose" if later >= flat else "fell" if later <= -flat else "stayed flat"
        return f"little first reaction; {name} then {direction}"
    words = {
        "grew": ("the move grew", "the move is growing so far"),
        "held": (f"{what} held", f"{what} is holding so far"),
        "faded": (f"{what} faded", f"{what} is fading so far"),
        "reversed": (f"{what} reversed", f"{what} has reversed so far"),
    }
    return words[verdict][0 if final else 1]


def describe(note: DeskNote, checkpoints: Sequence[Checkpoint], *, final: bool) -> FollowUp:
    symbol = subject_symbol(note)
    is_move = note.kind is NoteKind.MOVE
    first = first_reaction(note)
    # A move note names its own market by its label; the others always say "NQ".
    name = (first.label if first is not None else symbol) if is_move else "NQ"
    unit = first.change_unit if first is not None else "%"
    flat = flat_size(symbol, unit)
    parts: list[str] = []
    if first is not None:
        minutes = round((first.end_at - first.start_at).total_seconds() / 60)
        head = f"The move over {minutes} min" if is_move else f"First {minutes} min"
        parts.append(f"{head}: {name} {_change_text(first.change, unit)}")
    later: float | None = None
    named = first is not None  # name the market once, on the first number shown
    for point in checkpoints:
        found = next((m for m in point.moves if m.symbol == symbol), None)
        label = LABELS[point.label]
        if found is None:
            parts.append(f"{label}: not available")
            continue
        later = found.change
        parts.append(f"{label}: {'' if named else f'{name} '}{_change_text(found.change, unit)}")
        named = True
    verdict = classify(first.change if first is not None else None, later, flat)
    text = " · ".join(parts)
    if verdict is not None and later is not None:
        what = "the move" if is_move else "the first reaction"
        text += f" — {_verdict_text(verdict, later, final=final, name=name, flat=flat, what=what)}."
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

    Returns ``None`` when nothing changed. A checkpoint whose quote is still
    missing is retried until :data:`GIVE_UP_AFTER`, then recorded without it. On a
    move note a checkpoint at which the market is shut is recorded, empty, as soon as
    it is reached.
    """
    if note.event_at is None:
        return None
    is_move = note.kind is NoteKind.MOVE
    # A move note's close checkpoint can fall after a long weekend, more than the
    # horizon from the move's start, so it expires from its last checkpoint instead.
    if not is_move and now - note.event_at > FOLLOWUP_HORIZON:
        return None
    plans = planned_checkpoints(note)
    if not plans or (existing is not None and existing.final):
        return None
    done = {point.label: point for point in (existing.checkpoints if existing else [])}
    changed = False
    symbol = subject_symbol(note)
    if is_move and now - plans[-1][1] > FOLLOWUP_HORIZON:
        return None
    for label, at in plans:
        if label in done:
            continue
        if is_move and now >= at and not market_open(symbol, at):
            done[label] = Checkpoint(label=label, at=at, moves=[])  # shut: no quote, no guess
            changed = True
            continue
        if now < at + FEED_CATCH_UP:
            continue
        moves = [
            m for m in measure_moves(history, start=note.event_at, end=at, delayed=delayed,
                                     symbols=(symbol,) if is_move else FOLLOWUP_SYMBOLS)
            if at - m.end_at <= MAX_QUOTE_GAP  # an old quote would misstate the move
        ]
        if not any(m.symbol == symbol for m in moves) \
                and now < at + FEED_CATCH_UP + GIVE_UP_AFTER:
            continue  # the quote may still arrive
        done[label] = Checkpoint(label=label, at=at, moves=moves)
        changed = True
    if not changed:
        return None
    ordered = [done[label] for label, _ in plans if label in done]
    return describe(note, ordered, final=len(ordered) == len(plans))


__all__ = [
    "FLAT_BP",
    "FLAT_PCT",
    "FLAT_VOL_PCT",
    "FOLLOWUP_KINDS",
    "cash_close_after",
    "classify",
    "describe",
    "flat_size",
    "follows_up",
    "market_open",
    "planned_checkpoints",
    "subject_symbol",
    "update",
]
