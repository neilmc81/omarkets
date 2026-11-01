"""Phase 1 slice 2: the evaluation checker, frozen cases, compare, blind sheet and runner.

Gates: a worse or emptier draft fails ``compare``; mocked runs prove the run's
call count and dollar allowance hold across restarts, alongside the shared
weekly ledger. No test makes a network call. The cases and writers here are
desk:v8 (pinned, since production now defaults to desk:v9).
"""

from __future__ import annotations

import copy
import csv
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from core import cli
from core.config.settings import Settings
from core.desk.evaluation import cases as C
from core.desk.evaluation import checker as K
from core.desk.evaluation import compare as R
from core.desk.evaluation import sheet as S
from core.desk.evaluation.runner import (
    DONE,
    PENDING,
    UNKNOWN,
    EvalRunner,
    RunLedger,
    RunRefused,
)
from core.desk.identity import NQ
from core.desk.models import NoteDraft, NoteKind
from core.desk.prompts import SYSTEM, V8, facts_text, render
from core.desk.writer import DeskWriter, invented_numbers
from core.services.ai_budget import AICostGuard
from tests.desk import golden as G
from tests.desk.test_writer_and_api import FACTS, GOOD, completion

SHIPPED = Path(__file__).parents[1] / "fixtures" / "eval" / "golden-v4"
MODEL = "test/model"
RATES = {MODEL: (1.0, 5.0)}

# --------------------------------------------------------------------------- checker

LABELLED = {
    "markets_today": [
        {"market": "Nasdaq 100 futures", "level": "29,000.00", "change_today": "+0.06%"},
        {"market": "VIX fear gauge", "level": "18.00", "change_today": "-0.06%"},
        {"market": "Crude oil", "level": "$70.00", "change_today": "+0.88%"},
    ],
    "bond_market": {"yields": [{"name": "2-year yield", "level": "4.100%",
                                "change_today": "-1.7bp", "change_past_week": "+3.0bp"}]},
}


def test_fact_moves_read_each_instrument_from_any_facts_shape():
    moves = {(m.instrument, m.magnitude, m.sign, m.unit) for m in K.fact_moves(LABELLED)}
    assert moves == {("NQ", "0.06", 1, "%"), ("VIX", "0.06", -1, "%"), ("WTI", "0.88", 1, "%"),
                     ("US2Y", "1.7", -1, "bp"), ("US2Y", "3.0", 1, "bp")}
    # A curve text is a description, not a signed change.
    assert K.fact_moves({"curve": "+12bp, normal (10-year above 2-year)"}) == []


def test_sign_checks_are_per_instrument():
    # Before slice 2 both passed: some instrument moved 0.06% each way.
    assert K.sign_errors("NQ slipped 0.06% and the VIX rose 0.06%.", LABELLED) == [
        "slipped 0.06%", "rose 0.06%"]
    assert K.sign_errors("NQ rose 0.06% while the VIX eased 0.06%.", LABELLED) == []
    # Roundings count; the 2-year in basis points; a name inside a longer one.
    assert K.sign_errors("Oil fell 0.9%.", LABELLED) == ["fell 0.9%"]
    assert K.sign_errors("The 2-year yield rose 1.7bp.", LABELLED) == ["rose 1.7bp"]
    assert K.sign_errors("The Nasdaq fear gauge rose 0.06%.", {
        "markets_today": [{"market": "Nasdaq fear gauge", "change_today": "-0.06%"},
                          {"market": "Nasdaq 100 futures", "change_today": "+0.06%"}]}) == [
        "rose 0.06%"]
    # No instrument named in the clause: any instrument's change may justify it.
    assert K.sign_errors("Something slipped 0.06%.", LABELLED) == []
    # A unit that doesn't match the fact is a different number.
    assert K.sign_errors("The 2-year rose 1.7%.", LABELLED) == []


