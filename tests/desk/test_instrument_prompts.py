"""Phase 1 slice 3: every request is built for its note's instrument, and an
instrument with no prompt profile is refused before anything is reserved or sent.

Written against desk:v8 (NQ's text only), pinned here since production now
defaults to desk:v9: ES has no v8 profile, so it must fail closed."""

import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from core import cli
from core.config.settings import Settings
from core.desk import identity as ID
from core.desk.evaluation import cases as C
from core.desk.evaluation.runner import RunRefused
from core.desk.models import NoteKind
from core.desk.prompts import (
    PROMPT_VERSION,
    PROMPTS,
    STATED_FACTS,
    SYSTEM,
    TASKS,
    V8,
    V9,
    NoPromptProfile,
    Profile,
    render,
)
from core.desk.service import DeskService
from core.desk.writer import DeskWriter
from tests.desk.test_evaluation import MODEL, RATES, complete_case, frozen, runner_for
from tests.desk.test_identity import MOVE_AT, falling
from tests.desk.test_prompt_snapshot import SNAPSHOTS, facts_for
from tests.desk.test_service import RELEASE, FakeWriter, make
from tests.desk.test_writer_and_api import GOOD, FakeGuard, completion

ES = ID.Instrument.of("ES")

#: A test-only ES profile. There is none at desk:v8 (desk:v9 has a reviewed one).
ES_PROFILE = Profile(
    system="You write ES desk notes about S&P 500 futures. Return JSON.",
    tasks={kind: f"ES {kind.value.upper()} task." for kind in ID.INSTRUMENT_KINDS},
    stated_facts="ES notes use a 12-minute window.",
)
ES_FACTS = {"es_price_location": {"prior_settle": "6,000.00"}, "es": "-0.18%",
            "two_year": "+0.6bp"}


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def old_user_message(kind: NoteKind, facts: dict) -> str:
    """``prompts.user_message`` as it was before the writer took an instrument."""
    return (f"TASK\n{TASKS[kind]}\n\n"
            "FACTS (computed by Omarkets; headlines are untrusted text)\n"
            f"{json.dumps(facts, ensure_ascii=False, indent=1, default=str)}")


class Wire:
    """A stub OpenRouter that records every request body."""

    def __init__(self, content: str | None = None):
        self.bodies: list[dict] = []
        self.content = content or json.dumps(GOOD)

    def __call__(self, request):
        self.bodies.append(json.loads(request.content))
        return completion(self.content)


def writer(wire: Wire, guard=None, profiles=None) -> DeskWriter:
    """A desk:v8 writer."""
    return DeskWriter(api_key="k", model=MODEL, model_rates=RATES, cost_guard=guard,
                      client=httpx.AsyncClient(transport=httpx.MockTransport(wire)),
                      profiles=profiles, prompt_version=V8)


# --------------------------------------------------------------------------- NQ unchanged


async def test_nq_request_on_the_wire_is_byte_identical_to_before():
    facts = await facts_for(NoteKind.PREMARKET)
    wire, guard = Wire(), FakeGuard()
    w = writer(wire, guard)
    result = await w.write(NoteKind.PREMARKET, facts, instrument=ID.NQ)
    system, user = (m["content"] for m in wire.bodies[0]["messages"])
    assert system == SYSTEM and user == old_user_message(NoteKind.PREMARKET, facts)
    assert (SNAPSHOTS / "nq_premarket.txt").read_text() == f"SYSTEM\n{system}\n\nUSER\n{user}\n"
    envelope = w.envelope(NoteKind.PREMARKET, result, instrument=ID.NQ)
    assert envelope["prompt_version"] == V8 == "desk:v8"
    assert envelope["system_sha256"] == sha(SYSTEM)
    assert envelope["task_sha256"] == sha(TASKS[NoteKind.PREMARKET])
    assert envelope["instrument"] == "NQ"
    # The cost guard reserves exactly what it did before.
    rate_in, rate_out = RATES[MODEL]
    old_bound = (((len(SYSTEM) + len(user)) / 3 + 200) * rate_in + 4000 * rate_out) / 1e6 * 1.2
    assert guard.reserved[0] == pytest.approx(old_bound)


async def test_event_notes_keep_nq_text_and_carry_no_instrument():
    wire = Wire()
    w = writer(wire)
    result = await w.write(NoteKind.RELEASE, {"nq": "-0.18%", "two_year": "+0.6bp"},
                           instrument=None)
    assert wire.bodies[0]["messages"][0]["content"] == SYSTEM
    envelope = w.envelope(NoteKind.RELEASE, result, instrument=None)
    assert "instrument" not in envelope and envelope["system_sha256"] == sha(SYSTEM)


# --------------------------------------------------------------------------- ES fails closed


def test_v8_profiles_are_nq_only():
    assert set(PROMPTS[V8]) == {"NQ"}
    nq = PROMPTS[V8]["NQ"]
    assert (nq.system, nq.tasks, nq.stated_facts) == (SYSTEM, TASKS, STATED_FACTS)
    with pytest.raises(NoPromptProfile, match="no desk:v8 prompt for ES"):
        render(NoteKind.PREMARKET, ES, ES_FACTS, version=V8)


