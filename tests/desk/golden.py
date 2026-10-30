"""The desk:v4 golden set, scored by the evaluation checker (docs/PLAN.md §7).

The rules live in ``core/desk/evaluation/checker.py``; this module only adapts
the v4 fixtures (stored notes with partial facts) to it. Judgment rules are
scored by hand in ``tests/fixtures/golden_notes/README.md``.

Run ``python -m tests.desk.golden`` from the repository root for the table.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from core.desk.evaluation.cases import PARTIAL_FACT_KEYS
from core.desk.evaluation.checker import (  # noqa: F401 - re-exported for the tests
    Score,
    absent_not_missing,
    bp_as_percent,
    closed_as_stale,
    fact_moves,
    repeated_lesson,
    score_draft,
    sign_errors,
    signed_direction,
    summary_row,
    table,
)

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "golden_notes"


def stored_facts(note: dict) -> dict:
    """What a pre-capture note kept of its facts."""
    return {key: note.get(key) for key in PARTIAL_FACT_KEYS}


def score(fixture: dict, history: Sequence[dict]) -> Score:
    note = fixture["note"]
    return score_draft(case_id=fixture["id"], kind=fixture["kind"], model=fixture["model"],
                       created_at=note["created_at"], draft=note["draft"],
                       facts=stored_facts(note), facts_complete=False, history=history)


def load(version: str = "v4", directory: Path = GOLDEN) -> tuple[list[dict], list[dict]]:
    """One prompt version's notes (``<directory>/<version>/*.json``) and the lesson history."""
    history = json.loads((directory / "lesson_history.json").read_text())
    fixtures = [json.loads(path.read_text())
                for path in sorted((directory / version).glob("*.json"))]
    return fixtures, history


def score_all(version: str = "v4", directory: Path = GOLDEN) -> list[Score]:
    fixtures, history = load(version, directory)
    return [score(fixture, history) for fixture in fixtures]


def summary(scores: Sequence[Score]) -> dict[str, dict]:
    """The baseline record: counts per note, for comparing prompt versions."""
    return {s.case_id: summary_row(s) for s in scores}


if __name__ == "__main__":  # pragma: no cover - manual report
    scored = score_all()
    print(table(scored))
    for s in scored:
        for name, found in s.findings().items():
            print(f"{s.case_id} {name}: {found}")