def test_numbers_are_checked_against_the_facts_only():
    # The system prompt's own example numbers no longer count as allowed.
    assert "max 45 words" in SYSTEM
    draft = NoteDraft.model_validate({**GOOD, "summary": "In 45 words: NQ slipped 0.18%."})
    assert invented_numbers(draft, facts_text(FACTS)) == ["45"]
    # Digits inside names are not figures.
    named = NoteDraft.model_validate({
        **GOOD, "summary": "The Nasdaq 100 and S&P 500 waited on the 10-year and 2y."})
    assert invented_numbers(named, facts_text(FACTS)) == []


def test_invented_numbers_count_only_with_complete_facts():
    draft = {**GOOD, "summary": "NQ slipped 0.18% after a 777 point swing."}
    args = dict(case_id="c", kind="ask", model="m", created_at="2026-10-30",
                draft=draft, facts=FACTS, history=[])
    partial = K.score_draft(**args, facts_complete=False)
    complete = K.score_draft(**args, facts_complete=True)
    assert partial.unverifiable_numbers == ["777"] and partial.counts()["invented_numbers"] == 0
    assert complete.invented_numbers == ["777"] and complete.counts()["invented_numbers"] == 1
    assert complete.issues == partial.issues + 1


async def test_production_writer_regenerates_on_a_prompt_only_number():
    from tests.desk.test_writer_and_api import split_writer

    bad = {**GOOD, "summary": "Kept to 45 words."}
    sent = []

    def handler(request):
        sent.append(1)
        return completion(json.dumps(bad if len(sent) == 1 else GOOD))

    result = await split_writer(handler).write(NoteKind.MOVE, FACTS, instrument=NQ)
    assert len(sent) == 2 and result.unverified_numbers == ()


async def test_attempt_is_one_call_without_fallback_or_regeneration():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content)["model"])
        return completion(json.dumps({**GOOD, "summary": "A 777 point swing."}))

    writer = DeskWriter(api_key="k", model=MODEL, fallback_model="other/model",
                        model_rates=RATES, client=httpx.AsyncClient(
                            transport=httpx.MockTransport(handler)))
    result = await writer.attempt(NoteKind.MOVE, FACTS, instrument=NQ)
    assert sent == [MODEL] and result.unverified_numbers == ("777",)
    sent.clear()
    writer._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: sent.append(1) or completion("not json")))
    result = await writer.attempt(NoteKind.MOVE, FACTS, instrument=NQ)
    assert result.draft is None and sent == [1]


# --------------------------------------------------------------------------- manifest


def complete_case(case_id: str, kind: str = "ask") -> C.Case:
    return C.Case(id=case_id, kind=kind, title=f"Case {case_id}",
                  created_at="2026-10-30T10:00:00+00:00", facts=dict(FACTS),
                  facts_complete=True, envelope={"prompt_version": "desk:v8"},
                  baseline={"prompt_version": "desk:v8", "model": MODEL, "draft": dict(GOOD)})


def frozen(tmp_path: Path, cases: list[C.Case]) -> C.CaseSet:
    directory = tmp_path / "cases"
    C.write_cases(directory, cases, [])
    C.freeze(directory, "test")
    return C.load(directory)


def test_shipped_cases_are_frozen_and_match_the_golden_set():
    cases = C.load(SHIPPED)
    assert [c.id for c in cases.cases] == sorted(f["id"] for f in G.load()[0])
    golden = G.summary(G.score_all())
    stored = R.score_results(cases, R.stored_results(cases))
    # The live history is longer, but only notes written before a case count.
    assert {cid: K.summary_row(s) for cid, s in stored.items()} == golden


def test_a_changed_missing_or_extra_case_file_is_refused(tmp_path):
    cases = frozen(tmp_path, [complete_case("ask:1"), complete_case("ask:2")])
    directory = cases.directory
    path = directory / C.CASES / C.file_name("ask:1")
    original = path.read_text()
    path.write_text(original.replace("Case ask:1", "Case ask:one"))
    with pytest.raises(C.ManifestError, match="ask_1.json"):
        C.load(directory)
    path.unlink()
    with pytest.raises(C.ManifestError, match="ask_1.json"):
        C.load(directory)
    path.write_text(original)
    (directory / C.CASES / "stray.json").write_text(path.read_text().replace("ask:1", "x"))
    with pytest.raises(C.ManifestError, match="stray.json"):
        C.load(directory)
    (directory / C.CASES / "stray.json").unlink()
    assert C.load(directory).sha256 == cases.sha256
    manifest = json.loads((directory / C.MANIFEST).read_text())
    manifest["name"] = "renamed"
    (directory / C.MANIFEST).write_text(json.dumps(manifest))
    with pytest.raises(C.ManifestError, match="edited"):
        C.load(directory)
    with pytest.raises(C.ManifestError, match="already exists"):
        C.freeze(directory, "again")
    with pytest.raises(C.ManifestError, match="frozen"):
        C.write_cases(directory, [complete_case("ask:3")], [])


