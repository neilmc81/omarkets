"""Strip desk coverage after GOLD (and WTI) is DXY: the US Dollar Index is wired
through the desk like GOLD.

DXY is in the production default on desk:v9, before the yields. DXY gets move
and ask notes with
its own 3-hour move limit, never a pre-market (its profile has no pre-market
text at all), and its notes are written from its own prompt and say they are
about the dollar index, never NQ. DXY is already on the Today strip, so its
notes read its own quote there; NQ, ES, YM, GOLD and WTI notes are unchanged."""

import hashlib
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
from core.domain.instruments import AssetClass, QuoteType, get_instrument
from core.domain.models import FeedStatus
from core.domain.workspace import FocusUpdate
from core.providers import cnbc
from core.providers.registry import DEFAULT_ROUTES
from core.storage.workspace import WorkspaceStore
from tests.desk.test_complete_inputs import inst, state
from tests.desk.test_identity import MOVE_AT, falling
from tests.desk.test_prompt_snapshot import check
from tests.desk.test_prompt_v9 import V9Writer as SentWriter
from tests.desk.test_prompt_v9 import text
from tests.desk.test_service import FakeWriter, cpi_rows, make

DXY = ID.Instrument.of("DXY")
#: Desk instruments without DXY: the defaults on main before it.
BEFORE = ("NQ", "ES", "YM", "GOLD", "WTI")
ALL = (*BEFORE, "DXY")
#: The profiles that must not change: every v9 profile on main before DXY.
OTHERS = ("NQ", "ES", "YM", "GOLD", "WTI")


class V9Writer(FakeWriter):
    prompt_version = "desk:v9"


class V8Writer(FakeWriter):
    prompt_version = "desk:v8"


def desk(now, series=None, instruments=ALL, writer=None, **kwargs):
    return make(now, series=series, writer=writer or V9Writer(), instruments=instruments,
                **kwargs)


def flat(value, now):
    return falling(value, now, step=0.0)


def dxy_quote(value=99.842, **kwargs):
    # As CNBC serves .DXY: an index with no declared delay.
    kwargs = {"feed": FeedStatus.LIVE, "delay": 0.0, **kwargs}
    return inst("DXY", value, asset=AssetClass.FX_INDEX, quote=QuoteType.INDEX, **kwargs)


def gold_quote(value=4012.5, **kwargs):
    return inst("GOLD", value, asset=AssetClass.COMMODITY, **kwargs)


def wti_quote(value=71.2, **kwargs):
    return inst("WTI", value, asset=AssetClass.COMMODITY, **kwargs)


# --------------------------------------------------------------------------- defaults


def test_dxy_follows_wti_in_the_default_desk_on_desk_v9():
    assert ID.PRODUCTION_INSTRUMENTS[:6] == ALL
    assert ID.PRODUCTION_INSTRUMENTS[:5] == ("NQ", "ES", "YM", "GOLD", "WTI")
    assert ID.PRODUCTION_INSTRUMENTS[5] == "DXY"
    settings = Settings(_env_file=None)
    assert settings.desk_instruments[:6] == list(ALL)
    assert settings.desk_prompt_version == "desk:v9"
    assert all(P.has_profile(s, version=P.V9) for s in settings.desk_instruments)


def test_dxy_gets_move_and_ask_only():
    assert ID.instrument_kinds(DXY) == {NoteKind.MOVE, NoteKind.ASK}
    assert ID.note_id(NoteKind.MOVE, "20261020T10", DXY) == "move:DXY:20261020T10"
    assert ID.parse_note_id("ask:DXY:20261020T100000").instrument == "DXY"
    assert DXY.label == "US dollar"


def test_dxy_on_desk_v8_is_refused_before_anything_runs():
    base, _, _ = make(MOVE_AT)
    with pytest.raises(ValueError, match="no desk:v8 prompt for DXY"):
        DeskService(sources=base.sources, store=base.store, writer=V8Writer(),
                    instruments=("NQ", "DXY"))
    assert not P.has_profile("DXY", version=P.V8)