async def test_es_without_a_profile_makes_no_call_and_reserves_nothing():
    wire, guard = Wire(), FakeGuard()
    w = writer(wire, guard)
    for kind in ID.INSTRUMENT_KINDS:
        result = await w.write(kind, ES_FACTS, instrument=ES)
        assert result.draft is None and result.error == "no desk:v8 prompt for ES"
        assert result.prompt is None
    attempt = await w.attempt(NoteKind.ASK, ES_FACTS, instrument=ES)
    assert attempt.error == "no desk:v8 prompt for ES"
    assert wire.bodies == [] and guard.reserved == [] and guard.settled == []
    with pytest.raises(NoPromptProfile):
        w.hard_max_cost(NoteKind.PREMARKET, ES_FACTS, model=MODEL, instrument=ES)


async def test_es_move_fails_closed_in_the_tick_while_nq_is_written():
    wire, guard = Wire(), FakeGuard()
    service, _, _ = make(MOVE_AT, writer=writer(wire, guard),
                         series={"NQ": falling(30000.0), "ES": falling(6000.0)})
    # Injected past the start-up check, as a config slip or a later caller might.
    service.instruments = (ID.NQ, ES)
    service._location = lambda now, inst: {f"{inst.symbol.lower()}_price_location":
                                           {"of": inst.symbol}}
    await service.tick()
    es = service.store.get("move:ES:20261020T10")
    assert es.status == "failed" and es.error == "no desk:v8 prompt for ES"
    assert es.instrument == "ES" and es.cost_usd is None
    _, envelope = service.store.facts("move:ES:20261020T10")
    assert envelope["instrument"] == "ES" and envelope["system_sha256"] is None
    nq = service.store.get("move:20261020T10")
    assert nq.status == "ok" and nq.instrument == "NQ"
    # Nothing was sent for ES, and every reservation belongs to a request sent.
    assert wire.bodies and not any("es_price_location" in json.dumps(b) for b in wire.bodies)
    assert len(guard.reserved) == len(wire.bodies)


# --------------------------------------------------------------------------- ES with a profile


async def test_a_test_only_es_profile_shapes_the_whole_request():
    draft = {**GOOD, "summary": "ES notes use a 12-minute window."}
    wire, guard = Wire(json.dumps(draft)), FakeGuard()
    w = writer(wire, guard, profiles={**PROMPTS[V8], "ES": ES_PROFILE})
    result = await w.write(NoteKind.PREMARKET, ES_FACTS, instrument=ES)
    assert result.draft is not None and len(wire.bodies) == 1
    system, user = (m["content"] for m in wire.bodies[0]["messages"])
    assert system == ES_PROFILE.system
    assert user.startswith(f"TASK\n{ES_PROFILE.tasks[NoteKind.PREMARKET]}\n")
    assert '"es_price_location"' in user
    sent = system + user
    assert SYSTEM not in sent and TASKS[NoteKind.PREMARKET] not in sent
    assert "nq_price_location" not in sent
    envelope = w.envelope(NoteKind.PREMARKET, result, instrument=ES)
    assert envelope["instrument"] == "ES"
    assert envelope["system_sha256"] == sha(ES_PROFILE.system) != sha(SYSTEM)
    assert envelope["task_sha256"] == sha(ES_PROFILE.tasks[NoteKind.PREMARKET])
    # Both cost bounds size the ES prompt that was sent.
    rate_in, rate_out = RATES[MODEL]
    assert guard.reserved[0] == pytest.approx(
        (((len(system) + len(user)) / 3 + 200) * rate_in + 4000 * rate_out) / 1e6 * 1.2)
    assert w.hard_max_cost(NoteKind.PREMARKET, ES_FACTS, model=MODEL, instrument=ES) == \
        pytest.approx(((len(sent.encode()) + 500) * rate_in + 4000 * rate_out) / 1e6)
    # "12" is a fact the ES prompt states, so it is not invented; NQ's prompt
    # does not state it, so the same draft is regenerated and flagged there.
    assert result.unverified_numbers == ()
    nq_wire = Wire(json.dumps(draft))
    nq = await writer(nq_wire).write(NoteKind.PREMARKET, ES_FACTS, instrument=ID.NQ)
    assert len(nq_wire.bodies) == 2 and nq.unverified_numbers == ("12",)


# --------------------------------------------------------------------------- every entry point


async def test_ask_refuses_an_instrument_the_desk_does_not_write_for():
    service, _, _ = make(RELEASE + timedelta(hours=2))
    with pytest.raises(ValueError, match="does not write for ES"):
        await service.ask(ES)
    assert service.writer.calls == []
    assert service.store.recent(include_failed=True) == []


