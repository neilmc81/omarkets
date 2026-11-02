"""Strip desk coverage after DXY is the Treasury yields: US2Y, then US10Y, are
wired through the desk like DXY.

Both yields are in the production default on desk:v9, after DXY. Each gets move
and ask notes with its own 3-hour move limit and a move trigger in basis points,
never a pre-market (their profiles have no pre-market text at all), and its notes
are written from its own prompt and say they are about that yield, never NQ. The
yields are already on the Today strip, so their notes read their own quotes
there; NQ, ES, YM, GOLD, WTI and DXY notes are unchanged."""

import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.analysis.move_size import MoveSize
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
from tests.desk.test_complete_inputs import inst
from tests.desk.test_identity import MOVE_AT, falling
from tests.desk.test_prompt_snapshot import check
from tests.desk.test_prompt_v9 import V9Writer as SentWriter
from tests.desk.test_prompt_v9 import text
from tests.desk.test_service import FakeWriter, cpi_rows, make

YIELDS = ("US2Y", "US10Y")
US2Y = ID.Instrument.of("US2Y")
US10Y = ID.Instrument.of("US10Y")
#: Desk instruments without the yields: the defaults on main before them.
BEFORE = ("NQ", "ES", "YM", "GOLD", "WTI", "DXY")
ALL = (*BEFORE, *YIELDS)
#: The profiles that must not change: every v9 profile on main before the yields.
OTHERS = BEFORE
#: As the facts and the strip name them.
LABEL = {"US2Y": "2-year yield", "US10Y": "10-year yield"}
#: The other yield, which the curve is measured against.
OTHER = {"US2Y": "US10Y", "US10Y": "US2Y"}
YEARS = {"US2Y": "2-year", "US10Y": "10-year"}


class V9Writer(FakeWriter):
    prompt_version = "desk:v9"


class V8Writer(FakeWriter):
    prompt_version = "desk:v8"


def desk(now, series=None, instruments=ALL, writer=None, **kwargs):
    return make(now, series=series, writer=writer or V9Writer(), instruments=instruments,
                **kwargs)


def flat(value, now=MOVE_AT):
    return falling(value, now, step=0.0)


def drifting(start, bp_per_hour, now=MOVE_AT):
    """A yield falling ``bp_per_hour`` basis points an hour over the last 80 minutes."""
    return [SimpleNamespace(timestamp=now - timedelta(minutes=80) + timedelta(minutes=i),
                            value=start - bp_per_hour / 100 * i / 60) for i in range(80)]


def yield_quote(symbol, value=None, **kwargs):
    # As CNBC serves US2Y/US10Y: a yield in percent with no declared delay; the
    # change is in percentage points (0.031 = 3.1bp).
    value = {"US2Y": 3.912, "US10Y": 4.281}[symbol] if value is None else value
    kwargs = {"feed": FeedStatus.LIVE, "delay": 0.0, "change": 0.031, "change_pct": 0.8,
              **kwargs}
    return inst(symbol, value, asset=AssetClass.TREASURY_YIELD, quote=QuoteType.YIELD,
                **kwargs)


def dxy_quote(value=99.842, **kwargs):
    kwargs = {"feed": FeedStatus.LIVE, "delay": 0.0, **kwargs}
    return inst("DXY", value, asset=AssetClass.FX_INDEX, quote=QuoteType.INDEX, **kwargs)


def system(symbol):
    return P.PROMPTS[P.V9][symbol].system


def rule(symbol: str, number: int) -> str:
    return next(line for line in system(symbol).splitlines() if line.startswith(f"{number}. "))


# --------------------------------------------------------------------------- defaults


def test_the_yields_are_last_in_the_default_desk_us2y_then_us10y():
    assert ID.PRODUCTION_INSTRUMENTS == ALL
    assert ID.PRODUCTION_INSTRUMENTS[-2:] == ("US2Y", "US10Y")
    settings = Settings(_env_file=None)
    assert settings.desk_instruments == list(ALL)
    assert settings.desk_prompt_version == "desk:v9"
    assert all(P.has_profile(s, version=P.V9) for s in settings.desk_instruments)


