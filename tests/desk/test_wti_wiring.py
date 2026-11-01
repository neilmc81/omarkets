"""Strip desk coverage after GOLD is WTI: crude oil is wired through the desk
like GOLD.

WTI is in the production default on desk:v9, before DXY. WTI gets move
and ask notes with its own 3-hour move limit, never a pre-market (its profile
has no pre-market text at all), and its notes are written from its own prompt
and say they are about oil, never NQ. WTI is already on the Today strip, so its
notes read its own quote there; NQ, ES, YM and GOLD notes are unchanged."""

import hashlib
import json
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
from core.domain.instruments import AssetClass, get_instrument
from core.domain.workspace import FocusUpdate
from core.providers import cnbc
from core.providers.registry import DEFAULT_ROUTES
from core.storage.workspace import WorkspaceStore
from tests.desk.test_complete_inputs import inst, state
from tests.desk.test_identity import MOVE_AT, falling
from tests.desk.test_prompt_snapshot import check
from tests.desk.test_prompt_v9 import GOOD, Wire, text, v9_writer
from tests.desk.test_prompt_v9 import V9Writer as SentWriter
from tests.desk.test_service import FakeWriter, cpi_rows, make

WTI = ID.Instrument.of("WTI")
ALL = ("NQ", "ES", "YM", "GOLD", "WTI")
BEFORE = ("NQ", "ES", "YM", "GOLD")


class V9Writer(FakeWriter):
    prompt_version = "desk:v9"


class V8Writer(FakeWriter):
    prompt_version = "desk:v8"


def desk(now, series=None, instruments=ALL, writer=None, **kwargs):
    return make(now, series=series, writer=writer or V9Writer(), instruments=instruments,
                **kwargs)


def flat(value, now):
    return falling(value, now, step=0.0)


def wti_quote(value=71.2, **kwargs):
    return inst("WTI", value, asset=AssetClass.COMMODITY, **kwargs)


def gold_quote(value=4012.5, **kwargs):
    return inst("GOLD", value, asset=AssetClass.COMMODITY, **kwargs)


# --------------------------------------------------------------------------- defaults


def test_the_default_desk_includes_wti_on_desk_v9():
    assert ID.PRODUCTION_INSTRUMENTS[:5] == ALL
    settings = Settings(_env_file=None)
    assert settings.desk_instruments[:5] == list(ALL)
    assert settings.desk_prompt_version == "desk:v9"
    assert all(P.has_profile(s, version=P.V9) for s in settings.desk_instruments)


def test_wti_gets_move_and_ask_only():
    assert ID.instrument_kinds(WTI) == {NoteKind.MOVE, NoteKind.ASK}
    assert ID.note_id(NoteKind.MOVE, "20261020T10", WTI) == "move:WTI:20261020T10"
    assert ID.parse_note_id("ask:WTI:20261020T100000").instrument == "WTI"
    assert WTI.label == "Crude oil"


def test_wti_on_desk_v8_is_refused_before_anything_runs():
    base, _, _ = make(MOVE_AT)
    with pytest.raises(ValueError, match="no desk:v8 prompt for WTI"):
        DeskService(sources=base.sources, store=base.store, writer=V8Writer(),
                    instruments=("NQ", "WTI"))
    assert not P.has_profile("WTI", version=P.V8)


def test_wti_is_the_existing_delayed_cnbc_oil_feed_on_commodity_defaults():
    # The live feed is the one the strip already uses: CNBC's active CL contract,
    # declared about 10 minutes delayed. No new feed.
    assert DEFAULT_ROUTES["WTI"] == "cnbc"
    assert cnbc.EXPECTED_DELAY_SECONDS["WTI"] == 600
    oil = get_instrument("WTI")
    assert oil.asset_class is AssetClass.COMMODITY and oil.venue == "NYMEX"
    assert P.SPECS["WTI"].asset is AssetClass.COMMODITY
    assert "Prices are about 10 minutes delayed" in P.PROMPTS[P.V9]["WTI"].stated_facts


# --------------------------------------------------------------------------- prompt


