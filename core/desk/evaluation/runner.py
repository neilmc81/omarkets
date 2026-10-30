"""The evaluation runner: one paid attempt per case, under two independent limits.

1. Its own run ledger (SQLite): a hard call count and a dollar allowance per
   run, fixed when the run is created. Every attempt is recorded *before* the
   request is sent, so a crash or restart can never repeat a case or lift a
   limit; an attempt whose cost is unknown keeps its full bound. The bound is
   a ceiling, not an estimate (``DeskWriter.hard_max_cost``). Each result is
   stored in the ledger too, and the results file is always rebuilt from it,
   so a second process or a stale copy can't lose a paid draft.
2. The shared AI cost guard (the weekly $2.50 ledger): every call reserves its
   bound there too, exactly as desk notes do.

No fallback model, no regeneration (``DeskWriter.attempt``). The runner refuses
to start when a cost bound can't be set: a model with no price on record, no
shared ledger with spending limits, partial facts, or a plan that doesn't fit
the remaining calls and dollars.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.desk.evaluation.cases import Case, CaseSet, case_instrument
from core.desk.evaluation.compare import Results, save_results
from core.desk.identity import Instrument
from core.desk.models import NoteKind
from core.desk.prompts import PROMPT_VERSION, NoPromptProfile
from core.desk.writer import DeskWriter

#: Recorded before the request; left as-is if the process dies mid-call.
PENDING = "pending"
DONE = "done"
#: The request may have been billed (network error): the bound stays held.
UNKNOWN = "unknown"
#: The shared cost guard refused before anything was sent: not an attempt.
REFUSED = "refused"


#: Provider errors the writer settles at $0 (``DeskWriter._call``).
_UNBILLED = frozenset(f"provider returned HTTP {code}" for code in (400, 401, 402, 403, 404))


class RunRefused(RuntimeError):
    """The run cannot start (or continue) within its limits."""


def _micro(usd: float) -> int:
    if not math.isfinite(usd) or usd < 0:
        raise RunRefused(f"invalid dollar amount {usd!r}")
    return math.ceil(usd * 1_000_000)


@dataclass(frozen=True)
class RunLimits:
    manifest_sha256: str
    max_calls: int
    allowance_micro: int


class RunLedger:
    """Per-run call and dollar accounting that survives restarts."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=5)
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS eval_runs ("
                " run_id TEXT PRIMARY KEY, manifest_sha256 TEXT NOT NULL,"
                " max_calls INTEGER NOT NULL, allowance_micro INTEGER NOT NULL,"
                " created_at TEXT NOT NULL)")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS eval_calls ("
                " run_id TEXT NOT NULL REFERENCES eval_runs(run_id), case_id TEXT NOT NULL,"
                " model TEXT NOT NULL, reserved_micro INTEGER NOT NULL, actual_micro INTEGER,"
                " status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,"
                " entry TEXT, PRIMARY KEY (run_id, case_id))")

    def limits(self, run_id: str) -> RunLimits | None:
        with self._lock:
            row = self._db.execute("SELECT manifest_sha256, max_calls, allowance_micro "
                                   "FROM eval_runs WHERE run_id=?", (run_id,)).fetchone()
        return RunLimits(*row) if row else None

    def open_run(self, run_id: str, limits: RunLimits) -> None:
        """Create the run, or check a restart asks for exactly the same limits."""
        with self._lock, self._db:
            self._db.execute("INSERT OR IGNORE INTO eval_runs VALUES (?,?,?,?,?)",
                             (run_id, limits.manifest_sha256, limits.max_calls,
                              limits.allowance_micro, datetime.now(UTC).isoformat()))
        recorded = self.limits(run_id)
        if recorded != limits:
            raise RunRefused(f"run {run_id} was created with {recorded}; a restart cannot "
                             "change its cases or limits")

    def usage(self, run_id: str) -> tuple[int, int]:
        """(calls made, dollars committed in micro-USD): actual cost, else the bound."""
        with self._lock:
            return self._usage(run_id)

    def _usage(self, run_id: str) -> tuple[int, int]:
        row = self._db.execute(
            "SELECT COUNT(*), COALESCE(SUM(COALESCE(actual_micro, reserved_micro)), 0) "
            "FROM eval_calls WHERE run_id=? AND status!=?", (run_id, REFUSED)).fetchone()
        return int(row[0]), int(row[1])

    def attempted(self, run_id: str) -> dict[str, str]:
        """Cases already attempted (any outcome but a refusal) → status."""
        with self._lock:
            rows = self._db.execute("SELECT case_id, status FROM eval_calls "
                                    "WHERE run_id=? AND status!=?", (run_id, REFUSED)).fetchall()
        return dict(rows)

    def begin(self, run_id: str, case_id: str, model: str, bound_usd: float) -> None:
        """Admit one attempt, atomically against the run's limits, before sending it."""
        bound = _micro(bound_usd)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                limits = self._db.execute(
                    "SELECT max_calls, allowance_micro FROM eval_runs WHERE run_id=?",
                    (run_id,)).fetchone()
                if limits is None:
                    raise RunRefused(f"unknown run {run_id}")
                prior = self._db.execute(
                    "SELECT status FROM eval_calls WHERE run_id=? AND case_id=?",
                    (run_id, case_id)).fetchone()
                if prior and prior[0] != REFUSED:
                    raise RunRefused(f"{case_id} was already attempted in run {run_id}")
                calls, committed = self._usage(run_id)
                if calls >= limits[0]:
                    raise RunRefused(f"run {run_id} has used all {limits[0]} calls")
                if committed + bound > limits[1]:
                    raise RunRefused(
                        f"run {run_id}: ${(committed + bound) / 1e6:.4f} would exceed its "
                        f"${limits[1] / 1e6:.4f} allowance")
                self._db.execute(
                    "INSERT OR REPLACE INTO eval_calls VALUES (?,?,?,?,NULL,?,?,NULL,NULL)",
                    (run_id, case_id, model, bound, PENDING, datetime.now(UTC).isoformat()))
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def finish(self, run_id: str, case_id: str, status: str, cost_usd: float | None,
               entry: dict[str, Any]) -> None:
        """Record the outcome and the result itself (the draft, or the error)."""
        actual = None if cost_usd is None else _micro(cost_usd)
        with self._lock, self._db:
            self._db.execute(
                "UPDATE eval_calls SET status=?, actual_micro=?, finished_at=?, entry=? "
                "WHERE run_id=? AND case_id=? AND status=?",
                (status, actual, datetime.now(UTC).isoformat(),
                 json.dumps(entry, ensure_ascii=False), run_id, case_id, PENDING))

    def results(self, run_id: str, manifest_sha256: str, label: str) -> Results:
        """Every attempted case's result; an attempt with no outcome is marked interrupted."""
        with self._lock:
            rows = self._db.execute(
                "SELECT case_id, model, entry FROM eval_calls WHERE run_id=? AND status!=? "
                "ORDER BY started_at, case_id", (run_id, REFUSED)).fetchall()
        return Results(manifest_sha256, label, {
            case_id: json.loads(entry) if entry else {
                "model": model, "draft": None,
                "error": "attempt interrupted; not retried (one per case)"}
            for case_id, model, entry in rows})

    def close(self) -> None:
        with self._lock:
            self._db.close()