@pytest.mark.parametrize("symbol", YIELDS)
def test_each_yield_gets_move_and_ask_only(symbol):
    instrument = ID.Instrument.of(symbol)
    assert ID.instrument_kinds(instrument) == {NoteKind.MOVE, NoteKind.ASK}
    assert ID.note_id(NoteKind.MOVE, "20261020T10", instrument) == f"move:{symbol}:20261020T10"
    assert ID.parse_note_id(f"ask:{symbol}:20261020T100000").instrument == symbol
    assert instrument.label == LABEL[symbol]


@pytest.mark.parametrize("symbol", YIELDS)
def test_a_yield_on_desk_v8_is_refused_before_anything_runs(symbol):
    base, _, _ = make(MOVE_AT)
    with pytest.raises(ValueError, match=f"no desk:v8 prompt for {symbol}"):
        DeskService(sources=base.sources, store=base.store, writer=V8Writer(),
                    instruments=("NQ", symbol))
    assert not P.has_profile(symbol, version=P.V8)


@pytest.mark.parametrize("symbol", YIELDS)
def test_each_yield_is_the_existing_cnbc_feed_on_treasury_yield_defaults(symbol):
    # The live feed is the one the strip already uses: CNBC's US2Y/US10Y, with
    # no declared delay. No new feed.
    assert DEFAULT_ROUTES[symbol] == "cnbc"
    assert cnbc.SYMBOL_MAP[symbol] == symbol
    assert symbol not in cnbc.EXPECTED_DELAY_SECONDS
    definition = get_instrument(symbol)
    assert definition.asset_class is AssetClass.TREASURY_YIELD and definition.venue == "OTC"
    assert definition.quote_type is QuoteType.YIELD and definition.currency is None
    assert definition.unit == "percent" and definition.price_precision == 3
    assert P.SPECS[symbol].asset is AssetClass.TREASURY_YIELD
    stated = P.PROMPTS[P.V9][symbol].stated_facts
    # Not the futures' 10-minute delay, and no exchange settle: a yield has a
    # close, which the shared location facts still label "Prior settle".
    assert "10 minutes delayed" not in stated and "declares no delay" in stated
    assert "18:00 ET to 17:00 ET" in stated
    assert "not a futures contract" in stated and "no exchange sets a settle for it" in stated
    assert ("its change today is measured in basis points from the previous session's "
            'close, and its "Prior settle" level means that close') in stated


def test_treasury_yield_defaults_leave_the_other_asset_classes_alone():
    assert set(P.ASSET_DEFAULTS) == {AssetClass.EQUITY_INDEX_FUTURE, AssetClass.COMMODITY,
                                     AssetClass.FX_INDEX, AssetClass.TREASURY_YIELD}
    yields = P.ASSET_DEFAULTS[AssetClass.TREASURY_YIELD]
    assert yields.rates_rule is not None
    assert yields.rates_rule != P.ASSET_DEFAULTS[AssetClass.FX_INDEX].rates_rule


# --------------------------------------------------------------------------- prompt


@pytest.mark.parametrize("kind", [NoteKind.MOVE, NoteKind.ASK])
@pytest.mark.parametrize("symbol", YIELDS)
def test_each_yield_s_notes_use_its_own_v9_prompt(symbol, kind):
    prompt = P.render(kind, ID.Instrument.of(symbol), {})
    sym = symbol.lower()
    assert prompt.instrument == symbol and prompt.system == system(symbol)
    assert all(prompt.system != system(s) for s in (*OTHERS, OTHER[symbol]))
    assert f"move the US {YEARS[symbol]} Treasury yield ({symbol})" in prompt.system
    assert f'In the facts it is called "{LABEL[symbol]}"' in prompt.system
    assert f"{sym}_price_location" in prompt.system and f'"{sym}_is"' in prompt.system
    assert "nq_price_location" not in prompt.system and "gold" not in prompt.system.lower()
    assert f"supportive or negative for {symbol}" in prompt.system
    assert symbol in prompt.task and "NQ has" not in prompt.task
    assert "megacap_participation" not in prompt.task


