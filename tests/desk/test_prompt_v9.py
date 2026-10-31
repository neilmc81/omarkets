"""Phase 1 slice 5: prompt desk:v9, shared rules plus instrument profiles.

Production still writes desk:v8 by default; v9 is selected by
``desk_prompt_version`` (or ``eval --prompt``). Snapshots regenerate with
``UPDATE_PROMPT_SNAPSHOTS=1``; review the diff.
"""

import json
import typing
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from core import cli
from core.config.settings import Settings
from core.desk import identity as ID
from core.desk import prompts as P
from core.desk.evaluation import checker
from core.desk.models import NoteDraft, NoteKind
from core.desk.service import DeskService
from core.desk.writer import DeskWriter, WriteResult, check_draft, invented_numbers
from core.domain.instruments import AssetClass
from tests.desk.test_evaluation import MODEL, RATES, complete_case, frozen
from tests.desk.test_prompt_snapshot import check, facts_for
from tests.desk.test_service import RELEASE, FakeWriter, cpi_rows, draft, make
from tests.desk.test_writer_and_api import GOOD, FakeGuard, completion

ES = ID.Instrument.of("ES")
YM = ID.Instrument.of("YM")
NOW = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
NO_NUMBERS = {**GOOD, "market_reaction": "NQ slipped.", "rates_read": "Yields rose."}


def text(prompt: P.Prompt) -> str:
    return f"SYSTEM\n{prompt.system}\n\nUSER\n{prompt.user}\n"


class V9Writer(FakeWriter):
    """Records each call's instrument too."""

    prompt_version = P.V9

    def __init__(self):
        super().__init__()
        self.sent: list[tuple[NoteKind, str | None, dict]] = []

    async def write(self, kind, facts, *, instrument):
        self.sent.append((kind, instrument.symbol if instrument else None, facts))
        return WriteResult(draft(), "fake", 0.01)


def nq_and_es(now, **kwargs):
    """A desk that writes for NQ and ES, as tests inject (production is NQ only)."""
    base, clock, _ = make(now, **kwargs)
    writer = V9Writer()
    service = DeskService(sources=base.sources, store=base.store, writer=writer,
                          clock=lambda: clock["now"], instruments=("NQ", "ES"))
    return service, writer


def es_facts(writer: V9Writer, kind: NoteKind) -> dict:
    return next(f for k, s, f in writer.sent if k is kind and s == "ES")


# --------------------------------------------------------------------------- versions


def test_production_still_writes_v8_and_v8_stays_readable():
    assert P.PROMPT_VERSION == P.V8 == "desk:v8"
    assert Settings(_env_file=None).desk_prompt_version == "desk:v8"
    assert DeskWriter(api_key="k", model=MODEL).prompt_version == "desk:v8"
    assert P.PROFILES is P.PROMPTS[P.V8] and set(P.PROFILES) == {"NQ"}
    nq = P.PROMPTS[P.V8]["NQ"]
    assert (nq.system, nq.tasks, nq.stated_facts) == (P.SYSTEM, P.TASKS, P.STATED_FACTS)
    assert set(P.PROMPTS[P.V9]) == {"NQ", "ES"}


def test_the_setting_offers_exactly_the_known_versions():
    field = Settings.model_fields["desk_prompt_version"]
    assert typing.get_args(field.annotation) == P.VERSIONS
    assert Settings(_env_file=None, desk_prompt_version="desk:v9").desk_prompt_version == P.V9
    with pytest.raises(ValueError):
        Settings(_env_file=None, desk_prompt_version="desk:v10")
    with pytest.raises(ValueError, match="unknown prompt version"):
        DeskWriter(api_key="k", model=MODEL, prompt_version="desk:v10")


