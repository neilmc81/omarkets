"""Mechanical checks for desk notes (docs/PLAN.md §7 rules a program can check).

Judgment rules (coverage, cause attribution, windows, usefulness) are scored
by hand on the blind sheet (``core/desk/evaluation/sheet.py``).

Every check reads the note's own text and the facts it was written from, never
the prompt: the system prompt's example numbers are not facts.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from core.desk.evaluation.cases import instrument_for
from core.desk.lessons import RECENT, TOPICS, _lead
from core.desk.models import NoteDraft, NoteKind
from core.desk.prompts import (
    PROMPT_VERSION,
    PROMPTS,
    NoPromptProfile,
    facts_text,
    has_profile,
    render,
)
from core.desk.writer import _number_forms, invented_numbers

#: §7 length targets in words, over every draft field.
LENGTH_TARGETS: dict[str, tuple[int, int]] = {
    "premarket": (200, 260), "review": (200, 260),
    "move": (140, 180), "release": (140, 180), "fed": (140, 180),
    "earnings": (140, 180), "auction": (140, 180),
    "ask": (120, 160),
}
#: A lesson whose topic was taught in this many earlier notes counts as a repeat.
REPEAT_WINDOW = RECENT

#: Every counted rule, in report order. ``compare`` fails if any of them gets worse.
RULES: tuple[str, ...] = (
    "signed_direction", "closed_as_stale", "absent_not_missing", "bp_as_percent",
    "sign_errors", "repeated_lesson", "over_length", "under_length", "invented_numbers",
)

_UP = r"rose|rises|rising|risen|up|gained|gains|added|adds|higher|jumped|climbed|firmed|rallied"
_DOWN = r"fell|falls|falling|fallen|down|lower|dropped|drops|slipped|slid|declined|lost|eased|sank"
_FILLER = r"(?:\s+(?:by|about|nearly|almost|roughly|around|just|only|another))*"
SIGNED = re.compile(rf"\b(?:{_UP}|{_DOWN})\b{_FILLER}\s+[+\-−]\d[\d,.]*(?:%|bp)?",
                    re.IGNORECASE)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_CLAUSE_END = re.compile(r"[.!?;:]\s")
_CLOSED = re.compile(r"\b(closed|shut|weekend|reopen\w*|holiday)\b", re.IGNORECASE)
#: "closed, not stale" keeps the states apart, which is what the rule asks for.
_NOT_STALE = re.compile(r"\bnot\s+(?:a\s+)?stale\b", re.IGNORECASE)
_ABSENT = re.compile(r"\b(?:there (?:was|were|is|are) no|nothing was) (?:scheduled )?"
                     r"(?:event|events|release|releases|data|news)\b", re.IGNORECASE)
_HEDGED = re.compile(r"\b(supplied|in (?:our|the) (?:feed|data|calendar|facts)|not (?:in|on) "
                     r"(?:our|the))\b", re.IGNORECASE)
#: A basis point defined as a percent ("1bp is 0.01%"), or a yield change in
#: small percents ("the 10-year rose 0.02%").
_BP_PERCENT = re.compile(
    r"(?:(?<![a-z])bp\b|basis points?)[^.;]{0,20}?(?<![\d.])0\.\d+%"
    r"|\b(?:yields?|2-year|10-year)\b(?:\s+[\w-]+){0,4}?\s+(?:by\s+)?(?<![\d.])0\.\d+%",
    re.IGNORECASE)
#: A direction word at most three words before the end of the text (then the number).
_DIRECTION_BEFORE = re.compile(
    rf"\b(?:(?P<up>{_UP})|(?P<down>{_DOWN}))\b(?:\s+[\w,]+){{0,3}}?\s*[+\-−]?\s*$",
    re.IGNORECASE)

# --------------------------------------------------------------------------- instruments

#: How prose names each instrument. Facts label instruments by name ("market":
#: "Nasdaq 100 futures", "name": "2-year yield"); a label maps to the symbol
#: whose longest name it contains. All-capital codes match case-sensitively.
INSTRUMENT_NAMES: dict[str, tuple[str, ...]] = {
    "NQ": ("NQ", "Nasdaq 100 futures", "Nasdaq 100", "Nasdaq futures", "Nasdaq"),
    "ES": ("ES", "S&P 500 futures", "S&P 500", "S&P"),
    "YM": ("YM", "Dow futures", "Dow Jones", "Dow"),
    "US2Y": ("US2Y", "2-year yield", "2-year", "two-year", "2y"),
    "US10Y": ("US10Y", "10-year yield", "10-year", "ten-year", "10y"),
    "CURVE": ("curve",),
    "DXY": ("DXY", "US dollar", "dollar"),
    "VIX": ("VIX fear gauge", "VIX"),
    "VXN": ("Nasdaq fear gauge", "VXN"),
    "WTI": ("Crude oil", "crude", "oil", "WTI"),
    "GOLD": ("Gold",),
    "BTC": ("Bitcoin", "BTC"),
}
_LABEL_KEYS = ("market", "name", "label")
_SIGNED_TEXT = re.compile(r"\s*([+\-−])(\d[\d,]*(?:\.\d+)?)\s*(%|bps?|pts?)?\s*")
_DRAFT_NUMBER = re.compile(
    r"(?<![\d.,])(\d[\d,]*(?:\.\d+)?)(?![\d])"
    r"(?:\s*(%|bps?\b|basis points?|percentage points?|pts?\b))?", re.IGNORECASE)


def _unit(text: str | None) -> str | None:
    if not text:
        return None
    text = text.lower()
    if text == "%":
        return "%"
    if text.startswith(("bp", "basis")):
        return "bp"
    return "pt"


def _name_patterns(names: Iterable[str]) -> tuple[re.Pattern, re.Pattern]:
    ordered = sorted(set(names), key=len, reverse=True)
    codes = [n for n in ordered if n.isupper() and n.isalnum()]
    words = [n for n in ordered if n not in codes]

    def pattern(items: list[str], flags: int) -> re.Pattern:
        if not items:
            return re.compile(r"(?!)")
        return re.compile(r"(?<![\w&])(?:" + "|".join(re.escape(n) for n in items)
                          + r")(?![\w&])", flags)

    return pattern(codes, 0), pattern(words, re.IGNORECASE)


def _names(extra: Iterable[str] = ()) -> dict[str, str]:
    """Every name (lower-cased) → the instrument key it names."""
    lookup = {n.casefold(): symbol for symbol, names in INSTRUMENT_NAMES.items() for n in names}
    for label in extra:
        lookup.setdefault(label.casefold(), label.casefold())
    return lookup


def instrument_key(label: str) -> str:
    """The symbol a facts label names ("Nasdaq fear gauge" → VXN), else the label itself."""
    lookup = _names()
    codes, words = _name_patterns(n for names in INSTRUMENT_NAMES.values() for n in names)
    found = [m.group(0) for p in (codes, words) for m in p.finditer(label)]
    if not found:
        return label.casefold()
    return lookup[max(found, key=len).casefold()]


@dataclass(frozen=True)
class FactMove:
    """One signed change in the facts: whose, how big, which way, in what unit."""

    instrument: str | None
    magnitude: str
    sign: int
    unit: str | None


def fact_moves(facts: Any) -> list[FactMove]:
    """Every signed change text in the facts ("+0.88%", "-1.7bp") with its instrument.

    Read from any dict that names an instrument (``market``, ``name`` or
    ``label``); a signed text with no name nearby is kept with no instrument.
    """
    found: list[FactMove] = []

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            label = next((node[k] for k in _LABEL_KEYS if isinstance(node.get(k), str)), None)
            for value in node.values():
                if isinstance(value, str):
                    match = _SIGNED_TEXT.fullmatch(value)
                    if match:
                        sign = -1 if match.group(1) in "-−" else 1
                        found.append(FactMove(instrument_key(label) if label else None,
                                              match.group(2).replace(",", ""), sign,
                                              _unit(match.group(3))))
                else:
                    walk(value)
        elif isinstance(node, list | tuple):
            for item in node:
                walk(item)

    walk(facts)
    return found


def _forms(magnitude: str) -> set[str]:
    """A magnitude and its roundings; a fraction is never rounded to a whole number
    ("0.88" → 0.88, 0.9), but a whole number stays itself ("3.0" → 3)."""
    exact = _normal(magnitude)
    return {f for f in _number_forms(magnitude) if "." in f or f == exact}


def _normal(number: str) -> str | None:
    try:
        return format(Decimal(number.replace(",", "")).normalize(), "f")
    except InvalidOperation:
        return None


# --------------------------------------------------------------------------- rules


def draft_fields(draft: Mapping[str, Any]) -> list[str]:
    return [draft.get(key) or "" for key in
            ("headline", "summary", "what_it_means", "market_reaction", "rates_read", "learn")
            ] + list(draft.get("watch_next") or [])


def sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE.split(text) if s.strip()]


def signed_direction(text: str) -> list[str]:
    """'rose +0.88%': a direction word next to a signed number (say 'rose 0.88%')."""
    return [m.group(0) for m in SIGNED.finditer(text)]


def closed_as_stale(fields: Sequence[str]) -> list[str]:
    """A shut market described as 'stale' (closed and stale are different states)."""
    return [s for f in fields for s in sentences(f)
            if "stale" in s.lower() and _CLOSED.search(s) and not _NOT_STALE.search(s)]


def absent_not_missing(fields: Sequence[str]) -> list[str]:
    """'There was no event' where the facts can only say none was supplied."""
    return [s for f in fields for s in sentences(f)
            if _ABSENT.search(s) and not _HEDGED.search(s)]


def bp_as_percent(text: str) -> list[str]:
    """A basis point or yield change as a small percent; say 'percentage point' or bp."""
    return [m.group(0) for m in _BP_PERCENT.finditer(text)]


def sign_errors(text: str, facts: Any) -> list[str]:
    """A measured change quoted with the wrong direction word, per instrument.

    "NQ slipped 0.06%" is checked against NQ's own changes in the facts. When
    the sentence names no instrument, or names one with no change of that size,
    any instrument's change of that size may justify the direction (several
    markets can share a magnitude: NQ -0.06%, DXY +0.06%).
    """
    moves = fact_moves(facts)
    by_form: dict[str, list[FactMove]] = {}
    for move in moves:
        for form in _forms(move.magnitude):
            by_form.setdefault(form, []).append(move)
    if not by_form:
        return []
    labels = {m.instrument for m in moves if m.instrument}
    lookup = _names(labels)
    codes, words = _name_patterns(list(lookup) + [n for ns in INSTRUMENT_NAMES.values()
                                                   for n in ns])
    mentions = sorted([(m.start(), lookup[m.group(0).casefold()])
                       for p in (codes, words) for m in p.finditer(text)
                       if m.group(0).casefold() in lookup])
    found: list[str] = []
    for hit in _DRAFT_NUMBER.finditer(text):
        number = _normal(hit.group(1))
        candidates = by_form.get(number or "")
        if not candidates:
            continue
        unit = _unit(hit.group(2))
        if unit is not None:
            candidates = [m for m in candidates if m.unit in (None, unit)]
            if not candidates:
                continue
        before = text[max(0, hit.start() - 60):hit.start()]
        word = _DIRECTION_BEFORE.search(before)
        if word is None:
            continue
        word_at = hit.start() - len(before) + word.start()
        clause = max((m.end() for m in _CLAUSE_END.finditer(text, 0, word_at)), default=0)
        named = [key for at, key in mentions if clause <= at < word_at]
        own = [m for m in candidates if named and m.instrument == named[-1]]
        allowed = {m.sign for m in (own or candidates)}
        said = 1 if word.group("up") else -1
        if said not in allowed:
            found.append(f"{word.group(0).strip()} {hit.group(0).strip()}")
    return found


def lesson_term(text: str) -> str | None:
    """The term a lesson teaches ("vix", "auction"), from the Learn page's topic words.

    ``None`` when the lesson names no topic word, so a lesson filed under the
    catch-all "Market moves" is never counted as a repeat on that alone.
    """
    lead = _lead(text)
    for _, _, words in TOPICS:
        match = words.search(lead)
        if match:
            return match.group(0).lower().rstrip("s")
    return None


def repeated_lesson(learn: str | None, created_at: str, history: Sequence[Mapping],
                    *, note_id: str | None = None) -> str | None:
    """The term this lesson teaches, if one of the few notes written before it taught it.

    Judged from the candidate's own ``learn`` text against the frozen history of
    notes created strictly earlier (the note itself, if present, is skipped).
    """
    if not learn:
        return None
    mine = lesson_term(learn)
    if mine is None:
        return None
    earlier = [row for row in sorted(history, key=lambda r: r["created_at"])
               if row.get("learn") and row["created_at"] < created_at and row["id"] != note_id]
    recent = {lesson_term(row["learn"]) for row in earlier[-REPEAT_WINDOW:]}
    return mine if mine in recent else None


# --------------------------------------------------------------------------- scoring


@dataclass
class Score:
    """Mechanical findings for one draft. Fewer is better; zero is the goal."""

    case_id: str
    kind: str
    model: str
    words: int
    length_target: tuple[int, int] | None
    #: The facts are everything the writer saw, so a number missing from them
    #: was invented. Older notes stored only part of their facts.
    facts_complete: bool
    signed_direction: list[str] = field(default_factory=list)
    closed_as_stale: list[str] = field(default_factory=list)
    absent_not_missing: list[str] = field(default_factory=list)
    bp_as_percent: list[str] = field(default_factory=list)
    sign_errors: list[str] = field(default_factory=list)
    repeated_lesson: str | None = None
    #: Numbers found nowhere in the facts (or a rounding of one).
    numbers_not_in_facts: list[str] = field(default_factory=list)

    @property
    def over_length(self) -> bool:
        return self.length_target is not None and self.words > self.length_target[1]

    @property
    def under_length(self) -> bool:
        """Too short to carry the note: a candidate cannot win by saying less."""
        return self.length_target is not None and self.words < self.length_target[0]

    @property
    def invented_numbers(self) -> list[str]:
        """Counted only with complete facts; otherwise they are unverifiable."""
        return self.numbers_not_in_facts if self.facts_complete else []

    @property
    def unverifiable_numbers(self) -> list[str]:
        return [] if self.facts_complete else self.numbers_not_in_facts

    def counts(self) -> dict[str, int]:
        """Rule breaks per rule (``RULES``)."""
        return {
            "signed_direction": len(self.signed_direction),
            "closed_as_stale": len(self.closed_as_stale),
            "absent_not_missing": len(self.absent_not_missing),
            "bp_as_percent": len(self.bp_as_percent),
            "sign_errors": len(self.sign_errors),
            "repeated_lesson": int(self.repeated_lesson is not None),
            "over_length": int(self.over_length),
            "under_length": int(self.under_length),
            "invented_numbers": len(self.invented_numbers),
        }

    @property
    def issues(self) -> int:
        return sum(self.counts().values())

    def findings(self) -> dict[str, list[str]]:
        """The text behind each nonzero count, for reports."""
        return {name: value for name, value in (
            ("signed_direction", self.signed_direction),
            ("closed_as_stale", self.closed_as_stale),
            ("absent_not_missing", self.absent_not_missing),
            ("bp_as_percent", self.bp_as_percent),
            ("sign_errors", self.sign_errors),
            ("invented_numbers", self.invented_numbers),
            ("unverifiable_numbers", self.unverifiable_numbers),
        ) if value}


def score_draft(*, case_id: str, kind: str, model: str, created_at: str,
                draft: Mapping[str, Any], facts: Any, facts_complete: bool,
                history: Sequence[Mapping], instrument: str | None = None,
                prompt_version: str | None = None) -> Score:
    """``instrument`` is the symbol the note was written for (NQ when not recorded);
    ``prompt_version`` the prompt that wrote it, whose stated facts a number may
    also come from (the production default for older or unrecorded versions)."""
    target = instrument_for(kind, instrument)
    version = prompt_version if prompt_version in PROMPTS else PROMPT_VERSION
    if target is not None and not has_profile(target.symbol, version=version):
        # An instrument this version can't write for (ES at desk:v8): the
        # newest version that has its profile.
        version = next((v for v in reversed(PROMPTS) if has_profile(target.symbol, version=v)),
                       version)
    try:
        sources = render(NoteKind(kind), target, facts, version=version).number_sources
    except NoPromptProfile:
        sources = facts_text(facts)  # no prompt at all: only the facts are allowed
    fields = draft_fields(draft)
    text = " ".join(fields)
    parsed = NoteDraft.model_validate(dict(draft))
    return Score(
        case_id=case_id, kind=kind, model=model,
        words=len(text.split()), length_target=LENGTH_TARGETS.get(kind),
        facts_complete=facts_complete,
        signed_direction=signed_direction(text), closed_as_stale=closed_as_stale(fields),
        absent_not_missing=absent_not_missing(fields), bp_as_percent=bp_as_percent(text),
        sign_errors=[e for f in fields for e in sign_errors(f, facts)],
        repeated_lesson=repeated_lesson(draft.get("learn"), created_at, history,
                                        note_id=case_id),
        numbers_not_in_facts=invented_numbers(parsed, sources),
    )


def summary_row(score: Score) -> dict[str, Any]:
    """One draft's record: counts per rule, as stored in a baseline."""
    counts = score.counts()
    return {"words": score.words, "facts_complete": score.facts_complete,
            **counts, "over_length": score.over_length, "under_length": score.under_length,
            "repeated_lesson": score.repeated_lesson,
            "unverifiable_numbers": len(score.unverifiable_numbers), "issues": score.issues}


def table(scores: Sequence[Score]) -> str:
    lines = ["case | model | words (target) | signed | closed≠stale | absent | bp% | sign | "
             "repeat | invented | unverifiable | issues"]
    for s in scores:
        target = f"{s.length_target[0]}-{s.length_target[1]}" if s.length_target else "-"
        lines.append(f"{s.case_id} | {s.model.split('/')[-1]} | {s.words} ({target}) | "
                     f"{len(s.signed_direction)} | {len(s.closed_as_stale)} | "
                     f"{len(s.absent_not_missing)} | {len(s.bp_as_percent)} | "
                     f"{len(s.sign_errors)} | {s.repeated_lesson or '-'} | "
                     f"{len(s.invented_numbers)} | {len(s.unverifiable_numbers)} | {s.issues}")
    return "\n".join(lines)