@pytest.mark.parametrize("symbol", YIELDS)
def test_a_yield_s_profile_has_no_premarket_at_all(symbol):
    assert set(P.PROMPTS[P.V9][symbol].tasks) == {NoteKind.MOVE, NoteKind.ASK}
    with pytest.raises(P.NoPromptProfile, match=f"no desk:v9 premarket prompt for {symbol}"):
        P.render(NoteKind.PREMARKET, ID.Instrument.of(symbol), {})
    # Event notes stay desk-wide on NQ's profile.
    assert P.render(NoteKind.FED, None, {}).system == system("NQ")


@pytest.mark.parametrize("symbol", YIELDS)
def test_each_yield_says_what_it_is(symbol):
    about = system(symbol).split("\n\n")[1]
    assert f"the yield on the {YEARS[symbol]} US Treasury note" in about
    assert "constant-maturity yield" in about and "not a futures price" in about
    assert "Yields move the opposite way to bond prices" in about
    assert "its moves are measured in basis points" in about


def test_the_2_year_is_the_fed_path_yield():
    about = system("US2Y").split("\n\n")[1]
    assert "the Treasury yield most tied to the Federal Reserve" in about
    assert 'keep its rate higher for longer ("hawkish")' in about
    assert 'cuts sooner or deeper ("dovish")' in about
    assert "hotter inflation or stronger jobs data" in about
    assert 'a "flight to quality"' in about
    assert "heavy Treasury supply" in about and "deficits" in about
    assert "supply usually matters more for longer maturities such as the 10-year" in about


def test_the_10_year_is_the_growth_and_term_premium_yield():
    about = system("US10Y").split("\n\n")[1]
    assert "the benchmark long-term US interest rate" in about
    assert "long-run growth and inflation" in about
    assert "a term premium" in about
    assert "worries about deficits, heavy Treasury supply (big auctions)" in about
    assert "weak demand at Treasury auctions" in about
    assert "less tied to the Fed's next few meetings than the 2-year" in about
    assert 'a "flight to quality"' in about


@pytest.mark.parametrize("symbol", YIELDS)
def test_each_yield_s_tone_is_defined_without_advice(symbol):
    text_ = system(symbol)
    assert (f'"supportive" means pushing the {YEARS[symbol]} yield up (rates higher) and '
            '"negative" means pushing it down; neither is advice') in text_
    assert "higher yields are not good or bad in themselves" in text_
    assert "tendencies, not rules" in text_
    assert f"show the {YEARS[symbol]} yield moving against them, say so plainly" in text_
    # The shared no-advice rule is still there.
    assert "2. No trading advice." in text_


@pytest.mark.parametrize("symbol", YIELDS)
def test_a_yield_s_rates_rule_is_about_itself_and_the_curve(symbol):
    # The instrument IS a yield: no "yields matter for US10Y" as if they were an
    # outside input, and no "not a main driver" either.
    profile = P.PROMPTS[P.V9][symbol]
    rates = rule(symbol, 10)
    assert f"Bond yields matter for {symbol}" not in profile.system
    assert "usually weigh on stocks" not in profile.system
    assert "not a main driver" not in profile.system
    assert f"connect it to {symbol}" not in profile.system
    assert f"10. {symbol} is itself a Treasury yield" in rates
    assert '"bond_market" lists it with the other' in rates
    assert "the curve (the 10-year minus the 2-year)" in rates
    assert "Never work out a spread yourself" in rates
    assert "do not claim one yield moved the other" in rates
    # rates_read is about the yields together, never "why that matters for US10Y".
    assert "why that matters for" not in profile.system
    assert (f"- rates_read: max 70 words on how {symbol} moved alongside the "
            f"{YEARS[OTHER[symbol]]} yield and what the supplied curve did") in profile.system
    assert profile.rates_read_required
    assert P.render(NoteKind.ASK, ID.Instrument.of(symbol), {}).rates_read_required is True


def test_the_2_year_s_curve_link_is_the_fed_end():
    rates = rule("US2Y", 10)
    assert "The 2-year sits closest to the Fed's rate" in rates
    assert "When the 2-year rises more than the 10-year, the curve flattens" in rates