def test_production_wiring_passes_the_setting_to_the_writer(monkeypatch, tmp_path):
    import core.main as main

    built = {}

    class Recorder:
        def __init__(self, **kwargs):
            built.update(kwargs)

    monkeypatch.setattr("core.desk.writer.DeskWriter", Recorder)
    monkeypatch.setattr("core.desk.service.DeskService", lambda **kwargs: kwargs)
    settings = Settings(_env_file=None, desk_prompt_version="desk:v9",
                        desk_db_path=tmp_path / "desk.sqlite3")
    market = SimpleNamespace(current_state=None, history=None, ohlc=None)
    main.build_desk_service(settings, market=market, economics=None, news_service=None,
                            cost_guard=None)
    assert built["prompt_version"] == "desk:v9"


# --------------------------------------------------------------------------- v9 text


def test_every_placeholder_is_filled_and_nq_wording_lives_in_nq_profile():
    for symbol, profile in P.PROMPTS[P.V9].items():
        for part in (profile.system, *profile.tasks.values()):
            assert "{" not in part and "}" not in part, symbol
        assert f"({symbol})" in profile.system and '"tone"' not in profile.system
        assert "- tone:" in profile.system and "nq_tone" not in profile.system
    es = P.PROMPTS[P.V9]["ES"].system
    assert "Nasdaq 100 futures (NQ)" not in es and "nq_price_location" not in es
    assert "es_price_location" in es and "VXN" not in es
    nq = P.PROMPTS[P.V9]["NQ"].system
    assert "nq_price_location" in nq and "VXN" in nq
    # The shared rules are the same text for both, apart from the instrument.
    shared = nq.split("RULES\n")[1].split("\n")[:9]
    assert shared == [line.replace("ES", "NQ") for line in es.split("RULES\n")[1].split("\n")[:9]]


def test_the_section_7_rules_are_in_the_shared_text():
    system = P.PROMPTS[P.V9]["NQ"].system
    for rule in ('"rose 0.88%"', 'never "rose +0.88%"', "missing from Omarkets' feeds",
                 '"closed"', '"stale"', '"unavailable"', "Never merge measurement windows",
                 "0.01 percentage point (not 0.01%)", '"received"'):
        assert rule in system, rule


def test_the_move_task_is_the_section_7_text():
    for symbol in ("NQ", "ES"):
        task = P.PROMPTS[P.V9][symbol].tasks[NoteKind.MOVE]
        assert f"explain what moved {symbol}" in task
        assert "say so plainly" in task and "at most two" in task
        assert "'not confirmed', unranked" in task
        assert "'no event is on the supplied calendar'" in task
        assert "events_in_move_window" in task and "most likely candidates" not in task


def test_length_targets_follow_section_7():
    tasks = P.PROMPTS[P.V9]["NQ"].tasks
    for kind, target in checker.LENGTH_TARGETS.items():
        lo, hi = target
        assert f"Length: {lo}-{hi} words" in tasks[NoteKind(kind)], kind


def test_v9_states_its_own_facts():
    stated = P.PROMPTS[P.V9]["NQ"].stated_facts
    assert stated in P.PROMPTS[P.V9]["NQ"].system
    assert stated == P.PROMPTS[P.V9]["ES"].stated_facts
    note = NoteDraft.model_validate({**NO_NUMBERS, "summary": "The US open is 09:30 ET; "
                                     "data comes at 08:30 ET."})
    assert invented_numbers(note, stated) == []


# --------------------------------------------------------------------------- snapshots


@pytest.mark.parametrize("kind", [NoteKind.PREMARKET, NoteKind.RELEASE,
                                  NoteKind.MOVE, NoteKind.ASK])
async def test_v9_nq_snapshot(kind):
    facts = await facts_for(kind)
    prompt = P.render(kind, None if kind is NoteKind.RELEASE else ID.NQ, facts, version=P.V9)
    assert prompt.version == "desk:v9"
    check(f"v9_nq_{kind.value}.txt", text(prompt))


