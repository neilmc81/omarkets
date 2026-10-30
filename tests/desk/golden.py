"""Mechanical checks for the desk-note golden set (docs/PLAN.md §7).

Test tooling, not part of the app: it scores stored notes against the rules a
program can check reliably. Judgment rules (cause ranking, merged windows,
plain English) are scored by hand in ``tests/fixtures/golden_notes/README.md``.

Run ``python -m tests.desk.golden`` from the repository root for the table.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from core.desk.lessons import TOPICS, _lead
from core.desk.writer import invented_numbers

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "golden_notes"

#: §7 length targets in words, over every draft field.
LENGTH_TARGETS: dict[str, tuple[int, int]] = {
    "premarket": (200, 260), "review": (200, 260),
    "move": (140, 180), "release": (140, 180), "fed": (140, 180),
    "earnings": (140, 180), "auction": (140, 180),
    "ask": (120, 160),
}
#: A lesson whose topic was taught in this many earlier notes counts as a repeat.
REPEAT_WINDOW = 5

_UP = r"rose|rises|rising|risen|up|gained|gains|added|adds|higher|jumped|climbed|firmed|rallied"
_DOWN = r"fell|falls|falling|fallen|down|lower|dropped|drops|slipped|slid|declined|lost|eased|sank"
_FILLER = r"(?:\s+(?:by|about|nearly|almost|roughly|around|just|only|another))*"
SIGNED = re.compile(rf"\b(?:{_UP}|{_DOWN})\b{_FILLER}\s+[+\-−]\d[\d,.]*(?:%|bp)?",
                    re.IGNORECASE)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
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


@dataclass
class Score:
    """Mechanical findings for one note. Fewer is better; zero is the goal."""

    note_id: str
    kind: str
    model: str
    words: int
    length_target: tuple[int, int] | None
    signed_direction: list[str] = field(default_factory=list)
    closed_as_stale: list[str] = field(default_factory=list)
    absent_not_missing: list[str] = field(default_factory=list)
    bp_as_percent: list[str] = field(default_factory=list)
    sign_errors: list[str] = field(default_factory=list)
    repeated_lesson: str | None = None
    #: Numbers not found in the stored facts. Not errors: the quote strip the
    #: writer saw is not stored, so these cannot be checked (an upper bound).
    unverifiable_numbers: list[str] = field(default_factory=list)

    @property
    def over_length(self) -> bool:
        return self.length_target is not None and self.words > self.length_target[1]

    @property
    def under_length(self) -> bool:
        """Too short to carry the note: a candidate cannot win by saying less."""
        return self.length_target is not None and self.words < self.length_target[0]

    @property
    def issues(self) -> int:
        """Rule breaks counted for the score (unverifiable numbers excluded)."""
        return (len(self.signed_direction) + len(self.closed_as_stale)
                + len(self.absent_not_missing) + len(self.bp_as_percent)
                + len(self.sign_errors) + int(self.repeated_lesson is not None)
                + int(self.over_length) + int(self.under_length))


def draft_fields(draft: dict) -> list[str]:
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


def sign_errors(text: str, moves: Iterable[dict]) -> list[str]:
    """A measured move quoted with the opposite direction word ('fell 0.88%' for +0.88%)."""
    # Several moves can share a magnitude (NQ -0.06%, DXY +0.06%); only a
    # direction that matches none of them is an error.
    signs: dict[str, set[int]] = {}
    for move in moves:
        change = move.get("change")
        magnitude = re.sub(r"^[+\-−]", "", str(move.get("text") or ""))
        if change and magnitude:
            signs.setdefault(magnitude, set()).add(1 if change > 0 else -1)
    found: list[str] = []
    for magnitude, allowed in signs.items():
        for hit in re.finditer(rf"(?<![\d.]){re.escape(magnitude)}", text):
            before = text[max(0, hit.start() - 60):hit.start()]
            word = _DIRECTION_BEFORE.search(before)
            if word is None:
                continue
            said = 1 if word.group("up") else -1
            if said not in allowed:
                found.append(f"{word.group(0).strip()} {magnitude}")
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


def repeated_lesson(learn: str | None, created_at: str, history: Sequence[dict],
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


def stored_facts_text(note: dict) -> str:
    return json.dumps({key: note.get(key) for key in
                       ("figures", "moves", "headlines", "upcoming", "rates", "event_at")},
                      ensure_ascii=False, default=str)


def score(fixture: dict, history: Sequence[dict]) -> Score:
    from core.desk.models import NoteDraft

    note = fixture["note"]
    draft = note["draft"]
    fields = draft_fields(draft)
    text = " ".join(fields)
    parsed = NoteDraft.model_validate(draft)
    return Score(
        note_id=fixture["id"], kind=fixture["kind"], model=fixture["model"],
        words=len(text.split()), length_target=LENGTH_TARGETS.get(fixture["kind"]),
        signed_direction=signed_direction(text), closed_as_stale=closed_as_stale(fields),
        absent_not_missing=absent_not_missing(fields), bp_as_percent=bp_as_percent(text),
        sign_errors=sign_errors(text, note.get("moves") or []),
        repeated_lesson=repeated_lesson(draft.get("learn"), note["created_at"], history,
                                        note_id=fixture["id"]),
        unverifiable_numbers=invented_numbers(parsed, stored_facts_text(note)),
    )


def load(version: str = "v4", directory: Path = GOLDEN) -> tuple[list[dict], list[dict]]:
    """One prompt version's notes (``<directory>/<version>/*.json``) and the lesson history.

    Each version has its own folder, so candidates never enter another version's
    baseline.
    """
    history = json.loads((directory / "lesson_history.json").read_text())
    fixtures = [json.loads(path.read_text())
                for path in sorted((directory / version).glob("*.json"))]
    return fixtures, history


def score_all(version: str = "v4", directory: Path = GOLDEN) -> list[Score]:
    fixtures, history = load(version, directory)
    return [score(fixture, history) for fixture in fixtures]


def summary(scores: Sequence[Score]) -> dict[str, dict]:
    """The baseline record: counts per note, for comparing prompt versions."""
    return {s.note_id: {"words": s.words, "over_length": s.over_length,
                        "under_length": s.under_length,
                        "signed_direction": len(s.signed_direction),
                        "closed_as_stale": len(s.closed_as_stale),
                        "absent_not_missing": len(s.absent_not_missing),
                        "bp_as_percent": len(s.bp_as_percent),
                        "sign_errors": len(s.sign_errors),
                        "repeated_lesson": s.repeated_lesson,
                        "unverifiable_numbers": len(s.unverifiable_numbers),
                        "issues": s.issues}
            for s in scores}


def _table(scores: Sequence[Score]) -> str:
    lines = ["note | model | words (target) | signed | closed≠stale | absent | bp% | sign | "
             "repeat | unverifiable | issues"]
    for s in scores:
        target = f"{s.length_target[0]}-{s.length_target[1]}" if s.length_target else "-"
        lines.append(f"{s.note_id} | {s.model.split('/')[-1]} | {s.words} ({target}) | "
                     f"{len(s.signed_direction)} | {len(s.closed_as_stale)} | "
                     f"{len(s.absent_not_missing)} | {len(s.bp_as_percent)} | "
                     f"{len(s.sign_errors)} | {s.repeated_lesson or '-'} | "
                     f"{len(s.unverifiable_numbers)} | {s.issues}")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - manual report
    scored = score_all()
    print(_table(scored))
    for s in scored:
        for name in ("signed_direction", "closed_as_stale", "absent_not_missing",
                     "bp_as_percent", "sign_errors", "unverifiable_numbers"):
            if getattr(s, name):
                print(f"{s.note_id} {name}: {getattr(s, name)}")
