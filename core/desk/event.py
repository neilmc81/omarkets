"""One note per event: several desk instruments passing their move trigger in
the same scheduler pass are one cross-market event, written once.

Everything here is deterministic and model-free: which instruments form the
event, which one leads, their order, and the facts about each (every number is
formatted here and handed to the writer as text). Grouping is active only at
prompt versions that take an event task (``prompts.EVENT_VERSIONS``).
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from core.desk.facts import LABELS, YIELD_SYMBOLS
from core.desk.identity import LEGACY, Instrument
from core.desk.models import Move

#: A move note this recent, once written, is joined by instruments that pass their
#: trigger afterwards (silently), instead of each writing a note of its own.
JOIN_WINDOW = timedelta(minutes=60)

#: ``the_event.scope`` when only Treasury yields moved, and otherwise.
RATES = "rates"
CROSS_MARKET = "cross_market"


@dataclass(frozen=True)
class Mover:
    """An instrument that passed its move trigger in this pass."""

    instrument: Instrument
    move: Move
    #: Its trigger, in the unit its move is measured in (percent, or bp for a yield).
    trigger: float
    #: Position in ``desk_instruments``; breaks ties.
    order: int
    #: Set for a market that passed its trigger before this event note, in a note
    #: already written: when. Its ``move`` is then measured again, now.
    earlier_at: datetime | None = None

    @property
    def times_trigger(self) -> float:
        """How many times its trigger it moved (``0`` for a non-positive trigger)."""
        return abs(self.move.change) / self.trigger if self.trigger > 0 else 0.0


@dataclass(frozen=True)
class MoveGroup:
    """One note's worth of movers. ``members`` is lead first; a single mover is a
    group of one, written exactly as a move note always was."""

    members: tuple[Mover, ...]

    @property
    def lead(self) -> Mover:
        return self.members[0]

    @property
    def is_event(self) -> bool:
        return len(self.members) > 1

    @property
    def scope(self) -> str:
        return RATES if all(m.instrument.symbol in YIELD_SYMBOLS for m in self.members) \
            else CROSS_MARKET


def lead_of(movers: Sequence[Mover]) -> Mover:
    """NQ leads whenever it moved; otherwise the largest move relative to its own
    trigger, ties broken by ``desk_instruments`` order."""
    for mover in movers:
        if mover.instrument.symbol == LEGACY:
            return mover
    return min(movers, key=lambda m: (-m.times_trigger, m.order))


def group_moves(movers: Sequence[Mover], *, events: bool) -> list[MoveGroup]:
    """The notes to write for this pass's movers (already past any limit).

    Without ``events`` every mover is its own note, in desk order. With it, two
    or more movers are one event: the lead first, then the rest by how many times
    their trigger they moved, ties by desk order.
    """
    ordered = sorted(movers, key=lambda m: m.order)
    if not events or len(ordered) < 2:
        return [MoveGroup((m,)) for m in ordered]
    lead = lead_of(ordered)
    rest = sorted((m for m in ordered if m is not lead), key=lambda m: (-m.times_trigger, m.order))
    return [MoveGroup((lead, *rest))]


@dataclass(frozen=True)
class Routing:
    """What to do with this pass's movers when a move note may already be in the
    join window: ``joins`` join it silently; ``grouped`` are written as notes
    (:func:`group_moves`); ``escalate`` means an NQ-led event that also takes in the
    markets of the notes already in the window."""

    joins: tuple[Mover, ...] = ()
    grouped: tuple[Mover, ...] = ()
    escalate: bool = False


def route(movers: Sequence[Mover], anchor: Collection[str] | None) -> Routing:
    """Where this pass's movers go. ``anchor`` is every symbol (lead, members and
    later joiners) of the most recent written move note inside :data:`JOIN_WINDOW`,
    or ``None`` when there is no such note: then they are grouped as always.
    Otherwise they all join it, unless NQ is among them and the note does not
    already include NQ: then they, with the earlier markets, are one NQ-led event."""
    if not movers:
        return Routing()
    if anchor is None:
        return Routing(grouped=tuple(movers))
    if any(m.instrument.symbol == LEGACY for m in movers) and LEGACY not in anchor:
        return Routing(grouped=tuple(movers), escalate=True)
    return Routing(joins=tuple(movers))


def event_title(group: MoveGroup) -> str:
    symbols = [m.instrument.symbol for m in group.members]
    names = ", ".join(symbols[:-1]) + f" and {symbols[-1]}"
    if any(m.earlier_at is not None for m in group.members):
        return f"{names} moved in the same event"
    return (f"{names} yields moved together in the last hour" if group.scope == RATES
            else f"{names} moved together in the last hour")


def _trigger_text(mover: Mover) -> str:
    unit = "bp" if mover.move.change_unit == "bp" else "%"
    return f"{mover.trigger:.2f}{unit} in one hour"


def event_facts(group: MoveGroup, desk: Sequence[Instrument], *,
                when: Callable[[Any], str],
                locations: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """The ``the_event`` fact: each mover's own move, how many times its trigger it
    moved and its price location (``locations``, by symbol, where measured), and
    the desk instruments that did not pass their trigger."""
    moved = {m.instrument.symbol for m in group.members}
    rows = []
    for mover in group.members:
        symbol = mover.instrument.symbol
        row: dict[str, Any] = {
            "market": mover.instrument.label, "change": mover.move.text,
            "from": when(mover.move.start_at), "to": when(mover.move.end_at),
            "times_its_trigger": f"{mover.times_trigger:.1f}",
            "its_trigger": _trigger_text(mover),
            "delayed_feed": mover.move.delayed,
        }
        if mover.earlier_at is not None:
            row["triggered_earlier_at"] = when(mover.earlier_at)
        if symbol in locations:
            row.update(locations[symbol])
        rows.append(row)
    return {
        "scope": group.scope,
        "lead_market": group.lead.instrument.label,
        "markets_that_moved": rows,
        "markets_that_did_not_pass_trigger": [
            LABELS.get(i.symbol, i.label) for i in desk if i.symbol not in moved],
    }