async def test_v9_es_snapshots():
    from tests.desk.test_complete_inputs import inst

    service, writer = nq_and_es(RELEASE + timedelta(minutes=35), rows=cpi_rows())
    service.sources.market_state().instruments.update({i.symbol: i for i in (
        inst("NQ", 30120.5, change_pct=0.62), inst("ES", 6810.25, change_pct=0.31))})
    await service.tick()
    facts = es_facts(writer, NoteKind.PREMARKET)
    check("v9_es_premarket.txt", text(P.render(NoteKind.PREMARKET, ES, facts, version=P.V9)))

    es = [SimpleNamespace(timestamp=NOW - timedelta(minutes=80) + timedelta(minutes=i),
                          value=6800.0 * (1 - 0.0002 * i)) for i in range(80)]
    service, writer = nq_and_es(NOW, series={"ES": es}, rows=cpi_rows())
    await service.tick()
    facts = es_facts(writer, NoteKind.MOVE)
    assert facts["the_move"]["market"] == "S&P 500 futures"
    check("v9_es_move.txt", text(P.render(NoteKind.MOVE, ES, facts, version=P.V9)))

    service, writer = nq_and_es(NOW, rows=cpi_rows())
    await service.ask(ES)
    facts = es_facts(writer, NoteKind.ASK)
    check("v9_es_ask.txt", text(P.render(NoteKind.ASK, ES, facts, version=P.V9)))


async def test_the_v8_snapshots_are_untouched_at_the_default_version():
    facts = await facts_for(NoteKind.PREMARKET)
    prompt = P.render(NoteKind.PREMARKET, ID.NQ, facts)
    assert prompt.version == "desk:v8" and prompt.system == P.SYSTEM
    check("nq_premarket.txt", text(prompt))


# --------------------------------------------------------------------------- ES and refusals


def test_es_renders_with_its_own_profile_only_at_v9():
    with pytest.raises(P.NoPromptProfile, match="no desk:v8 prompt for ES"):
        P.render(NoteKind.PREMARKET, ES, {})
    prompt = P.render(NoteKind.PREMARKET, ES, {"es": "+0.31%"}, version=P.V9)
    assert prompt.instrument == "ES" and prompt.system == P.PROMPTS[P.V9]["ES"].system
    assert prompt.system != P.PROMPTS[P.V9]["NQ"].system
    assert "ES PRE-MARKET" in prompt.task and "NQ PRE-MARKET" not in prompt.task


def test_unknown_instruments_are_refused_and_never_fall_back_to_nq():
    for version in P.VERSIONS:
        with pytest.raises(P.NoPromptProfile, match=f"no {version} prompt for YM"):
            P.render(NoteKind.ASK, YM, {}, version=version)
        assert not P.has_profile("YM", version=version)
    assert P.has_profile("ES", version=P.V9) and not P.has_profile("ES")


def test_event_notes_stay_desk_wide_and_use_nq_v9():
    prompt = P.render(NoteKind.FED, None, {}, version=P.V9)
    assert prompt.instrument is None and prompt.system == P.PROMPTS[P.V9]["NQ"].system
    with pytest.raises(ValueError, match="desk-wide"):
        P.render(NoteKind.FED, ES, {}, version=P.V9)
    # ES has no event tasks: its profile only covers its own notes.
    assert set(P.PROMPTS[P.V9]["ES"].tasks) == ID.INSTRUMENT_KINDS


class Wire:
    def __init__(self, content=None):
        self.bodies: list[dict] = []
        self.content = content or json.dumps(GOOD)

    def __call__(self, request):
        self.bodies.append(json.loads(request.content))
        return completion(self.content)


def v9_writer(wire: Wire, guard=None) -> DeskWriter:
    return DeskWriter(api_key="k", model=MODEL, model_rates=RATES, cost_guard=guard,
                      client=httpx.AsyncClient(transport=httpx.MockTransport(wire)),
                      prompt_version=P.V9)