def test_the_10_year_s_curve_link_is_the_long_end():
    rates = rule("US10Y", 10)
    assert "The 10-year carries more of the long-run growth, inflation and term-premium " \
           "story" in rates
    assert "When the 10-year rises more than the 2-year, the curve steepens" in rates


@pytest.mark.parametrize("symbol", YIELDS)
def test_a_yield_s_ask_asks_for_the_other_yield_and_the_curve_not_itself(symbol):
    task = P.PROMPTS[P.V9][symbol].tasks[NoteKind.ASK]
    other = YEARS[OTHER[symbol]]
    assert f"what the {other} yield and the curve are doing (bond_market)" in task
    assert f"what the {YEARS[symbol]} yield" not in task
    assert "what yields and the dollar are doing" not in task
    assert "2-year and 10-year yields are doing" not in task
    assert ("any headlines on the Fed, inflation, growth, Treasury auctions or supply, or "
            "world news") in task
    assert f"say if the {YEARS[symbol]} yield is moving against what they would usually " \
           "suggest" in task


@pytest.mark.parametrize("symbol", YIELDS)
def test_a_yield_s_system_reads_like_a_rates_desk_not_an_equity_one(symbol):
    text_ = system(symbol)
    intro = text_.split("\n\n")[0]
    assert "company earnings" not in intro
    # Data surprises are read through the Fed path, never as "helpful for cuts".
    assert "helpful for rate cuts" not in text_
    assert "hotter inflation than expected can push back expected Fed rate cuts, which " \
           "often lifts yields" in text_
    assert "rush into the safety of Treasuries can pull yields down even on hot data" in text_
    # No "fear gauge where it has one" followed by "there is none".
    assert "where it has one" not in text_
    assert "no Treasury volatility index (such as the MOVE index) is supplied" in text_


@pytest.mark.parametrize("symbol", YIELDS)
def test_a_yield_s_move_size_rule_is_in_basis_points_without_realised_volatility(symbol):
    size = rule(symbol, 16)
    assert "the typical daily range (high to low, in basis points)" in size
    assert "realised volatility" not in size


#: Digests of the NQ, ES, YM, GOLD, WTI and DXY desk:v9 profiles (system, stated
#: facts, rates_read_required and every task) as on main before the yields.
MAIN_PROFILE_DIGESTS = {
    "NQ": "cdcb8aff0a45c44a9d56e16eaaf1c3c441a45bfeaf6c7fa6693745112b0ff0ad",
    "ES": "87ea2d6b78494cc773aaa2289124f30023c80a464f69a9f46eb8af4531596b4c",
    "YM": "94aec356da80a4cad0d8f44c347ad884cd36c5e6ebb45e90d9d1767df24478a4",
    "GOLD": "8cc57beb3b1ef6fadb43b90a04eb040fd19c3513f5f6af2f64dcb860307344fa",
    "WTI": "9a4c618eb42ad28280cb1e2362f2b82c030bc0b7fc712ab8ebbc7469088372df",
    "DXY": "328e597edd3cbced3d48fe8f314700a55573eba80ce0f7f48a6f2d10ac576949",
}


def profile_digest(profile: P.Profile) -> str:
    parts = [profile.system, profile.stated_facts, repr(profile.rates_read_required)]
    parts += [f"{kind.value}\n{task}" for kind, task in
              sorted(profile.tasks.items(), key=lambda item: item[0].value)]
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


@pytest.mark.parametrize("symbol", OTHERS)
def test_other_v9_profiles_are_byte_identical_to_main(symbol):
    assert profile_digest(P.PROMPTS[P.V9][symbol]) == MAIN_PROFILE_DIGESTS[symbol]


@pytest.mark.parametrize("symbol", YIELDS)
def test_a_yield_s_prompt_says_borrowed_facts_are_about_other_markets(symbol):
    text_ = system(symbol)
    assert "never use the VIX or VXN, which are stock-market gauges, as a yield's" in text_
    assert 'Yield notes get no "nq_vs_es"' in text_
    assert "use stocks only as context on investors' appetite for risk" in text_
    assert "the dollar as context on the Fed and US rates" in text_
    assert "say they are other markets, not the yield" in text_
    assert 'Yield notes get no "megacap_participation"' in text_
    assert '"for_nq" line is written for NQ (the Nasdaq 100), not yields' in text_
    assert "if you use it, say it is about NQ" in text_
    assert '"comparable"' not in text_