@pytest.mark.parametrize("kind", [NoteKind.MOVE, NoteKind.ASK])
def test_wti_notes_use_wti_s_own_v9_prompt(kind):
    prompt = P.render(kind, WTI, {})
    profile = P.PROMPTS[P.V9]["WTI"]
    assert prompt.instrument == "WTI" and prompt.system == profile.system
    assert all(prompt.system != P.PROMPTS[P.V9][s].system for s in BEFORE)
    assert "move Crude oil (WTI)" in prompt.system
    assert "NYMEX West Texas Intermediate crude oil futures" in prompt.system
    assert "US dollars per barrel" in prompt.system
    assert "wti_price_location" in prompt.system and '"wti_is"' in prompt.system
    assert "nq_price_location" not in prompt.system and "gold" not in prompt.system.lower()
    assert "supportive or negative for WTI" in prompt.system
    assert "WTI" in prompt.task and "NQ has" not in prompt.task
    assert "megacap_participation" not in prompt.task


def test_wti_s_profile_has_no_premarket_at_all():
    assert set(P.PROMPTS[P.V9]["WTI"].tasks) == {NoteKind.MOVE, NoteKind.ASK}
    with pytest.raises(P.NoPromptProfile, match="no desk:v9 premarket prompt for WTI"):
        P.render(NoteKind.PREMARKET, WTI, {})
    # Event notes stay desk-wide on NQ's profile.
    assert P.render(NoteKind.FED, None, {}).system == P.PROMPTS[P.V9]["NQ"].system


def test_wti_s_tone_is_defined_without_advice():
    system = P.PROMPTS[P.V9]["WTI"].system
    # What rising and falling oil mean, and what usually drives each.
    assert '"supportive" means pushing oil\'s price up' in system
    assert '"negative" means pushing it down; neither is advice' in system
    assert "Oil often rises when supply looks tighter" in system
    assert "OPEC+" in system and "lower US production" in system
    assert "demand looks stronger (faster economic growth, especially in China)" in system
    assert "the US dollar weakens" in system
    assert "investors are more willing to take risk" in system
    assert "tendencies, not rules" in system
    assert "show oil moving against them, say so plainly" in system
    # Inventory reports only when the facts carry them.
    assert "(from the API and the EIA)" in system
    assert "mention them only when they are in the supplied facts" in system
    # The shared no-advice rule is still there.
    assert "2. No trading advice." in system


def test_yields_are_not_a_main_driver_of_oil():
    profile = P.PROMPTS[P.V9]["WTI"]
    assert "10. Bond yields are not a main driver of WTI" in profile.system
    assert "- rates_read: leave this field out; yields are not a main driver of WTI." \
        in profile.system
    assert "Bond yields matter for WTI" not in profile.system
    assert not P.SPECS["WTI"].yields_matter and not profile.rates_read_required
    assert P.render(NoteKind.ASK, WTI, {}).rates_read_required is False
    # GOLD keeps its commodity yields rule; the flag is WTI's alone.
    assert P.PROMPTS[P.V9]["GOLD"].rates_read_required
    assert all(spec.yields_matter for symbol, spec in P.SPECS.items() if symbol != "WTI")


@pytest.mark.parametrize("kind", [NoteKind.MOVE, NoteKind.ASK])
def test_wti_tasks_never_demand_yields_commentary(kind):
    # Rule 10 makes yields optional for oil, so no WTI task may ask for them
    # outright (the shared ask task asked for "what yields and the dollar are doing").
    task = P.PROMPTS[P.V9]["WTI"].tasks[kind]
    assert "yield" not in task.lower()


def test_wti_ask_asks_for_oil_s_own_drivers():
    task = P.PROMPTS[P.V9]["WTI"].tasks[NoteKind.ASK]
    assert "the US dollar in markets_today, if the facts show it" in task
    assert ("supply (OPEC+, US output, inventories), geopolitics (wars, sanctions, "
            "shipping routes) or demand") in task


def test_wti_names_geopolitics_as_a_driver_only_from_the_facts():
    system = P.PROMPTS[P.V9]["WTI"].system
    intro, about = system.split("\n\n")[0], system.split("\n\n")[1]
    assert "supply, demand, geopolitics, the US dollar" in intro
    # A first-class tendency, not a vague "disruption".
    assert "when geopolitics threatens supply" in about
    assert "a war or conflict in an oil-producing region such as the Middle East" in about
    assert "sanctions on a big oil producer" in about
    assert "the Strait of Hormuz" in about
    assert "when such tensions ease" in about
    # Only from the supplied facts, and never invented.
    assert ("mention it only when the supplied headlines or facts show it, and never "
            "invent or assume a conflict") in about
    assert "tendencies, not rules" in about