async def test_a_v9_writer_writes_es_and_still_refuses_ym_without_a_call():
    wire, guard = Wire(), FakeGuard()
    w = v9_writer(wire, guard)
    result = await w.write(NoteKind.PREMARKET, {"es": "+0.31%"}, instrument=ES)
    assert result.draft is not None
    system, user = (m["content"] for m in wire.bodies[0]["messages"])
    assert system == P.PROMPTS[P.V9]["ES"].system and "ES PRE-MARKET" in user
    envelope = w.envelope(NoteKind.PREMARKET, result, instrument=ES)
    assert envelope["prompt_version"] == "desk:v9" and envelope["instrument"] == "ES"
    sent = len(wire.bodies)
    refused = await w.write(NoteKind.PREMARKET, {}, instrument=YM)
    assert refused.error == "no desk:v9 prompt for YM" and refused.prompt is None
    assert len(wire.bodies) == sent and len(guard.reserved) == sent


async def test_es_is_configurable_with_a_v9_writer_and_gets_its_own_notes():
    base, clock, _ = make(RELEASE + timedelta(minutes=35), rows=cpi_rows())
    wire = Wire()
    service = DeskService(sources=base.sources, store=base.store, writer=v9_writer(wire),
                          clock=lambda: clock["now"], instruments=("NQ", "ES"))
    await service.tick()
    es, nq = service.store.get("premarket:ES:2026-10-20"), service.store.get("premarket:2026-10-20")
    assert es.status == nq.status == "ok"
    assert es.model.endswith("· desk:v9") and nq.model.endswith("· desk:v9")
    systems = {b["messages"][0]["content"] for b in wire.bodies}
    assert P.PROMPTS[P.V9]["ES"].system in systems and P.PROMPTS[P.V9]["NQ"].system in systems
    _, envelope = service.store.facts("premarket:ES:2026-10-20")
    assert envelope["prompt_version"] == "desk:v9" and envelope["instrument"] == "ES"


# --------------------------------------------------------------------------- tone and rates_read


async def test_v9_tone_is_read_as_the_stored_nq_tone():
    body = {k: v for k, v in GOOD.items() if k != "nq_tone"} | {"tone": "mixed"}
    result = await v9_writer(Wire(json.dumps(body))).write(NoteKind.ASK, {}, instrument=ES)
    assert result.draft is not None and result.draft.nq_tone == "mixed"


async def test_rates_read_is_required_wherever_yields_matter():
    without = json.dumps({k: v for k, v in GOOD.items() if k != "rates_read"})
    result = await v9_writer(Wire(without)).attempt(NoteKind.ASK, {}, instrument=ES)
    assert result.draft is None and result.error == "model output was not valid"
    bare = NoteDraft.model_validate(json.loads(without))
    assert check_draft(bare) == "model output was not valid"
    assert check_draft(bare, rates_read_required=False) is None


def test_an_asset_class_where_yields_dont_matter_makes_rates_read_optional(monkeypatch):
    from dataclasses import replace

    monkeypatch.setitem(P.ASSET_DEFAULTS, AssetClass.CRYPTO,
                        P.AssetDefaults(about="Quotes are live.", rates_rule=None))
    profile = P._v9(replace(P.SPECS["ES"], symbol="BTC", name="Bitcoin",
                            asset=AssetClass.CRYPTO))
    assert not profile.rates_read_required
    assert "- rates_read: leave this field out" in profile.system
    assert "Bond yields are not a main driver of BTC" in profile.system


# --------------------------------------------------------------------------- evaluation


def es_case():
    from dataclasses import replace

    return replace(complete_case("premarket:ES:2026-10-20", kind="premarket"),
                   facts={"es": "+0.31%"},
                   envelope={"prompt_version": "desk:v8", "instrument": "ES"})


def eval_settings(tmp_path, **kwargs):
    return Settings(_env_file=None, openrouter_api_key="k", ai_weekly_budget_usd=2.5,
                    ai_budget_db_path=tmp_path / "budget.sqlite3", data_dir=tmp_path,
                    desk_model=MODEL, desk_model_rates=RATES, **kwargs)


