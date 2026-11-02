"""Note identity: which instrument a note is about, and its id.

NQ keeps its existing ids for good (``premarket:2026-10-29``): they are the keys
used by links, pins, the playbook and notifications. Another instrument's
pre-market, move and ask notes carry the symbol after the kind
(``premarket:ES:2026-10-29``). Event notes (release, Fed, auction, earnings,
primer, review) are desk-wide and never carry a symbol.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from core.desk.facts import LABELS
from core.desk.models import NoteKind

#: The instrument whose notes keep the legacy id form.
LEGACY = "NQ"
#: Note kinds written per instrument. Every other kind is desk-wide.
INSTRUMENT_KINDS = frozenset({NoteKind.PREMARKET, NoteKind.MOVE, NoteKind.ASK})
#: Instruments the production desk writes for by default (``desk_instruments``
#: overrides). Each needs a profile at the default prompt version. Tests may
#: pass others.
PRODUCTION_INSTRUMENTS: tuple[str, ...] = ("NQ", "ES", "YM", "GOLD", "WTI", "DXY")
#: What every instrument but NQ gets: move and ask notes, no daily pre-market
#: (the pre-market is written by the premium model; docs/PLAN.md §7 cost policy).
SECONDARY_KINDS = frozenset({NoteKind.MOVE, NoteKind.ASK})

_SYMBOL = re.compile(r"[A-Z][A-Z0-9]{0,9}")


@dataclass(frozen=True)
class Instrument:
    """The instrument a pre-market, move or ask note is written for."""

    symbol: str
    label: str

    @classmethod
    def of(cls, symbol: str) -> Instrument:
        if not _SYMBOL.fullmatch(symbol):
            raise ValueError(f"not an instrument symbol: {symbol!r}")
        return cls(symbol, LABELS.get(symbol, symbol))


NQ = Instrument.of("NQ")


@dataclass(frozen=True)
class NoteRef:
    kind: str
    instrument: str | None
    rest: str


def note_id(kind: NoteKind, rest: str, instrument: Instrument | str | None = None) -> str:
    """The id of a note. ``instrument`` is required for :data:`INSTRUMENT_KINDS`
    and refused for every other kind."""
    symbol = instrument.symbol if isinstance(instrument, Instrument) else instrument
    if kind in INSTRUMENT_KINDS:
        if symbol is None:
            raise ValueError(f"{kind.value} notes need an instrument")
        Instrument.of(symbol)
        return f"{kind.value}:{rest}" if symbol == LEGACY else f"{kind.value}:{symbol}:{rest}"
    if symbol is not None:
        raise ValueError(f"{kind.value} notes are desk-wide and carry no instrument")
    return f"{kind.value}:{rest}"


def parse_note_id(value: str) -> NoteRef:
    """Split an id into kind, instrument and the rest. Legacy instrument-kind ids
    are NQ's; event ids have no instrument (``earnings:AAPL:…`` is a company)."""
    kind, _, rest = value.partition(":")
    if kind not in {k.value for k in INSTRUMENT_KINDS}:
        return NoteRef(kind, None, rest)
    head, sep, tail = rest.partition(":")
    if sep and _SYMBOL.fullmatch(head):
        return NoteRef(kind, head, tail)
    return NoteRef(kind, LEGACY, rest)


def instrument_kinds(instrument: Instrument | str) -> frozenset[NoteKind]:
    """The note kinds the desk writes for an instrument: NQ gets all of
    :data:`INSTRUMENT_KINDS`, every other instrument :data:`SECONDARY_KINDS`."""
    symbol = instrument.symbol if isinstance(instrument, Instrument) else instrument
    return INSTRUMENT_KINDS if symbol == LEGACY else SECONDARY_KINDS


def instrument_of(value: str) -> str | None:
    return parse_note_id(value).instrument


__all__ = ["INSTRUMENT_KINDS", "LEGACY", "NQ", "PRODUCTION_INSTRUMENTS", "SECONDARY_KINDS",
           "Instrument", "NoteRef", "instrument_kinds", "instrument_of", "note_id",
           "parse_note_id"]