@pytest.mark.parametrize("symbol", YIELDS)
def test_a_yield_s_spec_has_no_participation_and_move_ask_kinds(symbol):
    spec = P.SPECS[symbol]
    assert spec.participation is False and symbol not in F.PARTICIPATION_INSTRUMENTS
    assert spec.kinds == {NoteKind.MOVE, NoteKind.ASK}
    assert not spec.events


@pytest.mark.parametrize("symbol", YIELDS)
def test_a_yield_s_location_rule_speaks_of_its_own_session_not_futures(symbol):
    location = rule(symbol, 15)
    assert "futures" not in location
    assert "the overnight high and low (since the session reopened at 18:00 ET;" in location
    assert "the prior day's high and low (the previous full session, 18:00 ET to " \
        "17:00 ET)" in location


# --------------------------------------------------------------------------- facts


@pytest.mark.parametrize("symbol", YIELDS)
def test_a_yield_has_no_fear_gauge_and_no_nq_es_context(symbol):
    assert symbol not in F.VOL_INDEXES
    assert F.participation(SimpleNamespace(instruments={}), symbol) == {}


@pytest.mark.parametrize("symbol", YIELDS)
def test_a_yield_note_reads_its_own_quote_from_the_strip_once(symbol):
    assert symbol in F.STRIP_SYMBOLS
    service, _, _ = desk(MOVE_AT)
    service.sources.market_state().instruments.update(
        {"NQ": inst("NQ", 29_000.0, at=MOVE_AT), "US2Y": yield_quote("US2Y", at=MOVE_AT),
         "US10Y": yield_quote("US10Y", at=MOVE_AT)})
    own, _ = service._common(MOVE_AT, ID.Instrument.of(symbol))
    nq, _ = service._common(MOVE_AT)
    rows = [row["market"] for row in own["markets_today"]]
    assert rows.count(LABEL[symbol]) == 1
    assert own["markets_today"] == nq["markets_today"]
    [row] = [row for row in own["markets_today"] if row["market"] == LABEL[symbol]]
    assert row["change_today"] == "+3.1bp" and row["level"].endswith("%")
    assert row["change_from"] == "the previous session's close"
    assert row["expected_delay"] == "none" and row["delayed"] is False
    # Both yields and the curve are in the bond market facts.
    names = [line["name"] for line in own["bond_market"]["yields"]]
    assert names == ["2-year yield", "10-year yield", "Curve (10y minus 2y)"]


@pytest.mark.parametrize("symbol", YIELDS)
def test_a_yield_s_context_has_no_vix_and_no_nq_es_blocks(symbol):
    service, _, _ = desk(MOVE_AT)
    service.sources.market_state().instruments.update(
        VIX=inst("VIX", 18.0, at=MOVE_AT, asset=AssetClass.VOLATILITY_INDEX),
        VXN=inst("VXN", 22.0, at=MOVE_AT, asset=AssetClass.VOLATILITY_INDEX),
        **{symbol: yield_quote(symbol, at=MOVE_AT)})
    context = service._nq_context(MOVE_AT, ID.Instrument.of(symbol))
    assert "nq_vs_es" not in context and "megacap_participation" not in context
    normal = context.get("normal_move_size", {})
    assert not {"vix", "vxn", "vix_as_of", "vxn_as_of"} & set(normal)


async def ask_facts(instruments, symbols=OTHERS):
    """What the ``symbols`` ask notes are written from, on a desk of ``instruments``."""
    writer = SentWriter()
    service, _, _ = desk(MOVE_AT, writer=writer, instruments=instruments, rows=cpi_rows())
    service.ask_daily_limit = len(symbols)  # six asks; the default allows five a day
    service.sources.market_state().instruments.update({i.symbol: i for i in (
        inst("NQ", 30120.5, change_pct=0.62), inst("ES", 6810.25, change_pct=0.31),
        inst("YM", 47_100.0, change_pct=0.2),
        inst("GOLD", 4012.5, asset=AssetClass.COMMODITY),
        inst("WTI", 71.2, asset=AssetClass.COMMODITY, change_pct=-1.1),
        dxy_quote(change_pct=0.31, change=0.31), yield_quote("US2Y"), yield_quote("US10Y"))})
    for symbol in symbols:
        await service.ask(ID.Instrument.of(symbol))
    return [P.facts_text(f) for _, _, f in writer.sent]