def test_capture_reads_full_facts_when_stored_and_never_writes(tmp_path):
    from datetime import UTC, datetime

    from core.desk.models import DeskNote
    from core.desk.store import DeskStore

    db = tmp_path / "desk.sqlite3"
    store = DeskStore(db)
    at = datetime(2026, 10, 30, 12, tzinfo=UTC)
    store.save(DeskNote(id="ask:new", kind=NoteKind.ASK, title="Now", created_at=at,
                        status="ok", draft=NoteDraft.model_validate(GOOD),
                        model=f"{MODEL} · desk:v8"), FACTS, {"prompt_version": "desk:v8"})
    store.close()
    before = db.read_bytes()
    found, history = C.capture(db, ["ask:new"])
    assert db.read_bytes() == before
    case = found[0]
    assert case.facts == FACTS and case.facts_complete
    assert case.baseline == {"prompt_version": "desk:v8", "model": MODEL,
                             "draft": NoteDraft.model_validate(GOOD).model_dump(mode="json")}
    assert history == [{"id": "ask:new", "kind": "ask", "created_at": at.isoformat(),
                        "learn": GOOD["learn"]}]
    with pytest.raises(KeyError):
        C.capture(db, ["ask:missing"])


# --------------------------------------------------------------------------- compare gate


def candidate(cases: C.CaseSet, change) -> R.Results:
    results = R.stored_results(cases)
    results = R.Results(results.manifest_sha256, "candidate", copy.deepcopy(results.entries))
    change(results.entries)
    return results


def test_compare_passes_an_identical_candidate():
    cases = C.load(SHIPPED)
    result = R.compare(cases, R.stored_results(cases), candidate(cases, lambda e: None))
    assert result.passed and not result.improvements


def test_compare_fails_a_worse_draft():
    cases = C.load(SHIPPED)

    def worse(entries):
        draft = entries["move:20261027T11"]["draft"]
        draft["market_reaction"] += " NQ rose +0.88% in the window."

    result = R.compare(cases, R.stored_results(cases), candidate(cases, worse))
    assert not result.passed
    assert [(c.case_id, c.rule) for c in result.regressions] == [
        ("move:20261027T11", "signed_direction")]


def test_compare_fails_an_emptier_draft_or_a_missing_one():
    cases = C.load(SHIPPED)

    def emptier(entries):
        draft = entries["release:20261027T1230:DURABLE_GOODS"]["draft"]
        for key in ("what_it_means", "market_reaction", "rates_read", "learn"):
            draft[key] = "Not known."
        draft["watch_next"] = []
        del entries["ask:20261024T074748"]

    result = R.compare(cases, R.stored_results(cases), candidate(cases, emptier))
    assert {(c.case_id, c.rule) for c in result.regressions} == {
        ("release:20261027T1230:DURABLE_GOODS", "under_length"),
        ("ask:20261024T074748", R.NO_DRAFT)}
    # Better on one rule never buys a regression on another.
    assert any(c.rule == "over_length" for c in result.improvements)


def test_compare_command_exit_codes_and_foreign_results(tmp_path, capsys):
    cases = C.load(SHIPPED)
    good, bad = tmp_path / "good.json", tmp_path / "bad.json"
    R.save_results(good, candidate(cases, lambda e: None))

    def worse(entries):
        entries["ask:20261024T074748"]["draft"]["summary"] += " There was no event today."

    R.save_results(bad, candidate(cases, worse))
    assert cli.main(["eval", "compare", str(SHIPPED), str(good)]) == 0
    assert cli.main(["eval", "compare", str(SHIPPED), str(bad)]) == 1
    assert "absent_not_missing: 0 → 1" in capsys.readouterr().out
    foreign = json.loads(good.read_text()) | {"manifest_sha256": "0" * 64}
    good.write_text(json.dumps(foreign))
    assert cli.main(["eval", "compare", str(SHIPPED), str(good)]) == 1
    assert "other cases" in capsys.readouterr().err