def test_dxy_is_the_existing_cnbc_index_feed_on_fx_index_defaults():
    # The live feed is the one the strip already uses: CNBC's .DXY, with no
    # declared delay. No new feed.
    assert DEFAULT_ROUTES["DXY"] == "cnbc"
    assert cnbc.SYMBOL_MAP["DXY"] == ".DXY"
    assert "DXY" not in cnbc.EXPECTED_DELAY_SECONDS
    dollar = get_instrument("DXY")
    assert dollar.asset_class is AssetClass.FX_INDEX and dollar.venue == "ICE"
    assert dollar.quote_type is QuoteType.INDEX and dollar.currency is None
    assert P.SPECS["DXY"].asset is AssetClass.FX_INDEX
    stated = P.PROMPTS[P.V9]["DXY"].stated_facts
    # Not the futures' 10-minute delay, and no exchange settle: an index has a
    # close, which the shared location facts still label "Prior settle".
    assert "10 minutes delayed" not in stated and "declares no delay" in stated
    assert "18:00 ET to 17:00 ET" in stated
    assert "no exchange sets a settle for it" in stated
    assert ("its change today is measured from the previous session's close, and its "
            '"Prior settle" level means that close') in stated


def test_fx_index_defaults_leave_the_other_asset_classes_alone():
    assert {AssetClass.EQUITY_INDEX_FUTURE, AssetClass.COMMODITY,
            AssetClass.FX_INDEX} <= set(P.ASSET_DEFAULTS)
    assert P.ASSET_DEFAULTS[AssetClass.FX_INDEX].rates_rule is not None


# --------------------------------------------------------------------------- prompt


@pytest.mark.parametrize("kind", [NoteKind.MOVE, NoteKind.ASK])
def test_dxy_notes_use_dxy_s_own_v9_prompt(kind):
    prompt = P.render(kind, DXY, {})
    profile = P.PROMPTS[P.V9]["DXY"]
    assert prompt.instrument == "DXY" and prompt.system == profile.system
    assert all(prompt.system != P.PROMPTS[P.V9][s].system for s in OTHERS)
    assert "move the US Dollar Index (DXY)" in prompt.system
    assert "ICE US Dollar Index" in prompt.system
    assert 'In the facts it is called "US dollar"' in prompt.system
    assert "dxy_price_location" in prompt.system and '"dxy_is"' in prompt.system
    assert "nq_price_location" not in prompt.system and "gold" not in prompt.system.lower()
    assert "supportive or negative for DXY" in prompt.system
    assert "DXY" in prompt.task and "NQ has" not in prompt.task
    assert "megacap_participation" not in prompt.task


def test_dxy_s_profile_has_no_premarket_at_all():
    assert set(P.PROMPTS[P.V9]["DXY"].tasks) == {NoteKind.MOVE, NoteKind.ASK}
    with pytest.raises(P.NoPromptProfile, match="no desk:v9 premarket prompt for DXY"):
        P.render(NoteKind.PREMARKET, DXY, {})
    # Event notes stay desk-wide on NQ's profile.
    assert P.render(NoteKind.FED, None, {}).system == P.PROMPTS[P.V9]["NQ"].system


def test_dxy_says_what_the_index_is():
    system = P.PROMPTS[P.V9]["DXY"].system
    assert "basket of six major currencies, weighted by trade" in system
    assert "the euro (more than half of the basket)" in system
    assert "the Japanese yen, the British pound, the Canadian dollar, the Swedish krona " \
           "and the Swiss franc" in system
    assert "A rising DXY means the dollar is strengthening against them" in system
    assert "often moves as the mirror image of the euro" in system


def test_dxy_s_tone_is_defined_without_advice():
    system = P.PROMPTS[P.V9]["DXY"].system
    # What a rising and falling dollar mean, and what usually drives each.
    assert '"supportive" means pushing the dollar index up' in system
    assert '"negative" means pushing it down; neither is advice' in system
    assert "US interest rates rise compared with other countries'" in system
    assert 'keep rates higher for longer, which traders call "hawkish"' in system
    assert "an inflation surprise pushes back expected Fed rate cuts" in system
    assert "the US economy looks stronger than the rest of the world" in system
    assert 'a "flight to the dollar"' in system
    assert "investors are more willing to take risk" in system
    assert "tendencies, not rules" in system
    assert "show the dollar moving against them, say so plainly" in system
    # The shared no-advice rule is still there.
    assert "2. No trading advice." in system


def test_dxy_names_safe_haven_demand_only_from_the_facts():
    about = P.PROMPTS[P.V9]["DXY"].system.split("\n\n")[1]
    assert "Geopolitical shocks can lift it as a safe haven" in about
    assert ("mention that only when the supplied headlines or facts show it, and never "
            "invent or assume one") in about