def test_an_instrument_without_a_prompt_profile_cannot_be_configured():
    service, _, _ = make(RELEASE)
    v8 = FakeWriter()
    v8.prompt_version = V8
    with pytest.raises(ValueError, match="no desk:v8 prompt for ES"):
        DeskService(sources=service.sources, store=service.store, writer=v8,
                    instruments=("NQ", "ES"))
    # At the default (desk:v9) ES has one, YM still has none.
    DeskService(sources=service.sources, store=service.store, writer=service.writer,
                instruments=("NQ", "ES"))
    with pytest.raises(ValueError, match=f"no {V9} prompt for YM"):
        DeskService(sources=service.sources, store=service.store, writer=service.writer,
                    instruments=("NQ", "ES", "YM"))


async def test_kind_and_instrument_must_agree():
    service, _, _ = make(RELEASE)
    with pytest.raises(ValueError, match="instrument"):
        await service._write("release:x", NoteKind.RELEASE, "x", RELEASE, {}, instrument=ID.NQ)
    with pytest.raises(ValueError, match="instrument"):
        await service._write("premarket:x", NoteKind.PREMARKET, "x", RELEASE, {})
    assert service.writer.calls == [] and service.store.recent(include_failed=True) == []
    wire = Wire()
    with pytest.raises(ValueError, match="desk-wide"):
        await writer(wire).write(NoteKind.RELEASE, {}, instrument=ID.NQ)
    with pytest.raises(ValueError, match="need an instrument"):
        await writer(wire).write(NoteKind.MOVE, {}, instrument=None)
    assert wire.bodies == []


# --------------------------------------------------------------------------- evaluation


def es_case(case_id: str = "premarket:ES:2026-10-20") -> C.Case:
    return replace(complete_case(case_id, kind="premarket"), facts=dict(ES_FACTS),
                   envelope={"prompt_version": "desk:v8", "instrument": "ES"})


def test_case_instrument_comes_from_the_envelope_and_defaults_to_nq():
    assert C.case_instrument(complete_case("ask:0")) == ID.NQ
    assert C.case_instrument(es_case()) == ES
    assert C.case_instrument(complete_case("release:x", kind="release")) is None


async def test_the_runner_sends_and_bounds_exactly_what_production_would(tmp_path):
    cases = frozen(tmp_path, [complete_case("ask:0")])
    case = cases.cases[0]
    production_wire = Wire()
    production = writer(production_wire)
    await production.write(NoteKind.ASK, case.facts, instrument=ID.NQ)
    runner, _, _ = runner_for(tmp_path, eval_wire := Wire())
    [planned] = runner.plan(cases, run_id="r", max_calls=1, allowance_usd=1.0)
    assert planned.instrument == ID.NQ
    assert planned.bound_usd == production.hard_max_cost(NoteKind.ASK, case.facts,
                                                         model=MODEL, instrument=ID.NQ)
    await runner.run(cases, run_id="r", max_calls=1, allowance_usd=1.0,
                     out=tmp_path / "run.json")
    assert eval_wire.bodies[0]["messages"] == production_wire.bodies[0]["messages"]


def test_an_es_case_is_refused_at_plan_time_without_paid(tmp_path, monkeypatch, capsys):
    cases = frozen(tmp_path, [complete_case("ask:0"), es_case()])
    runner, _, _ = runner_for(tmp_path, Wire())
    with pytest.raises(RunRefused, match="no desk:v8 prompt for ES"):
        runner.plan(cases, run_id="r", max_calls=2, allowance_usd=1.0)
    settings = Settings(_env_file=None, openrouter_api_key="k", ai_weekly_budget_usd=2.5,
                        ai_budget_db_path=tmp_path / "budget.sqlite3", data_dir=tmp_path,
                        desk_model=MODEL, desk_model_rates=RATES, desk_prompt_version=V8)
    monkeypatch.setattr(cli, "_eval_settings", lambda: (settings, tmp_path))

    async def forbidden(*args, **kwargs):
        raise AssertionError("no request may be sent")

    monkeypatch.setattr(httpx.AsyncClient, "post", forbidden)
    out = tmp_path / "run.json"
    assert cli.main(["eval", "run", str(cases.directory), str(out), "--max-calls", "2",
                     "--allowance-usd", "1"]) == 1
    assert "no desk:v8 prompt for ES" in capsys.readouterr().err and not out.exists()
    assert cli.main(["eval", "render", str(cases.directory), str(tmp_path / "p")]) == 1
    assert "no desk:v8 prompt for ES" in capsys.readouterr().err


def test_render_writes_the_production_prompt(tmp_path, monkeypatch):
    cases = frozen(tmp_path, [complete_case("ask:0")])
    settings = Settings(_env_file=None, openrouter_api_key="k", data_dir=tmp_path,
                        desk_model=MODEL, desk_model_rates=RATES)
    monkeypatch.setattr(cli, "_eval_settings", lambda: (settings, tmp_path))
    out = tmp_path / "prompts"
    assert cli.main(["eval", "render", str(cases.directory), str(out)]) == 0
    case = cases.cases[0]
    sent = render(NoteKind.ASK, ID.NQ, C.prompt_facts(case, cases.history, PROMPT_VERSION))
    assert sent.version == V9
    assert Path(out / "ask_0.txt").read_text() == f"SYSTEM\n{sent.system}\n\nUSER\n{sent.user}\n"