# --------------------------------------------------------------------------- blind sheet


def test_blind_sheet_hides_versions_and_tallies(tmp_path):
    cases = C.load(SHIPPED)
    first = replace(R.stored_results(cases), label="version-one")
    second = replace(candidate(cases, lambda e: None), label="version-two")
    out = tmp_path / "sheet"
    S.write_sheet(out, cases, first, second, seed=7)
    sheet = (out / S.SHEET).read_text()
    assert "version-one" not in sheet and "version-two" not in sheet
    assert sheet.count("### Note A") == sheet.count("### Note B") == len(cases.cases)
    key = json.loads((out / S.KEY).read_text())["key"]
    assert {frozenset(v.values()) for v in key.values()} == {
        frozenset({"version-one", "version-two"})}
    # Shuffled per case, not one fixed order.
    assert len({v["A"] for v in key.values()}) == 2
    rows = list(csv.DictReader((out / S.SCORES).open()))
    for row in rows:
        for criterion in S.CRITERIA:
            row[criterion] = "2" if key[row["case"]][row["note"]] == "version-one" else "1"
    with (out / S.SCORES).open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    totals = S.tally(out)
    assert totals["version-one"] == dict.fromkeys(S.CRITERIA, 12)
    assert totals["version-two"] == dict.fromkeys(S.CRITERIA, 6)
    rows[0]["coverage"] = ""
    with (out / S.SCORES).open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    with pytest.raises(ValueError, match="coverage"):
        S.tally(out)
    with pytest.raises(FileExistsError):
        S.write_sheet(out, cases, first, second)


# --------------------------------------------------------------------------- runner


class Provider:
    """A mocked OpenRouter: counts requests, can fail or crash on chosen calls."""

    def __init__(self, cost: float = 0.01):
        self.requests = 0
        self.cost = cost
        self.crash_on: int | None = None
        self.network_error_on: int | None = None

    def __call__(self, request):
        self.requests += 1
        if self.requests == self.crash_on:
            raise SystemExit("process killed mid-call")
        if self.requests == self.network_error_on:
            raise httpx.ConnectError("down")
        return completion(json.dumps(GOOD), cost=self.cost)


def runner_for(tmp_path, provider: Provider, *, weekly: float = 2.5,
               rates=RATES) -> tuple[EvalRunner, AICostGuard, RunLedger]:
    guard = AICostGuard(tmp_path / "budget.sqlite3", weekly_limit_usd=weekly, timezone="UTC")
    writer = DeskWriter(api_key="k", model=MODEL, prompt_version=V8, model_rates=rates,
                        cost_guard=guard,
                        client=httpx.AsyncClient(transport=httpx.MockTransport(provider)))
    ledger = RunLedger(tmp_path / "runs.sqlite3")
    return EvalRunner(writer=writer, guard=guard, ledger=ledger, rates=rates), guard, ledger


def bound(cases: C.CaseSet) -> float:
    """The run ledger's ceiling for one call."""
    writer = DeskWriter(api_key="k", model=MODEL, prompt_version=V8, model_rates=RATES)
    return writer.hard_max_cost(NoteKind.ASK, cases.cases[0].facts, model=MODEL, instrument=NQ)


def estimate(cases: C.CaseSet) -> float:
    """What the shared cost guard reserves for one call."""
    writer = DeskWriter(api_key="k", model=MODEL, prompt_version=V8, model_rates=RATES)
    return writer.max_cost(NoteKind.ASK, cases.cases[0].facts, model=MODEL, instrument=NQ)


def charges(guard: AICostGuard) -> list[tuple[int, int | None]]:
    guard.initialize()
    return guard._db.execute("SELECT reserved, actual FROM ai_charges ORDER BY rowid").fetchall()