@dataclass(frozen=True)
class PlannedCall:
    case: Case
    model: str
    bound_usd: float
    #: From the case's envelope; ``None`` for desk-wide event notes.
    instrument: Instrument | None = None


class EvalRunner:
    def __init__(self, *, writer: DeskWriter, guard: Any, ledger: RunLedger,
                 rates: Mapping[str, tuple[float, float]], model: str | None = None) -> None:
        self._writer = writer
        self._guard = guard
        self._ledger = ledger
        #: Only models with a price on record can be bounded.
        self._rates = dict(rates)
        #: One model for every case; otherwise the production routing per kind.
        self._model = model

    def plan(self, cases: CaseSet, *, run_id: str, max_calls: int,
             allowance_usd: float) -> list[PlannedCall]:
        """The calls still to make, or ``RunRefused`` if any limit can't be honoured."""
        if not isinstance(max_calls, int) or max_calls < 1:
            raise RunRefused("--max-calls must be a positive whole number")
        allowance = _micro(allowance_usd)
        if allowance <= 0:
            raise RunRefused("--allowance-usd must be positive")
        if self._guard is None or not self._guard.spending_limits_enabled:
            raise RunRefused("no shared AI cost ledger with spending limits; refusing to run")
        if not self._writer.ready:
            raise RunRefused(self._writer.detail or "desk writer not ready")
        limits = RunLimits(cases.sha256, max_calls, allowance)
        recorded = self._ledger.limits(run_id)
        if recorded is not None and recorded != limits:
            raise RunRefused(f"run {run_id} was created with {recorded}; a restart cannot "
                             "change its cases or limits")
        done = self._ledger.attempted(run_id)
        planned = []
        for case in cases.cases:
            if case.id in done:
                continue
            if not case.facts_complete:
                raise RunRefused(f"{case.id} has partial facts; only captured cases can run")
            kind = NoteKind(case.kind)
            instrument = case_instrument(case)
            model = self._model or self._writer.model_for(kind)
            if model not in self._rates:
                raise RunRefused(f"no price on record for {model}; can't bound its cost")
            try:
                bound = self._writer.hard_max_cost(kind, case.facts, model=model,
                                                   instrument=instrument)
            except NoPromptProfile as exc:
                raise RunRefused(f"{case.id}: {exc}") from exc
            if bound is None or not math.isfinite(bound) or bound <= 0:
                raise RunRefused(f"{case.id}: cost bound could not be set for {model} "
                                 "(its routing params change the token limit)")
            planned.append(PlannedCall(case, model, bound, instrument))
        calls, committed = self._ledger.usage(run_id)
        if calls + len(planned) > max_calls:
            raise RunRefused(f"{len(planned)} calls planned but only {max_calls - calls} "
                             f"of {max_calls} left in run {run_id}")
        needed = sum(_micro(call.bound_usd) for call in planned)
        if committed + needed > allowance:
            raise RunRefused(f"worst case ${(committed + needed) / 1e6:.4f} exceeds the run's "
                             f"${allowance / 1e6:.4f} allowance")
        return planned

    async def run(self, cases: CaseSet, *, run_id: str, max_calls: int, allowance_usd: float,
                  out: Path) -> Results:
        """Attempt every remaining case once; ``out`` is rewritten from the ledger
        after each call."""
        planned = self.plan(cases, run_id=run_id, max_calls=max_calls,
                            allowance_usd=allowance_usd)
        self._ledger.open_run(run_id, RunLimits(cases.sha256, max_calls, _micro(allowance_usd)))
        label = f"{PROMPT_VERSION} run {run_id}"

        def saved() -> Results:
            results = self._ledger.results(run_id, cases.sha256, label)
            save_results(out, results)
            return results

        results = saved()
        for call in planned:
            case = call.case
            self._ledger.begin(run_id, case.id, call.model, call.bound_usd)
            result = await self._writer.attempt(NoteKind(case.kind), case.facts,
                                                instrument=call.instrument, model=call.model)
            error = result.error or ""
            # The cost guard refused, or the provider rejected the request
            # without billing it (bad key, no credit, unknown model): the writer
            # settles those at $0. Not an attempt; the run stops.
            if error.startswith("AI budget") or error in _UNBILLED:
                status = REFUSED
            elif error.startswith("network error"):
                status = UNKNOWN
            else:
                status = DONE
            self._ledger.finish(run_id, case.id, status,
                                0.0 if status == REFUSED else result.cost_usd, {
                "model": result.model, "prompt_version": PROMPT_VERSION,
                "draft": result.draft.model_dump(mode="json") if result.draft else None,
                "error": result.error, "cost_usd": result.cost_usd,
                "bound_usd": call.bound_usd,
                "unverified_numbers": list(result.unverified_numbers),
            })
            results = saved()
            if status == REFUSED:
                raise RunRefused(f"{case.id} was not sent or was rejected unbilled: {error}")
            if result.cost_usd is not None and result.cost_usd > call.bound_usd:
                raise RunRefused(f"{case.id} cost ${result.cost_usd:.4f}, above its bound; "
                                 "stopping")
        return results
