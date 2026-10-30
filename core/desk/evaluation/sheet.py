"""Blind hand-scoring: two sets of drafts per case under shuffled letters.

``write_sheet`` makes three files: ``sheet.md`` (facts and the two notes for
each case, labelled A and B in a random order per case), ``scores.csv`` (one
row per note to fill in) and ``key.json`` (which letter is which version: do
not open it until scoring is done). ``tally`` reads the filled CSV with the key.
"""

from __future__ import annotations

import csv
import json
import random
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from core.desk.evaluation.cases import CaseSet
from core.desk.evaluation.compare import Results
from core.desk.prompts import facts_text

SHEET, SCORES, KEY = "sheet.md", "scores.csv", "key.json"
#: Scored 0 (breaks it), 1 (partly) or 2 (follows it).
CRITERIA: dict[str, str] = {
    "coverage": "Covers what the task asks for, using the facts supplied, without padding.",
    "attribution": "Honest about cause: no ranking of causes, unconfirmed links labelled, "
                   "nothing claimed beyond the facts.",
    "windows": "Keeps measurement windows and times apart (today vs overnight vs the "
               "release window; yesterday only when the dates match); closed, stale and "
               "unavailable kept distinct.",
    "usefulness": "A new trader understands what happened, why it matters and what to "
                  "watch, in plain English.",
}
LETTERS = ("A", "B")


def _note_text(entry: Mapping[str, Any] | None) -> str:
    draft = (entry or {}).get("draft")
    if not draft:
        return f"_(no note: {(entry or {}).get('error') or 'not written'})_"
    tone = draft.get("tone") or draft.get("nq_tone")
    parts = [f"**{draft.get('headline', '')}**", "", draft.get("summary", ""), "",
             f"*What it means.* {draft.get('what_it_means', '')}", "",
             f"*Market reaction.* {draft.get('market_reaction', '')}"]
    if draft.get("rates_read"):
        parts += ["", f"*Rates.* {draft['rates_read']}"]
    parts += ["", "*Watch next.*", *[f"- {item}" for item in draft.get("watch_next") or []],
              "", f"*Learn.* {draft.get('learn', '')}", "", f"Tone: {tone}"]
    return "\n".join(parts)


def write_sheet(directory: Path, cases: CaseSet, first: Results, second: Results,
                *, seed: int | None = None) -> None:
    """The blind sheet for two results sets. Refuses to overwrite a started sheet."""
    if any((directory / name).exists() for name in (SHEET, SCORES, KEY)):
        raise FileExistsError(f"{directory} already has a sheet; use a new folder")
    directory.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed if seed is not None else random.SystemRandom().getrandbits(64))
    key: dict[str, dict[str, str]] = {}
    lines = ["# Blind scoring sheet", "",
             "Score each note 0 (breaks it), 1 (partly) or 2 (follows it) in "
             f"`{SCORES}`. Do not open `{KEY}` until every score is filled in.", ""]
    lines += [f"- **{name}**: {text}" for name, text in CRITERIA.items()]
    rows = []
    for case in cases.cases:
        order = [first, second]
        rng.shuffle(order)
        key[case.id] = {letter: results.label for letter, results in zip(LETTERS, order,
                                                                           strict=True)}
        lines += ["", "---", "", f"## {case.title} (`{case.id}`, {case.kind})", "",
                  "<details><summary>Facts the writer was given"
                  + ("" if case.facts_complete else " (partial: only the fields older notes kept)")
                  + "</summary>", "", "```json", facts_text(case.facts), "```", "",
                  "</details>"]
        for letter, results in zip(LETTERS, order, strict=True):
            lines += ["", f"### Note {letter}", "", _note_text(results.entries.get(case.id))]
            rows.append({"case": case.id, "note": letter, **dict.fromkeys(CRITERIA, ""),
                         "comment": ""})
    (directory / SHEET).write_text("\n".join(lines) + "\n")
    with (directory / SCORES).open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["case", "note", *CRITERIA, "comment"])
        writer.writeheader()
        writer.writerows(rows)
    (directory / KEY).write_text(json.dumps({"manifest_sha256": cases.sha256, "key": key},
                                            indent=1) + "\n")


def tally(directory: Path) -> dict[str, dict[str, int]]:
    """Totals per version and criterion. Every score must be 0, 1 or 2."""
    key = json.loads((directory / KEY).read_text())["key"]
    totals: dict[str, dict[str, int]] = {}
    expected = {(case, letter) for case, letters in key.items() for letter in letters}
    seen: set[tuple[str, str]] = set()
    with (directory / SCORES).open(newline="") as handle:
        for row in csv.DictReader(handle):
            place = (row.get("case") or "", row.get("note") or "")
            if place not in expected or place in seen:
                raise ValueError(f"unexpected or repeated row: {place[0]} note {place[1]}")
            seen.add(place)
            label = key[row["case"]][row["note"]]
            for criterion in CRITERIA:
                value = (row.get(criterion) or "").strip()
                if value not in {"0", "1", "2"}:
                    raise ValueError(f"{row['case']} note {row['note']}: {criterion} "
                                     f"must be 0, 1 or 2 (got {value!r})")
                totals.setdefault(label, dict.fromkeys(CRITERIA, 0))[criterion] += int(value)
    missing = sorted(expected - seen)
    if missing:
        raise ValueError("unscored notes: " + ", ".join(f"{c} note {n}" for c, n in missing))
    return totals