async def test_enabling_the_yields_leaves_every_other_instrument_s_facts_byte_for_byte():
    with_yields = await ask_facts(ALL)
    assert len(with_yields) == len(OTHERS)
    assert with_yields == await ask_facts(BEFORE)


# --------------------------------------------------------------------------- basis points


def yield_size(**kwargs):
    """A normal 10-year day: about 7bp high to low (0.072 percentage points)."""
    values = dict(trade_date=MOVE_AT.date(), sessions=14, typical_range=0.072,
                  typical_range_pct=1.68, realised_vol_pct=31.4, today_range=0.09,
                  today_range_pct=2.1, today_partial=True, today_complete=False)
    return MoveSize(**{**values, **kwargs})


def test_a_yield_s_move_trigger_is_in_basis_points_scaled_to_its_range():
    # 0.55 of a 7.2bp typical day, kept between 4bp and 10bp.
    assert yield_size().trigger_bp() == 4.0
    assert yield_size(typical_range=0.10).trigger_bp() == 5.5
    assert yield_size(typical_range=0.40).trigger_bp() == 10.0
    assert yield_size(typical_range=None, typical_range_pct=None).trigger_bp() == 6.0
    # The percent trigger the others use is untouched.
    assert yield_size().trigger_pct(0.8) == 0.92


@pytest.mark.parametrize("symbol", YIELDS)
def test_a_yield_s_normal_move_size_is_in_basis_points(symbol):
    facts = F.normal_move(yield_size(), None, None, trigger_pct=4.0, trigger_scaled=True,
                          gauge="", benchmark=None, symbol=symbol)
    assert facts == {
        "typical_daily_range": "7.2bp",
        "typical_measured_over": "median of the last 14 complete sessions",
        "session_range_so_far": "Tue Oct 20 session: 9.0bp, 125% of a typical day",
        "large_move_trigger": "4.0bp in one hour, scaled to the typical daily range",
    }


def test_a_yield_note_states_its_range_in_bp_and_nq_s_is_unchanged():
    from tests.desk.test_normal_move import size
    service, _, _ = desk(MOVE_AT)
    service.sources.market_state().instruments.update(US10Y=yield_quote("US10Y", at=MOVE_AT))
    service._move_size = lambda now, inst=ID.NQ: yield_size() if inst == US10Y else size()
    ten = service._nq_context(MOVE_AT, US10Y)["normal_move_size"]
    assert ten["typical_daily_range"] == "7.2bp"
    assert ten["large_move_trigger"] == "4.0bp in one hour, scaled to the typical daily range"
    nq = service._nq_context(MOVE_AT)["normal_move_size"]
    assert nq["typical_daily_range"] == "451 points (1.46%)"
    assert nq["large_move_trigger"] == "0.80% in one hour, scaled to the typical daily range"


def level(key, value, text_):
    return SimpleNamespace(key=key, value=value, label=key, value_text=text_,
                           window_start=None, window_end=None, partial=False)


def test_a_yield_s_price_location_is_in_percent_and_basis_points():
    levels = SimpleNamespace(symbol="US10Y", levels=[
        level("prior_settle", 4.258, "4.258"), level("prior_day_high", 4.3, "4.300"),
        level("prior_day_low", 4.281, "4.281")])
    location = F.price_location(levels, 4.281)
    assert location["us10y_price"] == "4.281%"
    assert [(row["value"], row["us10y_is"]) for row in location["levels"]] == [
        ("4.258%", "2.3bp above"), ("4.300%", "1.9bp below"), ("4.281%", "at")]