async def test_runner_makes_one_call_per_case_reserved_on_the_shared_ledger(tmp_path):
    cases = frozen(tmp_path, [complete_case(f"ask:{i}") for i in range(3)])
    provider = Provider()
    runner, guard, ledger = runner_for(tmp_path, provider)
    out = tmp_path / "run.json"
    results = await runner.run(cases, run_id="r", max_calls=3, allowance_usd=1.0, out=out)
    assert provider.requests == 3
    assert set(results.entries) == {"ask:0", "ask:1", "ask:2"}
    assert all(e["draft"] and e["cost_usd"] == 0.01 for e in results.entries.values())
    assert [actual for _, actual in charges(guard)] == [10_000] * 3
    assert ledger.usage("r") == (3, 30_000)
    assert R.load_results(out, cases).entries == results.entries
    # Run again (a restart): every case was attempted, nothing is sent.
    again = await runner.run(cases, run_id="r", max_calls=3, allowance_usd=1.0, out=out)
    assert provider.requests == 3 and again.entries == results.entries


async def test_runner_refuses_when_a_cost_bound_cant_be_set(tmp_path):
    cases = frozen(tmp_path, [complete_case("ask:0"), complete_case("ask:1")])
    runner, _, _ = runner_for(tmp_path, Provider())
    plan = dict(run_id="r", max_calls=2, allowance_usd=1.0)
    assert len(runner.plan(cases, **plan)) == 2
    with pytest.raises(RunRefused, match="calls planned"):
        runner.plan(cases, **{**plan, "max_calls": 1})
    with pytest.raises(RunRefused, match="allowance"):
        runner.plan(cases, **{**plan, "allowance_usd": bound(cases) * 1.5})
    with pytest.raises(RunRefused, match="positive"):
        runner.plan(cases, **{**plan, "max_calls": 0})
    unpriced, _, _ = runner_for(tmp_path / "u", Provider(), rates={"other/model": (1.0, 1.0)})
    with pytest.raises(RunRefused, match="no price on record"):
        unpriced.plan(cases, **plan)
    unlimited, _, _ = runner_for(tmp_path / "n", Provider(), weekly=0)
    unlimited._guard = AICostGuard(tmp_path / "n" / "off.sqlite3", monthly_limit_usd=0,
                                   daily_limit_usd=0, weekly_limit_usd=0)
    with pytest.raises(RunRefused, match="spending limits"):
        unlimited.plan(cases, **plan)
    partial = frozen(tmp_path / "p", [replace(complete_case("ask:0"), facts_complete=False)])
    with pytest.raises(RunRefused, match="partial facts"):
        runner.plan(partial, **plan)


async def test_call_limit_holds_across_a_crash_and_restart(tmp_path):
    cases = frozen(tmp_path, [complete_case(f"ask:{i}") for i in range(3)])
    provider = Provider()
    provider.crash_on = 2
    runner, guard, ledger = runner_for(tmp_path, provider)
    out = tmp_path / "run.json"
    limits = dict(run_id="r", max_calls=3, allowance_usd=1.0)
    with pytest.raises(SystemExit):
        await runner.run(cases, **limits, out=out)
    ledger.close()
    # Restart: new objects over the same files.
    provider.crash_on = None
    runner, guard, ledger = runner_for(tmp_path, provider)
    assert ledger.attempted("r") == {"ask:0": DONE, "ask:1": PENDING}
    # The crashed call keeps its whole bound on both ledgers.
    calls, committed = ledger.usage("r")
    assert calls == 2 and committed == 10_000 + round(bound(cases) * 1e6 + 0.5)
    assert charges(guard)[1][1] is None
    # Limits can't be raised or changed by restarting.
    with pytest.raises(RunRefused, match="cannot change"):
        runner.plan(cases, **{**limits, "max_calls": 4})
    results = await runner.run(cases, **limits, out=out)
    assert provider.requests == 3  # only ask:2 was sent; ask:1 is never retried
    assert results.entries["ask:1"]["error"].startswith("attempt interrupted")
    assert ledger.usage("r")[0] == 3
    with pytest.raises(RunRefused, match="all 3 calls"):
        ledger.begin("r", "ask:extra", MODEL, 0.001)


