"""Strip desk coverage starts with GOLD: gold is wired through the desk like YM.

GOLD is in the production default on desk:v9. GOLD gets move and
ask notes with its own 3-hour move limit, never a pre-market (its profile has
no pre-market text at all), and its notes are written from its own prompt and
say they are about gold, never NQ. GOLD is already on the Today strip, so its
notes read its own quote there; NQ, ES and YM notes are unchanged."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.api import desk as desk_api
from core.config.settings import Settings
from core.desk import facts as F
from core.desk import identity as ID
from core.desk import prompts as P
from core.desk.models import NoteKind
from core.desk.service import DeskService
from core.domain.instruments import AssetClass
from core.domain.workspace import FocusUpdate
from core.storage.workspace import WorkspaceStore
from tests.desk.test_complete_inputs import inst, state
from tests.desk.test_identity import MOVE_AT, falling
from tests.desk.test_prompt_snapshot import check
from tests.desk.test_prompt_v9 import V9Writer as SentWriter
from tests.desk.test_prompt_v9 import text
from tests.desk.test_service import FakeWriter, cpi_rows, make

GOLD = ID.Instrument.of("GOLD")
ALL = ("NQ", "ES", "YM", "GOLD")


class V9Writer(FakeWriter):
    prompt_version = "desk:v9"


class V8Writer(FakeWriter):
    prompt_version = "desk:v8"


def desk(now, series=None, instruments=ALL, writer=None, **kwargs):
    return make(now, series=series, writer=writer or V9Writer(), instruments=instruments,
                **kwargs)


def flat(value, now):
    return falling(value, now, step=0.0)


def gold_quote(value=4012.5, **kwargs):
    return inst("GOLD", value, asset=AssetClass.COMMODITY, **kwargs)


# --------------------------------------------------------------------------- defaults


def test_the_default_desk_includes_gold_on_desk_v9():
    assert ID.PRODUCTION_INSTRUMENTS[:4] == ALL
    settings = Settings(_env_file=None)
    assert settings.desk_instruments[:4] == list(ALL)
    assert settings.desk_prompt_version == "desk:v9"
    assert all(P.has_profile(s, version=P.V9) for s in settings.desk_instruments)


def test_gold_gets_move_and_ask_only():
    assert ID.instrument_kinds(GOLD) == {NoteKind.MOVE, NoteKind.ASK}
    assert ID.note_id(NoteKind.MOVE, "20261020T10", GOLD) == "move:GOLD:20261020T10"
    assert ID.parse_note_id("ask:GOLD:20261020T100000").instrument == "GOLD"
    assert GOLD.label == "Gold"


def test_gold_on_desk_v8_is_refused_before_anything_runs():
    base, _, _ = make(MOVE_AT)
    with pytest.raises(ValueError, match="no desk:v8 prompt for GOLD"):
        DeskService(sources=base.sources, store=base.store, writer=V8Writer(),
                    instruments=("NQ", "GOLD"))
    assert not P.has_profile("GOLD", version=P.V8)


# --------------------------------------------------------------------------- prompt


@pytest.mark.parametrize("kind", [NoteKind.MOVE, NoteKind.ASK])
def test_gold_notes_use_gold_s_own_v9_prompt(kind):
    prompt = P.render(kind, GOLD, {})
    profile = P.PROMPTS[P.V9]["GOLD"]
    assert prompt.instrument == "GOLD" and prompt.system == profile.system
    assert all(prompt.system != P.PROMPTS[P.V9][s].system for s in ("NQ", "ES", "YM"))
    assert "move Gold (GOLD)" in prompt.system and "COMEX gold futures" in prompt.system
    assert "gold_price_location" in prompt.system and '"gold_is"' in prompt.system
    assert "nq_price_location" not in prompt.system
    assert "supportive or negative for GOLD" in prompt.system
    assert "GOLD" in prompt.task and "NQ has" not in prompt.task
    assert "megacap_participation" not in prompt.task


def test_gold_s_profile_has_no_premarket_at_all():
    assert set(P.PROMPTS[P.V9]["GOLD"].tasks) == {NoteKind.MOVE, NoteKind.ASK}
    with pytest.raises(P.NoPromptProfile, match="no desk:v9 premarket prompt for GOLD"):
        P.render(NoteKind.PREMARKET, GOLD, {})
    # Event notes stay desk-wide on NQ's profile.
    assert P.render(NoteKind.FED, None, {}).system == P.PROMPTS[P.V9]["NQ"].system


def test_gold_s_tone_is_defined_without_advice():
    system = P.PROMPTS[P.V9]["GOLD"].system
    # What rising and falling gold mean, and what usually drives each.
    assert '"supportive" means pushing gold\'s price up' in system
    assert '"negative" means pushing it down; neither is advice' in system
    assert "Gold often rises when yields fall or the Fed is expected to cut rates" in system
    assert '("dovish")' in system and "the US dollar weakens" in system
    assert "tendencies, not rules" in system
    # Yields matter for gold (it pays no interest), so rates_read is required.
    assert "Bond yields matter for GOLD: gold pays no interest" in system
    assert P.PROMPTS[P.V9]["GOLD"].rates_read_required


def test_gold_prompt_says_borrowed_facts_are_about_other_markets():
    system = P.PROMPTS[P.V9]["GOLD"].system
    assert "The facts have no gold fear gauge" in system
    assert "never use the VIX or VXN, which are stock-market gauges, as gold's" in system
    assert 'Gold notes get no "nq_vs_es"' in system
    assert "say they are stock markets, not gold" in system
    assert 'Gold notes get no "megacap_participation"' in system
    assert '"for_nq" line is written for NQ (the Nasdaq 100), not gold' in system
    assert '"comparable"' not in system


def test_the_stock_index_profiles_keep_the_nq_vs_es_wording():
    # GOLD's profile left these out; every other v9 profile still has them.
    for symbol in ("NQ", "ES", "YM"):
        profile = P.PROMPTS[P.V9][symbol]
        assert 'If "comparable" is false, say the comparison is not available and give its ' \
               '"why_not".' in profile.system
        assert ("whether the biggest tech stocks moved together (megacap_participation)"
                in profile.tasks[NoteKind.ASK])
        assert set(profile.tasks) >= ID.INSTRUMENT_KINDS


def test_a_spec_s_participation_matches_the_facts_it_gets():
    for symbol, spec in P.SPECS.items():
        assert spec.participation == (symbol in F.PARTICIPATION_INSTRUMENTS), symbol


# --------------------------------------------------------------------------- facts


def test_gold_has_no_fear_gauge_and_no_nq_es_context():
    assert "GOLD" not in F.VOL_INDEXES
    assert F.participation(state(), "GOLD") == {}


def test_a_gold_note_reads_its_own_quote_from_the_strip_once():
    assert "GOLD" in F.STRIP_SYMBOLS
    service, _, _ = desk(MOVE_AT)
    service.sources.market_state().instruments.update(
        NQ=inst("NQ", 29_000.0, at=MOVE_AT), GOLD=gold_quote(at=MOVE_AT))
    gold_facts, _ = service._common(MOVE_AT, GOLD)
    nq_facts, _ = service._common(MOVE_AT)
    rows = [row["market"] for row in gold_facts["markets_today"]]
    assert rows.count("Gold") == 1
    assert gold_facts["markets_today"] == nq_facts["markets_today"]
    [gold] = [row for row in gold_facts["markets_today"] if row["market"] == "Gold"]
    assert gold["level"] == "$4,012.50" and gold["change_from"] == "the previous session's settle"


def test_gold_context_has_no_vix_and_no_nq_es_blocks():
    service, _, _ = desk(MOVE_AT)
    service.sources.market_state().instruments.update(
        VIX=inst("VIX", 18.0, at=MOVE_AT, asset=AssetClass.VOLATILITY_INDEX),
        GOLD=gold_quote(at=MOVE_AT))
    context = service._nq_context(MOVE_AT, GOLD)
    assert "nq_vs_es" not in context and "megacap_participation" not in context
    assert "vix" not in context.get("normal_move_size", {})


async def nq_es_ym_facts(instruments):
    """What NQ, ES and YM ask notes are written from, on a desk of ``instruments``."""
    writer = SentWriter()
    service, _, _ = desk(MOVE_AT, writer=writer, instruments=instruments, rows=cpi_rows())
    service.sources.market_state().instruments.update({i.symbol: i for i in (
        inst("NQ", 30120.5, change_pct=0.62), inst("ES", 6810.25, change_pct=0.31),
        inst("YM", 47_100.0, change_pct=0.2), gold_quote())})
    for symbol in ("NQ", "ES", "YM"):
        await service.ask(ID.Instrument.of(symbol))
    return [P.facts_text(f) for _, _, f in writer.sent]


async def test_enabling_gold_leaves_nq_es_and_ym_facts_byte_for_byte():
    assert await nq_es_ym_facts(ALL) == await nq_es_ym_facts(("NQ", "ES", "YM"))


# --------------------------------------------------------------------------- moves


async def test_gold_move_fires_when_the_stock_indexes_are_flat():
    series = {"NQ": flat(30000.0, MOVE_AT), "ES": flat(6000.0, MOVE_AT),
              "YM": flat(47000.0, MOVE_AT), "GOLD": falling(4000.0)}
    service, _, _ = desk(MOVE_AT, series)
    [note] = await service.tick()
    assert note.id == "move:GOLD:20261020T10" and note.instrument == "GOLD"
    assert note.title.startswith("GOLD down") and note.tone_for == "GOLD"
    [(kind, facts)] = service.writer.calls
    assert kind is NoteKind.MOVE
    assert facts["the_move"]["market"] == "Gold"
    assert "Gold" in [m["market"] for m in facts["other_markets_same_window"]]
    assert "nq_vs_es" not in facts and "megacap_participation" not in facts


async def test_nq_move_fires_when_gold_is_flat():
    series = {"NQ": falling(30000.0), "GOLD": flat(4000.0, MOVE_AT)}
    service, _, _ = desk(MOVE_AT, series)
    [note] = await service.tick()
    assert note.id == "move:20261020T10" and note.instrument == "NQ"


async def test_gold_has_its_own_three_hour_move_limit():
    series = {"NQ": flat(30000.0, MOVE_AT), "GOLD": falling(4000.0)}
    service, clock, _ = desk(MOVE_AT, series)
    assert [n.id for n in await service.tick()] == ["move:GOLD:20261020T10"]

    # An hour on, NQ and gold fall: gold is inside its 3 hours; NQ writes.
    later = MOVE_AT + timedelta(hours=1)
    series.update(NQ=falling(30000.0, later), GOLD=falling(4000.0, later))
    clock["now"] = later
    assert [n.id for n in await service.tick()] == ["move:20261020T11"]

    # Past gold's 3 hours but inside NQ's: only gold writes again.
    last = MOVE_AT + timedelta(hours=3, minutes=5)
    series.update(NQ=falling(30000.0, last), GOLD=falling(4000.0, last))
    clock["now"] = last
    [note] = await service.tick()
    assert note.id.startswith("move:GOLD:") and note.id != "move:GOLD:20261020T10"
    assert service.store.count_since("move", MOVE_AT.isoformat(), instrument="GOLD") == 2
    assert service.store.count_since("move", MOVE_AT.isoformat(), instrument="NQ") == 1


async def test_gold_move_is_not_written_when_gold_is_not_enabled():
    series = {"NQ": flat(30000.0, MOVE_AT), "GOLD": falling(4000.0)}
    service, _, _ = desk(MOVE_AT, series, instruments=("NQ", "ES", "YM"))
    assert await service.tick() == []
    assert service.writer.calls == []


async def test_no_gold_premarket_is_scheduled():
    # Tuesday 09:05 ET: inside the pre-market window.
    now = datetime(2026, 10, 20, 13, 5, tzinfo=UTC)
    service, _, _ = desk(now)
    jobs = await service.due_jobs(now)
    assert [j[0] for j in jobs if j[1] is NoteKind.PREMARKET] == ["premarket:2026-10-20"]
    await service.tick()
    pre = [n for n in service.store.recent(include_failed=True) if n.kind is NoteKind.PREMARKET]
    assert [(n.id, n.instrument) for n in pre] == [("premarket:2026-10-20", "NQ")]


# --------------------------------------------------------------------------- POST /desk/ask


def client(service, focus: str | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(desk_api.router)
    app.state.desk = service
    if focus is not None:
        store = WorkspaceStore(":memory:")
        store.set_focus(FocusUpdate(symbol=focus))
        app.state.workspace = store
    return TestClient(app)


def test_ask_writes_for_gold_when_enabled():
    service, _, _ = desk(MOVE_AT)
    response = client(service).post("/api/v1/desk/ask", params={"instrument": "GOLD"})
    assert response.status_code == 200
    body = response.json()
    assert body["id"].startswith("ask:GOLD:") and body["instrument"] == "GOLD"
    assert body["tone_for"] == "GOLD"
    [(kind, _)] = service.writer.calls
    assert kind is NoteKind.ASK


def test_ask_on_a_gold_focus_writes_gold_not_nq():
    service, _, _ = desk(MOVE_AT)
    response = client(service, focus="GOLD").post("/api/v1/desk/ask")
    assert response.status_code == 200
    assert response.json()["id"].startswith("ask:GOLD:")
    assert response.json()["instrument"] == "GOLD"


def test_ask_for_gold_includes_gold_in_the_last_two_hours():
    series = {"GOLD": [SimpleNamespace(timestamp=MOVE_AT - timedelta(minutes=180 - i),
                                       value=4000.0 - i * 0.1) for i in range(180)]}
    service, _, _ = desk(MOVE_AT, series)
    assert client(service).post("/api/v1/desk/ask",
                                params={"instrument": "GOLD"}).status_code == 200
    [(_, facts)] = service.writer.calls
    assert "Gold" in [m["market"] for m in facts["last_two_hours"]]


def test_ask_refuses_gold_when_the_desk_does_not_write_for_it():
    service, _, _ = desk(MOVE_AT, instruments=("NQ", "ES", "YM"))
    response = client(service, focus="GOLD").post("/api/v1/desk/ask")
    assert response.status_code == 400
    assert response.json()["detail"] == "The desk does not write for GOLD"
    assert service.writer.calls == [] and service.store.recent(include_failed=True) == []


# --------------------------------------------------------------------------- snapshots


def gold_facts(writer: SentWriter, kind: NoteKind) -> dict:
    return next(f for k, s, f in writer.sent if k is kind and s == "GOLD")


def quotes(service) -> None:
    service.sources.market_state().instruments.update({i.symbol: i for i in (
        inst("NQ", 30120.5, change_pct=0.62), inst("ES", 6810.25, change_pct=0.31),
        gold_quote(change_pct=-0.84, change=-34.0))})


async def test_v9_gold_snapshots():
    now = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
    gold = [SimpleNamespace(timestamp=now - timedelta(minutes=80) + timedelta(minutes=i),
                            value=4050.0 * (1 - 0.0002 * i)) for i in range(80)]
    writer = SentWriter()
    service, _, _ = desk(now, {"GOLD": gold}, instruments=("NQ", "GOLD"), writer=writer,
                         rows=cpi_rows())
    quotes(service)
    await service.tick()
    facts = gold_facts(writer, NoteKind.MOVE)
    assert facts["the_move"]["market"] == "Gold"
    check("v9_gold_move.txt", text(P.render(NoteKind.MOVE, GOLD, facts, version=P.V9)))

    writer = SentWriter()
    service, _, _ = desk(now, instruments=("NQ", "GOLD"), writer=writer, rows=cpi_rows())
    quotes(service)
    await service.ask(GOLD)
    facts = gold_facts(writer, NoteKind.ASK)
    check("v9_gold_ask.txt", text(P.render(NoteKind.ASK, GOLD, facts, version=P.V9)))