def test_other_instruments_keep_their_location_text():
    levels = SimpleNamespace(symbol="DXY", levels=[level("prior_settle", 99.512, "99.512")])
    location = F.price_location(levels, 99.514)
    assert location["dxy_price"] == "99.514"
    assert location["levels"][0] == {"level": "prior_settle", "value": "99.512",
                                     "dxy_is": "0.002 points above"}


# --------------------------------------------------------------------------- moves


@pytest.mark.parametrize("symbol", YIELDS)
async def test_a_yield_move_fires_when_everything_else_is_flat(symbol):
    series = {"NQ": flat(30000.0), "ES": flat(6000.0), "DXY": flat(100.0),
              OTHER[symbol]: flat(4.0), symbol: drifting(4.0, 8.0)}
    service, _, _ = desk(MOVE_AT, series)
    [note] = await service.tick()
    assert note.id == f"move:{symbol}:20261020T10" and note.instrument == symbol
    assert note.title == f"{symbol} down 9.9bp in the last hour"
    assert note.tone_for == symbol
    [(kind, facts)] = service.writer.calls
    assert kind is NoteKind.MOVE
    assert facts["the_move"]["market"] == LABEL[symbol]
    assert facts["the_move"]["change"] == "-9.9bp"
    others = [m["market"] for m in facts["other_markets_same_window"]]
    assert others.count(LABEL[symbol]) == 1
    assert "nq_vs_es" not in facts and "megacap_participation" not in facts


@pytest.mark.parametrize("symbol", YIELDS)
async def test_an_ordinary_yield_hour_does_not_fire(symbol):
    # 2.5bp in an hour is routine for a yield. The percent trigger the others
    # use (0.8) must never be read as 0.8bp.
    series = {"NQ": flat(30000.0), symbol: drifting(4.0, 2.0)}
    service, _, _ = desk(MOVE_AT, series)
    assert await service.tick() == []
    assert service.writer.calls == []


async def test_nq_move_fires_when_the_yields_are_flat():
    series = {"NQ": falling(30000.0), "US2Y": flat(3.9), "US10Y": flat(4.3)}
    service, _, _ = desk(MOVE_AT, series)
    [note] = await service.tick()
    assert note.id == "move:20261020T10" and note.instrument == "NQ"


@pytest.mark.parametrize("symbol", YIELDS)
async def test_each_yield_has_its_own_three_hour_move_limit(symbol):
    series = {"NQ": flat(30000.0), symbol: drifting(4.0, 8.0)}
    service, clock, _ = desk(MOVE_AT, series)
    assert [n.id for n in await service.tick()] == [f"move:{symbol}:20261020T10"]

    # An hour on, NQ and the yield fall: the yield is inside its 3 hours; NQ writes.
    later = MOVE_AT + timedelta(hours=1)
    series.update({"NQ": falling(30000.0, later), symbol: drifting(4.0, 8.0, later)})
    clock["now"] = later
    assert [n.id for n in await service.tick()] == ["move:20261020T11"]

    # Past the yield's 3 hours but inside NQ's: only the yield writes again.
    last = MOVE_AT + timedelta(hours=3, minutes=5)
    series.update({"NQ": falling(30000.0, last), symbol: drifting(4.0, 8.0, last)})
    clock["now"] = last
    [note] = await service.tick()
    assert note.id.startswith(f"move:{symbol}:") and note.id != f"move:{symbol}:20261020T10"
    assert service.store.count_since("move", MOVE_AT.isoformat(), instrument=symbol) == 2
    assert service.store.count_since("move", MOVE_AT.isoformat(), instrument="NQ") == 1


async def test_the_2_year_and_10_year_moves_are_limited_separately():
    series = {"NQ": flat(30000.0), "US2Y": drifting(3.9, 8.0), "US10Y": drifting(4.3, 8.0)}
    service, _, _ = desk(MOVE_AT, series)
    ids = sorted(n.id for n in await service.tick())
    assert ids == ["move:US10Y:20261020T10", "move:US2Y:20261020T10"]


async def test_a_yield_move_is_not_written_when_the_yields_are_not_enabled():
    series = {"NQ": flat(30000.0), "US2Y": drifting(3.9, 8.0), "US10Y": drifting(4.3, 8.0)}
    service, _, _ = desk(MOVE_AT, series, instruments=BEFORE)
    assert await service.tick() == []
    assert service.writer.calls == []