async def test_dollar_allowance_holds_across_restarts(tmp_path):
    cases = frozen(tmp_path, [complete_case(f"ask:{i}") for i in range(3)])
    per_call = bound(cases)
    provider = Provider(cost=per_call)  # every call costs its whole bound
    runner, _, ledger = runner_for(tmp_path, provider)
    allowance = per_call * 3 + 0.0001
    await runner.run(cases, run_id="r", max_calls=5, allowance_usd=allowance,
                     out=tmp_path / "a.json")
    ledger.close()
    ledger = RunLedger(tmp_path / "runs.sqlite3")
    assert ledger.usage("r")[0] == 3
    # Calls are left, dollars are not.
    with pytest.raises(RunRefused, match="exceed"):
        ledger.begin("r", "ask:extra", MODEL, per_call)
    assert provider.requests == 3


async def test_unknown_cost_keeps_the_bound_and_a_budget_refusal_is_not_an_attempt(tmp_path):
    cases = frozen(tmp_path, [complete_case(f"ask:{i}") for i in range(2)])
    provider = Provider()
    provider.network_error_on = 1
    runner, guard, ledger = runner_for(tmp_path, provider)
    out = tmp_path / "run.json"
    results = await runner.run(cases, run_id="r", max_calls=2, allowance_usd=1.0, out=out)
    assert ledger.attempted("r")["ask:0"] == UNKNOWN
    assert ledger.usage("r") == (2, round(bound(cases) * 1e6 + 0.5) + 10_000)
    assert results.entries["ask:0"]["error"].startswith("network error")
    assert charges(guard)[0][1] is None  # still reserved on the shared ledger

    # The shared weekly ledger is full: the guard refuses before anything is sent.
    tight = frozen(tmp_path / "t", [complete_case("ask:a"), complete_case("ask:b")])
    provider = Provider()
    runner, guard, ledger = runner_for(tmp_path / "t", provider, weekly=estimate(tight) * 1.2)
    with pytest.raises(RunRefused, match="AI budget"):
        await runner.run(tight, run_id="t", max_calls=2, allowance_usd=1.0,
                         out=tmp_path / "t.json")
    assert provider.requests == 1
    assert ledger.attempted("t") == {"ask:a": DONE}
    assert ledger.usage("t") == (1, 10_000)


def test_run_without_paid_makes_no_calls(tmp_path, monkeypatch, capsys):
    cases = frozen(tmp_path, [complete_case("ask:0")])
    settings = Settings(_env_file=None, openrouter_api_key="k", ai_weekly_budget_usd=2.5,
                        ai_budget_db_path=tmp_path / "budget.sqlite3", data_dir=tmp_path,
                        desk_model=MODEL, desk_model_rates=RATES, desk_prompt_version=V8)
    monkeypatch.setattr(cli, "_eval_settings", lambda: (settings, tmp_path))

    async def forbidden(*args, **kwargs):
        raise AssertionError("no request may be sent without --paid")

    monkeypatch.setattr(httpx.AsyncClient, "post", forbidden)
    out = tmp_path / "run.json"
    assert cli.main(["eval", "run", str(cases.directory), str(out), "--max-calls", "1",
                     "--allowance-usd", "0.5"]) == 0
    assert "plan only" in capsys.readouterr().out and not out.exists()
    assert cli.main(["eval", "run", str(cases.directory), str(out), "--max-calls", "1",
                     "--allowance-usd", "0.0001"]) == 1
    assert "allowance" in capsys.readouterr().err
    assert cli.main(["eval", "render", str(cases.directory), str(tmp_path / "prompts")]) == 0
    rendered = (tmp_path / "prompts" / "ask_0.txt").read_text()
    assert rendered.startswith(f"SYSTEM\n{SYSTEM}") and facts_text(FACTS) in rendered


# --------------------------------------------------------------------------- review fixes


