"""Results files and ``compare``: a candidate must be no worse on any rule, in any case.

A results file holds one draft per case (or the error that stopped it), tied to
one frozen case set by its hash. ``stored`` stands for the drafts the cases
were captured with.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.desk.evaluation.cases import CaseSet
from core.desk.evaluation.checker import RULES, Score, score_draft

STORED = "stored"
#: A case with a baseline draft but no candidate draft (failed, missing or emptied).
NO_DRAFT = "no_draft"


@dataclass
class Results:
    manifest_sha256: str
    label: str
    #: case id → {"model", "draft" (or None), "error", "cost_usd", ...}
    entries: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"manifest_sha256": self.manifest_sha256, "label": self.label,
                "entries": self.entries}


def stored_results(cases: CaseSet) -> Results:
    return Results(cases.sha256, STORED, {
        case.id: {"model": case.baseline.get("model") or "",
                  "prompt_version": case.baseline.get("prompt_version"),
                  "draft": case.baseline.get("draft")}
        for case in cases.cases})


def load_results(path: Path, cases: CaseSet) -> Results:
    """A results file for these cases (``stored`` for the captured drafts)."""
    if str(path) == STORED:
        return stored_results(cases)
    data = json.loads(Path(path).read_text())
    if data.get("manifest_sha256") != cases.sha256:
        raise ValueError(f"{path} was produced for other cases (manifest hash differs)")
    unknown = set(data.get("entries") or {}) - {c.id for c in cases.cases}
    if unknown:
        raise ValueError(f"{path} has drafts for unknown cases: {', '.join(sorted(unknown))}")
    return Results(data["manifest_sha256"], data.get("label") or Path(path).stem,
                   dict(data.get("entries") or {}))


def save_results(path: Path, results: Results) -> None:
    """Atomic write: a crash leaves the previous file whole."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(results.to_json(), indent=1, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def score_results(cases: CaseSet, results: Results) -> dict[str, Score | None]:
    """Each case's score, or ``None`` when it has no draft."""
    scores: dict[str, Score | None] = {}
    for case in cases.cases:
        entry: Mapping[str, Any] = results.entries.get(case.id) or {}
        draft = entry.get("draft")
        scores[case.id] = None if not draft else score_draft(
            case_id=case.id, kind=case.kind, model=entry.get("model") or "",
            created_at=case.created_at, draft=draft, facts=case.facts,
            facts_complete=case.facts_complete, history=cases.history)
    return scores


@dataclass(frozen=True)
class Change:
    case_id: str
    rule: str
    baseline: int
    candidate: int


@dataclass
class Comparison:
    baseline: str
    candidate: str
    regressions: list[Change]
    improvements: list[Change]
    #: rule → (baseline total, candidate total), over cases both drafted.
    totals: dict[str, tuple[int, int]]
    #: Cases with no baseline draft: nothing to compare against.
    skipped: list[str]

    @property
    def passed(self) -> bool:
        return not self.regressions

    def report(self) -> str:
        lines = [f"compare {self.baseline} → {self.candidate}",
                 "rule | baseline | candidate"]
        lines += [f"{rule} | {b} | {c}" for rule, (b, c) in self.totals.items()]
        for title, changes in (("REGRESSIONS", self.regressions),
                               ("improvements", self.improvements)):
            if changes:
                lines.append(f"{title}:")
                lines += [f"  {c.case_id} {c.rule}: {c.baseline} → {c.candidate}"
                          for c in changes]
        if self.skipped:
            lines.append(f"skipped (no baseline draft): {', '.join(self.skipped)}")
        lines.append("PASS: no rule got worse in any case" if self.passed
                     else f"FAIL: {len(self.regressions)} regression(s)")
        return "\n".join(lines)


def compare(cases: CaseSet, baseline: Results, candidate: Results) -> Comparison:
    """Per case and per rule: any count higher in the candidate is a regression."""
    base_scores = score_results(cases, baseline)
    cand_scores = score_results(cases, candidate)
    regressions: list[Change] = []
    improvements: list[Change] = []
    totals = {rule: [0, 0] for rule in (*RULES, NO_DRAFT)}
    skipped: list[str] = []
    for case in cases.cases:
        base, cand = base_scores[case.id], cand_scores[case.id]
        if base is None:
            skipped.append(case.id)
            continue
        if cand is None:
            regressions.append(Change(case.id, NO_DRAFT, 0, 1))
            totals[NO_DRAFT][1] += 1
            continue
        before, after = base.counts(), cand.counts()
        for rule in RULES:
            totals[rule][0] += before[rule]
            totals[rule][1] += after[rule]
            if after[rule] > before[rule]:
                regressions.append(Change(case.id, rule, before[rule], after[rule]))
            elif after[rule] < before[rule]:
                improvements.append(Change(case.id, rule, before[rule], after[rule]))
    return Comparison(baseline.label, candidate.label, regressions, improvements,
                      {rule: (b, c) for rule, (b, c) in totals.items()}, skipped)
