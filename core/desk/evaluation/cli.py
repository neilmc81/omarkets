"""``omarketsctl eval``: capture, rebuild, freeze, check, compare, render, sheet, tally, run.

Only ``run --paid`` makes paid calls; everything else is offline.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.desk.evaluation import cases as C
from core.desk.evaluation import checker
from core.desk.evaluation import compare as R
from core.desk.evaluation import sheet as S
from core.desk.evaluation.runner import EvalRunner, RunLedger, RunRefused
from core.desk.models import NoteKind
from core.desk.prompts import VERSIONS, NoPromptProfile

#: (settings, project directory) as the running service sees them.
SettingsFn = Callable[[], tuple[Any, Path]]


def add_parser(sub: Any) -> None:
    ev = sub.add_parser("eval", help="desk-note evaluation (offline unless run --paid)")
    cmd = ev.add_subparsers(dest="eval_command", required=True)
    p = cmd.add_parser("capture", help="copy stored notes and their facts into case files")
    p.add_argument("directory", type=Path)
    p.add_argument("note_ids", nargs="+")
    p.add_argument("--db", type=Path, help="desk database (default: the service's)")
    p = cmd.add_parser("rebuild", help="rebuild stored notes' full facts offline (no AI)")
    p.add_argument("directory", type=Path)
    p.add_argument("note_ids", nargs="+")
    p.add_argument("--copies", type=Path, required=True,
                   help="folder holding copies of desk.sqlite3, events.sqlite3, "
                        "economics-live.sqlite3 and omarkets-live.duckdb (+ .wal)")
    p = cmd.add_parser("freeze", help="record every case file's hash in manifest.json")
    p.add_argument("directory", type=Path)
    p.add_argument("--name", required=True)
    p = cmd.add_parser("check", help="score one results file (default: the stored drafts)")
    p.add_argument("directory", type=Path)
    p.add_argument("results", nargs="?", default=R.STORED)
    p = cmd.add_parser("compare", help="fail if the candidate is worse on any rule in any case")
    p.add_argument("directory", type=Path)
    p.add_argument("candidate")
    p.add_argument("--baseline", default=R.STORED)
    p = cmd.add_parser("render", help="write each case's exact prompt, offline")
    p.add_argument("directory", type=Path)
    p.add_argument("out", type=Path)
    p.add_argument("--model", help="one model for every case (default: production routing)")
    _prompt_arg(p)
    p = cmd.add_parser("sheet", help="blind hand-scoring sheet for two results files")
    p.add_argument("directory", type=Path)
    p.add_argument("first")
    p.add_argument("second")
    p.add_argument("out", type=Path)
    p.add_argument("--seed", type=int)
    p = cmd.add_parser("tally", help="totals from a filled-in scoring sheet")
    p.add_argument("sheet", type=Path)
    p = cmd.add_parser("run", help="plan (or, with --paid, make) one attempt per case")
    p.add_argument("directory", type=Path)
    p.add_argument("out", type=Path, help="results file")
    p.add_argument("--max-calls", type=int, required=True)
    p.add_argument("--allowance-usd", type=float, required=True)
    p.add_argument("--model", help="one model for every case (default: production routing)")
    p.add_argument("--run-id", help="default: the results file name")
    p.add_argument("--ledger", type=Path, help="run ledger (default: data dir)")
    p.add_argument("--paid", action="store_true", help="actually make the paid calls")
    _prompt_arg(p)


def _prompt_arg(p: Any) -> None:
    p.add_argument("--prompt", choices=VERSIONS,
                   help="prompt version (default: the service's desk_prompt_version)")


def _writer(settings: Any, guard: Any = None, *, api_key: str | None = None,
            prompt: str | None = None):
    from core.desk.writer import DeskWriter

    return DeskWriter(
        prompt_version=prompt or settings.desk_prompt_version,
        api_key=api_key, models={kind: settings.desk_model_for(kind) for kind in NoteKind},
        model_params=settings.resolved_desk_model_params,
        model_rates=settings.resolved_desk_model_rates,
        input_rate=settings.desk_input_rate, output_rate=settings.desk_output_rate,
        cost_guard=guard)


def _guard(settings: Any, project_dir: Path):
    from core.services.ai_budget import AICostGuard

    return AICostGuard(
        project_dir / settings.resolved_ai_budget_db_path,
        monthly_limit_usd=settings.ai_monthly_budget_usd,
        daily_limit_usd=settings.ai_daily_budget_usd,
        weekly_limit_usd=settings.ai_weekly_budget_usd,
        timezone=settings.ai_budget_timezone,
        openrouter_daily_request_limit=settings.ai_openrouter_daily_request_limit,
        openrouter_background_request_limit=settings.ai_openrouter_background_request_limit)


def _render(cases: C.CaseSet, out: Path, settings: Any, model: str | None,
            prompt_version: str | None = None) -> None:
    out.mkdir(parents=True, exist_ok=True)
    writer = _writer(settings, prompt=prompt_version)
    rates = settings.resolved_desk_model_rates
    index = [f"# Rendered prompts ({writer.prompt_version}, cases {cases.name})", "",
             "case | kind | facts | model | prompt chars | cost bound",
             "--- | --- | --- | --- | --- | ---"]
    for case in cases.cases:
        kind = NoteKind(case.kind)
        instrument = C.case_instrument(case)
        chosen = model or writer.model_for(kind)
        prompt = writer.prompt(kind, case.facts, instrument=instrument)
        (out / f"{C.file_name(case.id)[:-5]}.txt").write_text(
            f"SYSTEM\n{prompt.system}\n\nUSER\n{prompt.user}\n")
        bound = (f"${writer.max_cost(kind, case.facts, model=chosen, instrument=instrument):.4f}"
                 if chosen in rates else "no price on record")
        index.append(f"{case.id} | {case.kind} | "
                     f"{'complete' if case.facts_complete else 'partial'} | {chosen} | "
                     f"{len(prompt.system) + len(prompt.user)} | {bound}")
    (out / "index.md").write_text("\n".join(index) + "\n")


def _run(args: argparse.Namespace, cases: C.CaseSet, settings: Any, project_dir: Path) -> int:
    run_id = args.run_id or args.out.stem
    ledger = RunLedger(args.ledger or project_dir / settings.data_dir / "desk-eval-runs.sqlite3")
    guard = _guard(settings, project_dir)
    key = settings.openrouter_api_key
    api_key = key.get_secret_value() if hasattr(key, "get_secret_value") else key
    writer = _writer(settings, guard, api_key=api_key, prompt=args.prompt)
    print(f"prompt {writer.prompt_version}")
    runner = EvalRunner(writer=writer, guard=guard,
                        ledger=ledger, rates=settings.resolved_desk_model_rates, model=args.model)
    try:
        planned = runner.plan(cases, run_id=run_id, max_calls=args.max_calls,
                              allowance_usd=args.allowance_usd)
        calls, committed = ledger.usage(run_id)
        print(f"run {run_id}: {len(planned)} call(s) planned; used so far {calls} call(s), "
              f"${committed / 1e6:.4f}; limits {args.max_calls} calls, "
              f"${args.allowance_usd:.4f}")
        for call in planned:
            print(f"  {call.case.id:<44} {call.model:<40} bound ${call.bound_usd:.4f}")
        print(f"  worst case ${sum(c.bound_usd for c in planned):.4f}")
        if not args.paid:
            print("plan only: no calls made (add --paid to run)")
            return 0
        if args.out.exists() and ledger.limits(run_id) is None:
            raise RunRefused(f"{args.out} exists but run {run_id} is not in {ledger.path}; "
                             "the ledger may be lost, so these cases may already be paid for")
        results = asyncio.run(runner.run(cases, run_id=run_id, max_calls=args.max_calls,
                                         allowance_usd=args.allowance_usd, out=args.out))
        calls, committed = ledger.usage(run_id)
        print(f"wrote {len(results.entries)} result(s) to {args.out}; run total "
              f"{calls} call(s), ${committed / 1e6:.4f}")
        return 0
    except RunRefused as exc:
        print(f"omarketsctl: eval run refused: {exc}", file=sys.stderr)
        return 1
    finally:
        ledger.close()


def main(args: argparse.Namespace, service_settings: SettingsFn) -> int:
    try:
        return _main(args, service_settings)
    except (C.ManifestError, FileNotFoundError, FileExistsError, KeyError, ValueError,
            NoPromptProfile) as exc:
        print(f"omarketsctl: eval {args.eval_command}: {exc}", file=sys.stderr)
        return 1


def _main(args: argparse.Namespace, service_settings: SettingsFn) -> int:
    command = args.eval_command
    if command == "capture":
        if args.db is None:
            settings, project_dir = service_settings()
            db = project_dir / settings.resolved_desk_db_path
        else:
            db = args.db
        found, history = C.capture(db, args.note_ids)
        C.write_cases(args.directory, found, history)
        for case in found:
            print(f"{case.id}: {'complete' if case.facts_complete else 'partial'} facts")
        print(f"captured {len(found)} case(s) into {args.directory}; review, then freeze")
        return 0
    if command == "rebuild":
        from core.desk.evaluation import rebuild as RB

        settings, _ = service_settings()
        copies = args.copies
        sources = RB.live_sources(
            market_db=copies / "omarkets-live.duckdb", events_db=copies / "events.sqlite3",
            economics_db=copies / "economics-live.sqlite3", work=copies / "work",
            policy=RB.stale_policy(settings))
        found = RB.rebuild(args.note_ids, desk_db=copies / "desk.sqlite3", sources_at=sources)
        _, history = C.capture(copies / "desk.sqlite3", [])
        C.write_cases(args.directory, found, history)
        for case in found:
            print(f"{case.id}: facts rebuilt at {case.envelope['reconstructed']['built_at']}")
        print(f"rebuilt {len(found)} case(s) into {args.directory}; review, then freeze")
        return 0
    if command == "freeze":
        manifest = C.freeze(args.directory, args.name)
        print(f"froze {len(manifest['cases'])} case(s): {manifest['sha256']}")
        return 0
    if command == "tally":
        for label, totals in S.tally(args.sheet).items():
            print(f"{label}: " + ", ".join(f"{k} {v}" for k, v in totals.items())
                  + f" (total {sum(totals.values())})")
        return 0
    cases = C.load(args.directory)
    if command == "check":
        scores = [s for s in R.score_results(cases, R.load_results(args.results, cases)).values()
                  if s is not None]
        print(checker.table(scores))
        for s in scores:
            for name, found in s.findings().items():
                print(f"{s.case_id} {name}: {json.dumps(found, ensure_ascii=False)}")
        return 0
    if command == "compare":
        result = R.compare(cases, R.load_results(args.baseline, cases),
                           R.load_results(args.candidate, cases))
        print(result.report())
        return 0 if result.passed else 1
    if command == "sheet":
        S.write_sheet(args.out, cases, R.load_results(args.first, cases),
                      R.load_results(args.second, cases), seed=args.seed)
        print(f"wrote {args.out / S.SHEET} and {args.out / S.SCORES}; "
              f"do not open {S.KEY} until scored")
        return 0
    settings, project_dir = service_settings()
    if command == "render":
        _render(cases, args.out, settings, args.model, args.prompt)
        print(f"rendered {len(cases.cases)} prompt(s) into {args.out} (no calls made)")
        return 0
    return _run(args, cases, settings, project_dir)