def test_wti_system_reads_like_an_oil_desk_not_an_equity_one():
    system = P.PROMPTS[P.V9]["WTI"].system
    intro = system.split("\n\n")[0]
    # What moves oil, not what moves a stock index.
    assert "company earnings" not in intro and "bond yields" not in intro
    assert "the US dollar, economic data and world news move Crude oil (WTI)" in intro
    # No rate-cut framing of data surprises.
    assert "helpful for rate cuts" not in system
    assert "a sign of weaker demand for oil" in system
    # No "fear gauge where it has one" followed by "there is none".
    assert "where it has one" not in system
    assert "no oil volatility index is supplied" in system


#: Digests of the NQ, ES, YM and GOLD desk:v9 profiles (system, stated facts,
#: rates_read_required and every task) as on main before WTI. WTI's wording is
#: its own: none of it may leak into another instrument's prompt.
MAIN_PROFILE_DIGESTS = {
    "NQ": "cdcb8aff0a45c44a9d56e16eaaf1c3c441a45bfeaf6c7fa6693745112b0ff0ad",
    "ES": "87ea2d6b78494cc773aaa2289124f30023c80a464f69a9f46eb8af4531596b4c",
    "YM": "94aec356da80a4cad0d8f44c347ad884cd36c5e6ebb45e90d9d1767df24478a4",
    "GOLD": "8cc57beb3b1ef6fadb43b90a04eb040fd19c3513f5f6af2f64dcb860307344fa",
}


def profile_digest(profile: P.Profile) -> str:
    parts = [profile.system, profile.stated_facts, repr(profile.rates_read_required)]
    parts += [f"{kind.value}\n{task}" for kind, task in
              sorted(profile.tasks.items(), key=lambda item: item[0].value)]
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


@pytest.mark.parametrize("symbol", BEFORE)
def test_other_v9_profiles_are_byte_identical_to_main(symbol):
    assert profile_digest(P.PROMPTS[P.V9][symbol]) == MAIN_PROFILE_DIGESTS[symbol]


async def test_a_wti_note_without_rates_read_is_accepted():
    body = {k: v for k, v in GOOD.items() if k not in ("nq_tone", "rates_read")}
    without = json.dumps(body | {"tone": "negative"})
    result = await v9_writer(Wire(without)).attempt(NoteKind.ASK, {}, instrument=WTI)
    assert result.draft is not None and result.draft.rates_read is None
    assert result.draft.nq_tone == "negative"


def test_wti_prompt_says_borrowed_facts_are_about_other_markets():
    system = P.PROMPTS[P.V9]["WTI"].system
    assert "no fear gauge: no oil volatility index is supplied" in system
    assert "never use the VIX or VXN, which are stock-market gauges, as oil's" in system
    assert 'Oil notes get no "nq_vs_es"' in system
    assert "use them only as context on investors' appetite for risk" in system
    assert "say they are stock markets, not oil" in system
    assert 'Oil notes get no "megacap_participation"' in system
    assert '"for_nq" line is written for NQ (the Nasdaq 100), not oil' in system
    assert "if you use it, say it is about NQ" in system
    assert '"comparable"' not in system


def test_wti_spec_has_no_participation_and_move_ask_kinds():
    spec = P.SPECS["WTI"]
    assert spec.participation is False and "WTI" not in F.PARTICIPATION_INSTRUMENTS
    assert spec.kinds == {NoteKind.MOVE, NoteKind.ASK}
    assert not spec.events


# --------------------------------------------------------------------------- facts


def test_wti_has_no_fear_gauge_and_no_nq_es_context():
    assert "WTI" not in F.VOL_INDEXES
    assert F.participation(state(), "WTI") == {}


def test_a_wti_note_reads_its_own_quote_from_the_strip_once():
    assert "WTI" in F.STRIP_SYMBOLS
    service, _, _ = desk(MOVE_AT)
    service.sources.market_state().instruments.update(
        NQ=inst("NQ", 29_000.0, at=MOVE_AT), WTI=wti_quote(at=MOVE_AT))
    wti_facts, _ = service._common(MOVE_AT, WTI)
    nq_facts, _ = service._common(MOVE_AT)
    rows = [row["market"] for row in wti_facts["markets_today"]]
    assert rows.count("Crude oil") == 1
    assert wti_facts["markets_today"] == nq_facts["markets_today"]
    [oil] = [row for row in wti_facts["markets_today"] if row["market"] == "Crude oil"]
    assert oil["level"] == "$71.20" and oil["change_from"] == "the previous session's settle"