async def test_no_yield_premarket_is_scheduled():
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


@pytest.mark.parametrize("symbol", YIELDS)
def test_ask_writes_for_each_yield_when_enabled(symbol):
    service, _, _ = desk(MOVE_AT)
    response = client(service).post("/api/v1/desk/ask", params={"instrument": symbol})
    assert response.status_code == 200
    body = response.json()
    assert body["id"].startswith(f"ask:{symbol}:") and body["instrument"] == symbol
    assert body["tone_for"] == symbol
    [(kind, _)] = service.writer.calls
    assert kind is NoteKind.ASK


@pytest.mark.parametrize("symbol", YIELDS)
def test_ask_on_a_yield_focus_writes_that_yield_not_nq(symbol):
    service, _, _ = desk(MOVE_AT)
    response = client(service, focus=symbol).post("/api/v1/desk/ask")
    assert response.status_code == 200
    assert response.json()["id"].startswith(f"ask:{symbol}:")
    assert response.json()["instrument"] == symbol


@pytest.mark.parametrize("symbol", YIELDS)
def test_ask_for_a_yield_includes_it_in_the_last_two_hours_once(symbol):
    series = {symbol: [SimpleNamespace(timestamp=MOVE_AT - timedelta(minutes=180 - i),
                                       value=4.0 - i * 0.0002) for i in range(180)]}
    service, _, _ = desk(MOVE_AT, series)
    assert client(service).post("/api/v1/desk/ask",
                                params={"instrument": symbol}).status_code == 200
    [(_, facts)] = service.writer.calls
    rows = [m for m in facts["last_two_hours"] if m["market"] == LABEL[symbol]]
    assert len(rows) == 1 and rows[0]["change"].endswith("bp")


@pytest.mark.parametrize("symbol", YIELDS)
def test_ask_refuses_a_yield_when_the_desk_does_not_write_for_it(symbol):
    service, _, _ = desk(MOVE_AT, instruments=BEFORE)
    response = client(service, focus=symbol).post("/api/v1/desk/ask")
    assert response.status_code == 400
    assert response.json()["detail"] == f"The desk does not write for {symbol}"
    assert service.writer.calls == [] and service.store.recent(include_failed=True) == []


# --------------------------------------------------------------------------- snapshots


def own_facts(writer: SentWriter, kind: NoteKind, symbol: str) -> dict:
    return next(f for k, s, f in writer.sent if k is kind and s == symbol)


def quotes(service) -> None:
    service.sources.market_state().instruments.update({i.symbol: i for i in (
        inst("NQ", 30120.5, change_pct=0.62), inst("ES", 6810.25, change_pct=0.31),
        dxy_quote(change_pct=0.18, change=0.18),
        yield_quote("US2Y", 3.884, change=-0.071, change_pct=-1.8),
        yield_quote("US10Y", 4.236, change=-0.052, change_pct=-1.2))})


@pytest.mark.parametrize("symbol", YIELDS)
async def test_v9_yield_snapshots(symbol):
    now = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
    instrument = ID.Instrument.of(symbol)
    sym = symbol.lower()
    writer = SentWriter()
    service, _, _ = desk(now, {symbol: drifting(4.0, 8.0, now)}, instruments=("NQ", symbol),
                         writer=writer, rows=cpi_rows())
    quotes(service)
    await service.tick()
    facts = own_facts(writer, NoteKind.MOVE, symbol)
    assert facts["the_move"]["market"] == LABEL[symbol]
    check(f"v9_{sym}_move.txt", text(P.render(NoteKind.MOVE, instrument, facts, version=P.V9)))

    writer = SentWriter()
    service, _, _ = desk(now, instruments=("NQ", symbol), writer=writer, rows=cpi_rows())
    quotes(service)
    await service.ask(instrument)
    facts = own_facts(writer, NoteKind.ASK, symbol)
    check(f"v9_{sym}_ask.txt", text(P.render(NoteKind.ASK, instrument, facts, version=P.V9)))