def test_yields_matter_for_the_dollar():
    profile = P.PROMPTS[P.V9]["DXY"]
    assert "10. Bond yields matter for DXY: the dollar tends to follow" in profile.system
    assert '(the "rate differential")' in profile.system
    # Only US yields are supplied, so the gap itself is never claimed.
    assert "The facts supply US yields only, not other countries'" in profile.system
    assert "never claim the gap itself widened or narrowed" in profile.system
    assert "- rates_read: max 70 words on what the 2-year and 10-year yields did and " \
           "why that matters for DXY." in profile.system
    assert "not a main driver" not in profile.system
    assert profile.rates_read_required
    assert P.render(NoteKind.ASK, DXY, {}).rates_read_required is True


def test_dxy_ask_asks_for_yields_and_the_dollar_s_own_drivers():
    # Yields matter, so the ask task asks for them; it never asks what "the
    # dollar" is doing alongside DXY, because DXY is the dollar.
    task = P.PROMPTS[P.V9]["DXY"].tasks[NoteKind.ASK]
    assert "what the 2-year and 10-year yields are doing" in task
    assert "what yields and the dollar are doing" not in task
    assert "headlines on the Fed, other central banks, US growth or geopolitics" in task
    assert "say if the dollar is moving against what they would usually suggest" in task


def test_dxy_system_reads_like_an_fx_desk_not_an_equity_one():
    system = P.PROMPTS[P.V9]["DXY"].system
    intro = system.split("\n\n")[0]
    assert "company earnings" not in intro
    assert ("the Federal Reserve, interest rates and bond yields, economic data, appetite "
            "for risk and world news move the US Dollar Index (DXY)") in intro
    # Data surprises are read through the Fed path, never as "helpful for cuts".
    assert "helpful for rate cuts" not in system
    assert "hotter inflation than expected can push back expected Fed rate cuts, which " \
           "often lifts the dollar" in system
    # No "fear gauge where it has one" followed by "there is none".
    assert "where it has one" not in system
    assert "no dollar or currency volatility index is supplied" in system


#: Digests of the NQ, ES, YM, GOLD and WTI desk:v9 profiles (system, stated facts,
#: rates_read_required and every task) as on main before DXY. DXY's wording is
#: its own: none of it may leak into another instrument's prompt.
MAIN_PROFILE_DIGESTS = {
    "NQ": "cdcb8aff0a45c44a9d56e16eaaf1c3c441a45bfeaf6c7fa6693745112b0ff0ad",
    "ES": "87ea2d6b78494cc773aaa2289124f30023c80a464f69a9f46eb8af4531596b4c",
    "YM": "94aec356da80a4cad0d8f44c347ad884cd36c5e6ebb45e90d9d1767df24478a4",
    "GOLD": "8cc57beb3b1ef6fadb43b90a04eb040fd19c3513f5f6af2f64dcb860307344fa",
    "WTI": "9a4c618eb42ad28280cb1e2362f2b82c030bc0b7fc712ab8ebbc7469088372df",
}


def profile_digest(profile: P.Profile) -> str:
    parts = [profile.system, profile.stated_facts, repr(profile.rates_read_required)]
    parts += [f"{kind.value}\n{task}" for kind, task in
              sorted(profile.tasks.items(), key=lambda item: item[0].value)]
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


@pytest.mark.parametrize("symbol", OTHERS)
def test_other_v9_profiles_are_byte_identical_to_main(symbol):
    assert profile_digest(P.PROMPTS[P.V9][symbol]) == MAIN_PROFILE_DIGESTS[symbol]


def test_dxy_prompt_says_borrowed_facts_are_about_other_markets():
    system = P.PROMPTS[P.V9]["DXY"].system
    assert "no fear gauge: no dollar or currency volatility index is supplied" in system
    assert "never use the VIX or VXN, which are stock-market gauges, as the dollar's" \
        in system
    assert 'Dollar notes get no "nq_vs_es"' in system
    assert "use them only as context on investors' appetite for risk" in system
    assert "say they are stock markets, not the dollar" in system
    assert 'Dollar notes get no "megacap_participation"' in system
    assert '"for_nq" line is written for NQ (the Nasdaq 100), not the dollar' in system
    assert "if you use it, say it is about NQ" in system
    assert '"comparable"' not in system