def test_whole_number_changes_are_still_sign_checked():
    facts = {"bond_market": {"yields": [{"name": "2-year yield", "change_today": "-3.0bp"}]}}
    assert K.sign_errors("The 2-year rose 3bp.", facts) == ["rose 3bp"]
    assert K.sign_errors("The 2-year rose 3.0bp.", facts) == ["rose 3.0bp"]
    assert K.sign_errors("The 2-year fell 3bp.", facts) == []


def test_only_real_names_skip_the_number_check():
    def numbers(text):
        draft = NoteDraft.model_validate({**GOOD, "summary": text})
        return invented_numbers(draft, facts_text(FACTS))

    assert numbers("Nasdaq 777% higher; S&P 999 points; a 77-year record.") == [
        "77", "777", "999"]
    assert numbers("Nasdaq 100% higher, toward Nasdaq 100,000.") == ["100", "100,000"]
    assert numbers("The Nasdaq 100, S&P 500, Nasdaq-100 futures, the 2-year and 10y.") == []


def test_the_run_bound_is_a_ceiling_the_provider_cannot_pass(tmp_path):
    cases = frozen(tmp_path, [complete_case("ask:0")])
    case = cases.cases[0]
    writer = DeskWriter(api_key="k", model=MODEL, prompt_version=V8, model_rates=RATES)
    ceiling = writer.hard_max_cost(NoteKind.ASK, case.facts, model=MODEL, instrument=NQ)
    # The worst a call can bill: one token per prompt byte, every output token used.
    sent = render(NoteKind.ASK, NQ, case.facts, version=V8)
    prompt_bytes = len((sent.system + sent.user).encode())
    rate_in, rate_out = RATES[MODEL]
    assert (prompt_bytes * rate_in + 4000 * rate_out) / 1e6 < ceiling
    # Routing params that change the token limit make the cost unboundable.
    loose = DeskWriter(api_key="k", model=MODEL, prompt_version=V8, model_rates=RATES,
                       model_params={MODEL: {"max_tokens": 100_000}})
    assert loose.hard_max_cost(NoteKind.ASK, case.facts, model=MODEL, instrument=NQ) is None
    guard = AICostGuard(tmp_path / "budget.sqlite3", weekly_limit_usd=2.5, timezone="UTC")
    runner = EvalRunner(writer=DeskWriter(api_key="k", model=MODEL, prompt_version=V8,
                                          model_rates=RATES,
                                          model_params={MODEL: {"max_tokens": 100_000}},
                                          cost_guard=guard),
                        guard=guard, ledger=RunLedger(tmp_path / "runs.sqlite3"), rates=RATES)
    with pytest.raises(RunRefused, match="token limit"):
        runner.plan(cases, run_id="r", max_calls=1, allowance_usd=1.0)


async def test_a_stale_or_second_runner_never_loses_a_paid_draft(tmp_path):
    cases = frozen(tmp_path, [complete_case(f"ask:{i}") for i in range(2)])
    provider = Provider()
    runner, _, ledger = runner_for(tmp_path, provider)
    out = tmp_path / "run.json"
    await runner.run(cases, run_id="r", max_calls=2, allowance_usd=1.0, out=out)
    # Another process (or a stale copy) wipes the results file.
    out.write_text(json.dumps({"manifest_sha256": cases.sha256, "label": "x", "entries": {}}))
    second, _, _ = runner_for(tmp_path, provider)
    results = await second.run(cases, run_id="r", max_calls=2, allowance_usd=1.0, out=out)
    assert provider.requests == 2
    assert all(entry["draft"] == NoteDraft.model_validate(GOOD).model_dump(mode="json")
               for entry in results.entries.values())
    assert R.load_results(out, cases).entries == results.entries