def test_wti_context_has_no_vix_and_no_nq_es_blocks():
    service, _, _ = desk(MOVE_AT)
    service.sources.market_state().instruments.update(
        VIX=inst("VIX", 18.0, at=MOVE_AT, asset=AssetClass.VOLATILITY_INDEX),
        VXN=inst("VXN", 22.0, at=MOVE_AT, asset=AssetClass.VOLATILITY_INDEX),
        WTI=wti_quote(at=MOVE_AT))
    context = service._nq_context(MOVE_AT, WTI)
    assert "nq_vs_es" not in context and "megacap_participation" not in context
    normal = context.get("normal_move_size", {})
    assert not {"vix", "vxn", "vix_as_of", "vxn_as_of"} & set(normal)


async def ask_facts(instruments, symbols=BEFORE):
    """What the ``symbols`` ask notes are written from, on a desk of ``instruments``."""
    writer = SentWriter()
    service, _, _ = desk(MOVE_AT, writer=writer, instruments=instruments, rows=cpi_rows())
    service.sources.market_state().instruments.update({i.symbol: i for i in (
        inst("NQ", 30120.5, change_pct=0.62), inst("ES", 6810.25, change_pct=0.31),
        inst("YM", 47_100.0, change_pct=0.2), gold_quote(), wti_quote(change_pct=-1.1))})
    for symbol in symbols:
        await service.ask(ID.Instrument.of(symbol))
    return [P.facts_text(f) for _, _, f in writer.sent]


async def test_enabling_wti_leaves_nq_es_ym_and_gold_facts_byte_for_byte():
    with_wti = await ask_facts(ALL)
    assert len(with_wti) == len(BEFORE)
    assert with_wti == await ask_facts(BEFORE)


# --------------------------------------------------------------------------- moves


async def test_wti_move_fires_when_the_stock_indexes_and_gold_are_flat():
    series = {"NQ": flat(30000.0, MOVE_AT), "ES": flat(6000.0, MOVE_AT),
              "YM": flat(47000.0, MOVE_AT), "GOLD": flat(4000.0, MOVE_AT),
              "WTI": falling(72.0)}
    service, _, _ = desk(MOVE_AT, series)
    [note] = await service.tick()
    assert note.id == "move:WTI:20261020T10" and note.instrument == "WTI"
    assert note.title.startswith("WTI down") and note.tone_for == "WTI"
    [(kind, facts)] = service.writer.calls
    assert kind is NoteKind.MOVE
    assert facts["the_move"]["market"] == "Crude oil"
    assert "Crude oil" in [m["market"] for m in facts["other_markets_same_window"]]
    assert "nq_vs_es" not in facts and "megacap_participation" not in facts


async def test_nq_move_fires_when_wti_is_flat():
    series = {"NQ": falling(30000.0), "WTI": flat(72.0, MOVE_AT)}
    service, _, _ = desk(MOVE_AT, series)
    [note] = await service.tick()
    assert note.id == "move:20261020T10" and note.instrument == "NQ"


async def test_wti_has_its_own_three_hour_move_limit():
    series = {"NQ": flat(30000.0, MOVE_AT), "WTI": falling(72.0)}
    service, clock, _ = desk(MOVE_AT, series)
    assert [n.id for n in await service.tick()] == ["move:WTI:20261020T10"]

    # An hour on, NQ and oil fall: oil is inside its 3 hours; NQ writes.
    later = MOVE_AT + timedelta(hours=1)
    series.update(NQ=falling(30000.0, later), WTI=falling(72.0, later))
    clock["now"] = later
    assert [n.id for n in await service.tick()] == ["move:20261020T11"]

    # Past oil's 3 hours but inside NQ's: only oil writes again.
    last = MOVE_AT + timedelta(hours=3, minutes=5)
    series.update(NQ=falling(30000.0, last), WTI=falling(72.0, last))
    clock["now"] = last
    [note] = await service.tick()
    assert note.id.startswith("move:WTI:") and note.id != "move:WTI:20261020T10"
    assert service.store.count_since("move", MOVE_AT.isoformat(), instrument="WTI") == 2
    assert service.store.count_since("move", MOVE_AT.isoformat(), instrument="NQ") == 1