def test_dxy_spec_has_no_participation_and_move_ask_kinds():
    spec = P.SPECS["DXY"]
    assert spec.participation is False and "DXY" not in F.PARTICIPATION_INSTRUMENTS
    assert spec.kinds == {NoteKind.MOVE, NoteKind.ASK}
    assert not spec.events


# --------------------------------------------------------------------------- facts


def test_dxy_has_no_fear_gauge_and_no_nq_es_context():
    assert "DXY" not in F.VOL_INDEXES
    assert F.participation(state(), "DXY") == {}


def test_a_dxy_note_reads_its_own_quote_from_the_strip_once():
    assert "DXY" in F.STRIP_SYMBOLS
    service, _, _ = desk(MOVE_AT)
    service.sources.market_state().instruments.update(
        NQ=inst("NQ", 29_000.0, at=MOVE_AT), DXY=dxy_quote(at=MOVE_AT))
    dxy_facts, _ = service._common(MOVE_AT, DXY)
    nq_facts, _ = service._common(MOVE_AT)
    rows = [row["market"] for row in dxy_facts["markets_today"]]
    assert rows.count("US dollar") == 1
    assert dxy_facts["markets_today"] == nq_facts["markets_today"]
    [dollar] = [row for row in dxy_facts["markets_today"] if row["market"] == "US dollar"]
    assert dollar["change_from"] == "the previous session's close"
    assert dollar["expected_delay"] == "none" and dollar["delayed"] is False


def test_dxy_context_has_no_vix_and_no_nq_es_blocks():
    service, _, _ = desk(MOVE_AT)
    service.sources.market_state().instruments.update(
        VIX=inst("VIX", 18.0, at=MOVE_AT, asset=AssetClass.VOLATILITY_INDEX),
        VXN=inst("VXN", 22.0, at=MOVE_AT, asset=AssetClass.VOLATILITY_INDEX),
        DXY=dxy_quote(at=MOVE_AT))
    context = service._nq_context(MOVE_AT, DXY)
    assert "nq_vs_es" not in context and "megacap_participation" not in context
    normal = context.get("normal_move_size", {})
    assert not {"vix", "vxn", "vix_as_of", "vxn_as_of"} & set(normal)


async def ask_facts(instruments, symbols=OTHERS):
    """What the ``symbols`` ask notes are written from, on a desk of ``instruments``."""
    writer = SentWriter()
    service, _, _ = desk(MOVE_AT, writer=writer, instruments=instruments, rows=cpi_rows())
    service.sources.market_state().instruments.update({i.symbol: i for i in (
        inst("NQ", 30120.5, change_pct=0.62), inst("ES", 6810.25, change_pct=0.31),
        inst("YM", 47_100.0, change_pct=0.2), gold_quote(), wti_quote(change_pct=-1.1),
        dxy_quote(change_pct=0.31, change=0.31))})
    for symbol in symbols:
        await service.ask(ID.Instrument.of(symbol))
    return [P.facts_text(f) for _, _, f in writer.sent]


async def test_enabling_dxy_leaves_nq_es_ym_gold_and_wti_facts_byte_for_byte():
    with_dxy = await ask_facts(ALL)
    assert len(with_dxy) == len(OTHERS)
    assert with_dxy == await ask_facts(BEFORE)


# --------------------------------------------------------------------------- precision


def dxy_size():
    """A normal DXY day: the whole session moves well under one index point."""
    from tests.desk.test_normal_move import size
    return size(typical_range=0.42, typical_range_pct=0.4207, today_range=0.62,
                today_range_pct=0.6211)


def test_dxy_s_sub_one_point_ranges_keep_their_decimals():
    facts = F.normal_move(dxy_size(), None, None, trigger_pct=0.3, trigger_scaled=True,
                          gauge="", benchmark=None, symbol="DXY")
    assert facts["typical_daily_range"] == "0.420 points (0.42%)"
    assert facts["session_range_so_far"].endswith(
        "session: 0.620 points (0.62%), 148% of a typical day")