def test_facts_the_prompt_states_may_be_quoted_but_its_limits_may_not():
    from core.desk.prompts import STATED_FACTS, TASKS

    sources = render(NoteKind.MOVE, NQ, FACTS, version=V8).number_sources
    # Every stated fact really is in the prompt the writer sees.
    prompt = SYSTEM + " ".join(TASKS.values())
    for phrase in ("10 minutes", "18:00 ET", "08:30 ET", "30 minutes before the US open"):
        assert phrase in prompt and phrase in STATED_FACTS
    draft = NoteDraft.model_validate({
        **GOOD, "summary": "Prices are about 10 minutes delayed; data came at 08:30 ET."})
    assert invented_numbers(draft, sources) == []
    limits = NoteDraft.model_validate({**GOOD, "summary": "Max 45 words, 2-4 items (rule 13)."})
    assert invented_numbers(limits, sources) == ["13", "2", "4", "45"]


def test_a_blank_or_invalid_draft_is_a_regression_not_a_crash(tmp_path, capsys):
    cases = C.load(SHIPPED)

    def blank(entries):
        entries["move:20261027T11"]["draft"] = dict.fromkeys(
            entries["move:20261027T11"]["draft"], "")
        del entries["ask:20261024T074748"]["draft"]["headline"]

    bad = tmp_path / "blank.json"
    R.save_results(bad, candidate(cases, blank))
    assert cli.main(["eval", "compare", str(SHIPPED), str(bad)]) == 1
    out = capsys.readouterr().out
    assert "move:20261027T11 no_draft: 0 → 1" in out and "ask:20261024T074748 no_draft" in out
    assert "FAIL: 2 regression(s)" in out


async def test_an_unbilled_provider_rejection_is_not_an_attempt_and_stops_the_run(tmp_path):
    cases = frozen(tmp_path, [complete_case(f"ask:{i}") for i in range(2)])
    sent = []

    def rejected(request):
        sent.append(1)
        return httpx.Response(402, json={"error": "no credit"})

    runner, guard, ledger = runner_for(tmp_path, rejected)
    with pytest.raises(RunRefused, match="HTTP 402"):
        await runner.run(cases, run_id="r", max_calls=2, allowance_usd=1.0,
                         out=tmp_path / "run.json")
    assert sent == [1]
    assert ledger.attempted("r") == {} and ledger.usage("r") == (0, 0)
    assert [actual for _, actual in charges(guard)] == [0]


def test_tally_refuses_a_half_filled_sheet(tmp_path):
    cases = C.load(SHIPPED)
    out = tmp_path / "sheet"
    S.write_sheet(out, cases, R.stored_results(cases), candidate(cases, lambda e: None), seed=1)
    rows = list(csv.DictReader((out / S.SCORES).open()))
    for row in rows:
        row.update(dict.fromkeys(S.CRITERIA, "2"))

    def write(selected):
        with (out / S.SCORES).open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(selected)

    write(rows[:3])
    with pytest.raises(ValueError, match="unscored notes"):
        S.tally(out)
    write([*rows, rows[0]])
    with pytest.raises(ValueError, match="repeated"):
        S.tally(out)


def test_paid_run_refuses_when_the_ledger_was_lost(tmp_path, monkeypatch, capsys):
    cases = frozen(tmp_path, [complete_case("ask:0")])
    settings = Settings(_env_file=None, openrouter_api_key="k", ai_weekly_budget_usd=2.5,
                        ai_budget_db_path=tmp_path / "budget.sqlite3", data_dir=tmp_path,
                        desk_model=MODEL, desk_model_rates=RATES, desk_prompt_version=V8)
    monkeypatch.setattr(cli, "_eval_settings", lambda: (settings, tmp_path))

    async def forbidden(*args, **kwargs):
        raise AssertionError("no request may be sent")

    monkeypatch.setattr(httpx.AsyncClient, "post", forbidden)
    out = tmp_path / "run.json"
    out.write_text("{}")  # drafts from a run whose ledger is gone
    assert cli.main(["eval", "run", str(cases.directory), str(out), "--max-calls", "1",
                     "--allowance-usd", "0.5", "--paid"]) == 1
    assert "ledger may be lost" in capsys.readouterr().err


def test_bps_is_a_basis_point_unit():
    facts = {"bond_market": {"yields": [{"name": "2-year yield", "change_today": "-1.7bps"}]}}
    assert K.sign_errors("The 2-year rose 1.7bps.", facts) == ["rose 1.7bps"]
    assert K.sign_errors("The 2-year rose 1.7%.", facts) == []