def test_eval_render_and_plan_take_a_prompt_version(tmp_path, monkeypatch, capsys):
    cases = frozen(tmp_path, [complete_case("ask:0"), es_case()])
    monkeypatch.setattr(cli, "_eval_settings", lambda: (eval_settings(tmp_path), tmp_path))

    async def forbidden(*args, **kwargs):
        raise AssertionError("no request may be sent")

    monkeypatch.setattr(httpx.AsyncClient, "post", forbidden)
    out = tmp_path / "prompts"
    assert cli.main(["eval", "render", str(cases.directory), str(out),
                     "--prompt", "desk:v9"]) == 0
    es = P.render(NoteKind.PREMARKET, ES, {"es": "+0.31%"}, version=P.V9)
    assert (out / "premarket_ES_2026-10-20.txt").read_text() == text(es)
    assert "(desk:v9, cases" in (out / "index.md").read_text()
    capsys.readouterr()
    assert cli.main(["eval", "run", str(cases.directory), str(tmp_path / "run.json"),
                     "--max-calls", "2", "--allowance-usd", "1", "--prompt", "desk:v9"]) == 0
    printed = capsys.readouterr().out
    assert "prompt desk:v9" in printed and "plan only: no calls made" in printed
    assert not (tmp_path / "run.json").exists()


def test_the_checker_allows_the_stated_facts_of_the_version_that_wrote_the_draft():
    note = {**NO_NUMBERS, "summary": "The US stock market opens at 09:30 ET."}
    score = dict(case_id="ask:0", kind="ask", model=MODEL, created_at="2026-10-20T15:00:00+00:00",
                 draft=note, facts={}, facts_complete=True, history=[])
    assert checker.score_draft(**score, prompt_version="desk:v9").numbers_not_in_facts == []
    assert checker.score_draft(**score, prompt_version="desk:v8").numbers_not_in_facts == ["09"]
    # v8 states 08:30 but not 09:30. Older or unrecorded versions are checked
    # as before, at the production default.
    assert checker.score_draft(**score, prompt_version="desk:v4").numbers_not_in_facts == ["09"]


def test_price_location_fields_name_their_own_instrument():
    from core.desk.facts import price_location

    settle = SimpleNamespace(key="prior_settle", value=6000.0, label="Prior settle",
                             value_text="6,000.00", window_start=None, window_end=None,
                             partial=False)
    es = price_location(SimpleNamespace(symbol="ES", levels=[settle]), 6010.0)
    assert es["es_price"] == "6,010.00" and es["levels"][0]["es_is"] == "10.00 points above"
    assert "nq_price" not in es and "nq_is" not in es["levels"][0]
    nq = price_location(SimpleNamespace(symbol="NQ", levels=[settle]), 6010.0)
    assert "nq_price" in nq and "nq_is" in nq["levels"][0]


async def test_a_run_never_mixes_prompt_versions(tmp_path):
    from core.desk.evaluation.runner import EvalRunner, RunRefused
    from tests.desk.test_evaluation import runner_for

    v8, guard, ledger = runner_for(tmp_path, Wire())
    cases = frozen(tmp_path / "one", [complete_case("ask:0")])
    await v8.run(cases, run_id="r", max_calls=1, allowance_usd=1.0, out=tmp_path / "r.json")
    assert json.loads((tmp_path / "r.json").read_text())["label"] == "desk:v8 run r"
    v9 = EvalRunner(writer=v9_writer(Wire(), guard), guard=guard, ledger=ledger, rates=RATES)
    with pytest.raises(RunRefused, match="has desk:v8 results"):
        v9.plan(cases, run_id="r", max_calls=1, allowance_usd=1.0)
    assert v9.plan(cases, run_id="fresh", max_calls=1, allowance_usd=1.0)