def test_a_dxy_note_states_its_range_at_quote_precision_and_nq_s_is_unchanged():
    from tests.desk.test_normal_move import size
    service, _, _ = desk(MOVE_AT)
    service.sources.market_state().instruments.update(DXY=dxy_quote(at=MOVE_AT))
    service._move_size = lambda now, inst=ID.NQ: dxy_size() if inst == DXY else size()
    dollar = service._nq_context(MOVE_AT, DXY)["normal_move_size"]
    assert dollar["typical_daily_range"] == "0.420 points (0.42%)"
    nq = service._nq_context(MOVE_AT)["normal_move_size"]
    assert nq["typical_daily_range"] == "451 points (1.46%)"


def level(key, value, text):
    return SimpleNamespace(key=key, value=value, label=key, value_text=text,
                           window_start=None, window_end=None, partial=False)


def test_dxy_s_price_location_uses_its_three_decimal_quotes():
    levels = SimpleNamespace(symbol="DXY", levels=[
        level("prior_settle", 99.512, "99.512"), level("prior_day_high", 99.844, "99.844")])
    location = F.price_location(levels, 99.514)
    assert location["dxy_price"] == "99.514"
    assert [row["dxy_is"] for row in location["levels"]] == [
        "0.002 points above", "0.330 points below"]


def test_other_instruments_keep_their_two_decimal_location_text():
    levels = SimpleNamespace(symbol="WTI", levels=[level("prior_settle", 71.0, "71.00")])
    location = F.price_location(levels, 71.2)
    assert location["wti_price"] == "$71.20"
    assert location["levels"][0]["wti_is"] == "0.20 points above"


def location_rule(symbol: str) -> str:
    system = P.PROMPTS[P.V9][symbol].system
    return next(line for line in system.splitlines() if line.startswith("15. "))


def test_dxy_s_location_rule_speaks_of_its_own_session_not_futures():
    rule = location_rule("DXY")
    assert "futures" not in rule
    assert "the overnight high and low (since the session reopened at 18:00 ET;" in rule
    assert "the prior day's high and low (the previous full session, 18:00 ET to " \
        "17:00 ET)" in rule


@pytest.mark.parametrize("symbol", OTHERS)
def test_the_futures_keep_their_futures_location_rule(symbol):
    rule = location_rule(symbol)
    assert "(since futures reopened;" in rule
    assert "(the previous full futures session)" in rule


# --------------------------------------------------------------------------- moves


async def test_dxy_move_fires_when_the_stock_indexes_and_gold_are_flat():
    series = {"NQ": flat(30000.0, MOVE_AT), "ES": flat(6000.0, MOVE_AT),
              "YM": flat(47000.0, MOVE_AT), "GOLD": flat(4000.0, MOVE_AT),
              "DXY": falling(100.0)}
    service, _, _ = desk(MOVE_AT, series)
    [note] = await service.tick()
    assert note.id == "move:DXY:20261020T10" and note.instrument == "DXY"
    assert note.title.startswith("DXY down") and note.tone_for == "DXY"
    [(kind, facts)] = service.writer.calls
    assert kind is NoteKind.MOVE
    assert facts["the_move"]["market"] == "US dollar"
    others = [m["market"] for m in facts["other_markets_same_window"]]
    assert others.count("US dollar") == 1
    assert "nq_vs_es" not in facts and "megacap_participation" not in facts


async def test_nq_move_fires_when_dxy_is_flat():
    series = {"NQ": falling(30000.0), "DXY": flat(100.0, MOVE_AT)}
    service, _, _ = desk(MOVE_AT, series)
    [note] = await service.tick()
    assert note.id == "move:20261020T10" and note.instrument == "NQ"


async def test_dxy_has_its_own_three_hour_move_limit():
    series = {"NQ": flat(30000.0, MOVE_AT), "DXY": falling(100.0)}
    service, clock, _ = desk(MOVE_AT, series)
    assert [n.id for n in await service.tick()] == ["move:DXY:20261020T10"]

    # An hour on, NQ and the dollar fall: DXY is inside its 3 hours; NQ writes.
    later = MOVE_AT + timedelta(hours=1)
    series.update(NQ=falling(30000.0, later), DXY=falling(100.0, later))
    clock["now"] = later
    assert [n.id for n in await service.tick()] == ["move:20261020T11"]

    # Past DXY's 3 hours but inside NQ's: only DXY writes again.
    last = MOVE_AT + timedelta(hours=3, minutes=5)
    series.update(NQ=falling(30000.0, last), DXY=falling(100.0, last))
    clock["now"] = last
    [note] = await service.tick()
    assert note.id.startswith("move:DXY:") and note.id != "move:DXY:20261020T10"
    assert service.store.count_since("move", MOVE_AT.isoformat(), instrument="DXY") == 2
    assert service.store.count_since("move", MOVE_AT.isoformat(), instrument="NQ") == 1