async def test_wti_and_gold_moves_are_limited_separately():
    series = {"NQ": flat(30000.0, MOVE_AT), "GOLD": falling(4000.0), "WTI": falling(72.0)}
    service, _, _ = desk(MOVE_AT, series)
    ids = sorted(n.id for n in await service.tick())
    assert ids == ["move:GOLD:20261020T10", "move:WTI:20261020T10"]


async def test_wti_move_is_not_written_when_wti_is_not_enabled():
    series = {"NQ": flat(30000.0, MOVE_AT), "WTI": falling(72.0)}
    service, _, _ = desk(MOVE_AT, series, instruments=BEFORE)
    assert await service.tick() == []
    assert service.writer.calls == []


async def test_no_wti_premarket_is_scheduled():
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


def test_ask_writes_for_wti_when_enabled():
    service, _, _ = desk(MOVE_AT)
    response = client(service).post("/api/v1/desk/ask", params={"instrument": "WTI"})
    assert response.status_code == 200
    body = response.json()
    assert body["id"].startswith("ask:WTI:") and body["instrument"] == "WTI"
    assert body["tone_for"] == "WTI"
    [(kind, _)] = service.writer.calls
    assert kind is NoteKind.ASK


def test_ask_on_a_wti_focus_writes_wti_not_nq():
    service, _, _ = desk(MOVE_AT)
    response = client(service, focus="WTI").post("/api/v1/desk/ask")
    assert response.status_code == 200
    assert response.json()["id"].startswith("ask:WTI:")
    assert response.json()["instrument"] == "WTI"


def test_ask_for_wti_includes_wti_in_the_last_two_hours():
    series = {"WTI": [SimpleNamespace(timestamp=MOVE_AT - timedelta(minutes=180 - i),
                                      value=72.0 - i * 0.01) for i in range(180)]}
    service, _, _ = desk(MOVE_AT, series)
    assert client(service).post("/api/v1/desk/ask",
                                params={"instrument": "WTI"}).status_code == 200
    [(_, facts)] = service.writer.calls
    assert "Crude oil" in [m["market"] for m in facts["last_two_hours"]]


def test_ask_refuses_wti_when_the_desk_does_not_write_for_it():
    service, _, _ = desk(MOVE_AT, instruments=BEFORE)
    response = client(service, focus="WTI").post("/api/v1/desk/ask")
    assert response.status_code == 400
    assert response.json()["detail"] == "The desk does not write for WTI"
    assert service.writer.calls == [] and service.store.recent(include_failed=True) == []


# --------------------------------------------------------------------------- snapshots


def wti_facts(writer: SentWriter, kind: NoteKind) -> dict:
    return next(f for k, s, f in writer.sent if k is kind and s == "WTI")


def quotes(service) -> None:
    service.sources.market_state().instruments.update({i.symbol: i for i in (
        inst("NQ", 30120.5, change_pct=0.62), inst("ES", 6810.25, change_pct=0.31),
        wti_quote(change_pct=-1.84, change=-1.33))})


async def test_v9_wti_snapshots():
    now = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
    oil = [SimpleNamespace(timestamp=now - timedelta(minutes=80) + timedelta(minutes=i),
                           value=72.5 * (1 - 0.0002 * i)) for i in range(80)]
    writer = SentWriter()
    service, _, _ = desk(now, {"WTI": oil}, instruments=("NQ", "WTI"), writer=writer,
                         rows=cpi_rows())
    quotes(service)
    await service.tick()
    facts = wti_facts(writer, NoteKind.MOVE)
    assert facts["the_move"]["market"] == "Crude oil"
    check("v9_wti_move.txt", text(P.render(NoteKind.MOVE, WTI, facts, version=P.V9)))

    writer = SentWriter()
    service, _, _ = desk(now, instruments=("NQ", "WTI"), writer=writer, rows=cpi_rows())
    quotes(service)
    await service.ask(WTI)
    facts = wti_facts(writer, NoteKind.ASK)
    check("v9_wti_ask.txt", text(P.render(NoteKind.ASK, WTI, facts, version=P.V9)))