async def test_dxy_and_gold_moves_are_limited_separately():
    series = {"NQ": flat(30000.0, MOVE_AT), "GOLD": falling(4000.0), "DXY": falling(100.0)}
    service, _, _ = desk(MOVE_AT, series)
    ids = sorted(n.id for n in await service.tick())
    assert ids == ["move:DXY:20261020T10", "move:GOLD:20261020T10"]


async def test_dxy_move_is_not_written_when_dxy_is_not_enabled():
    series = {"NQ": flat(30000.0, MOVE_AT), "DXY": falling(100.0)}
    service, _, _ = desk(MOVE_AT, series, instruments=BEFORE)
    assert await service.tick() == []
    assert service.writer.calls == []


async def test_no_dxy_premarket_is_scheduled():
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


def test_ask_writes_for_dxy_when_enabled():
    service, _, _ = desk(MOVE_AT)
    response = client(service).post("/api/v1/desk/ask", params={"instrument": "DXY"})
    assert response.status_code == 200
    body = response.json()
    assert body["id"].startswith("ask:DXY:") and body["instrument"] == "DXY"
    assert body["tone_for"] == "DXY"
    [(kind, _)] = service.writer.calls
    assert kind is NoteKind.ASK


def test_ask_on_a_dxy_focus_writes_dxy_not_nq():
    service, _, _ = desk(MOVE_AT)
    response = client(service, focus="DXY").post("/api/v1/desk/ask")
    assert response.status_code == 200
    assert response.json()["id"].startswith("ask:DXY:")
    assert response.json()["instrument"] == "DXY"


def test_ask_for_dxy_includes_dxy_in_the_last_two_hours_once():
    series = {"DXY": [SimpleNamespace(timestamp=MOVE_AT - timedelta(minutes=180 - i),
                                      value=100.0 - i * 0.001) for i in range(180)]}
    service, _, _ = desk(MOVE_AT, series)
    assert client(service).post("/api/v1/desk/ask",
                                params={"instrument": "DXY"}).status_code == 200
    [(_, facts)] = service.writer.calls
    assert [m["market"] for m in facts["last_two_hours"]].count("US dollar") == 1


def test_ask_refuses_dxy_when_the_desk_does_not_write_for_it():
    service, _, _ = desk(MOVE_AT, instruments=BEFORE)
    response = client(service, focus="DXY").post("/api/v1/desk/ask")
    assert response.status_code == 400
    assert response.json()["detail"] == "The desk does not write for DXY"
    assert service.writer.calls == [] and service.store.recent(include_failed=True) == []


# --------------------------------------------------------------------------- snapshots


def dxy_facts(writer: SentWriter, kind: NoteKind) -> dict:
    return next(f for k, s, f in writer.sent if k is kind and s == "DXY")


def quotes(service) -> None:
    service.sources.market_state().instruments.update({i.symbol: i for i in (
        inst("NQ", 30120.5, change_pct=0.62), inst("ES", 6810.25, change_pct=0.31),
        dxy_quote(change_pct=-0.41, change=-0.411))})


async def test_v9_dxy_snapshots():
    now = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
    dollar = [SimpleNamespace(timestamp=now - timedelta(minutes=80) + timedelta(minutes=i),
                              value=100.25 * (1 - 0.0002 * i)) for i in range(80)]
    writer = SentWriter()
    service, _, _ = desk(now, {"DXY": dollar}, instruments=("NQ", "DXY"), writer=writer,
                         rows=cpi_rows())
    quotes(service)
    await service.tick()
    facts = dxy_facts(writer, NoteKind.MOVE)
    assert facts["the_move"]["market"] == "US dollar"
    check("v9_dxy_move.txt", text(P.render(NoteKind.MOVE, DXY, facts, version=P.V9)))

    writer = SentWriter()
    service, _, _ = desk(now, instruments=("NQ", "DXY"), writer=writer, rows=cpi_rows())
    quotes(service)
    await service.ask(DXY)
    facts = dxy_facts(writer, NoteKind.ASK)
    check("v9_dxy_ask.txt", text(P.render(NoteKind.ASK, DXY, facts, version=P.V9)))
