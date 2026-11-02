"""Strip desk coverage after the Treasury yields is VIX: the CBOE Volatility
Index is wired through the desk like DXY and the yields.

VIX is last in the production default on desk:v9, after US10Y. It gets move and
ask notes with its own 3-hour move limit and a move trigger sized for a
volatility index, never a pre-market (its profile has no pre-market text at
all), and its notes are written from its own prompt and say they are about VIX,
never NQ. VIX is itself the stock market's fear gauge, so its prompt never
treats "the VIX" as an outside input. VIX is already on the Today strip, so its
notes read its own quote there; every other instrument's notes are unchanged."""

import dataclasses
import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.analysis import move_size as MS
from core.analysis.levels import key_levels
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
from core.domain.sessions import NEW_YORK, globex_session
from core.domain.workspace import FocusUpdate
from core.providers import cnbc
from core.providers.registry import DEFAULT_ROUTES
from core.storage.workspace import WorkspaceStore
from tests.analysis.test_move_size import bucket, ohlc_of, past_days, session_rows
from tests.desk.test_complete_inputs import inst
from tests.desk.test_identity import MOVE_AT, falling
from tests.desk.test_prompt_snapshot import check
from tests.desk.test_prompt_v9 import NO_NUMBERS, text
from tests.desk.test_prompt_v9 import V9Writer as SentWriter
from tests.desk.test_service import FakeWriter, cpi_rows, make

VIX = ID.Instrument.of("VIX")
#: Desk instruments without VIX: the defaults on main before it.
BEFORE = ("NQ", "ES", "YM", "GOLD", "WTI", "DXY", "US2Y", "US10Y")
ALL = (*BEFORE, "VIX")
#: The profiles that must not change: every v9 profile on main before VIX.
OTHERS = BEFORE
#: As the facts and the strip name it.
LABEL = "VIX fear gauge"


class V9Writer(FakeWriter):
    prompt_version = "desk:v9"


class V8Writer(FakeWriter):
    prompt_version = "desk:v8"


def desk(now, series=None, instruments=ALL, writer=None, **kwargs):
    return make(now, series=series, writer=writer or V9Writer(), instruments=instruments,
                **kwargs)


def flat(value, now=MOVE_AT):
    return falling(value, now, step=0.0)


def vix_quote(value=17.42, **kwargs):
    # As CNBC serves .VIX: the Cboe cash index, with no declared delay.
    kwargs = {"feed": FeedStatus.LIVE, "delay": 0.0, "change": 0.61, "change_pct": 3.63,
              **kwargs}
    return inst("VIX", value, asset=AssetClass.VOLATILITY_INDEX, quote=QuoteType.INDEX,
                **kwargs)


def system(symbol="VIX"):
    return P.PROMPTS[P.V9][symbol].system


def rule(number: int, symbol: str = "VIX") -> str:
    return next(line for line in system(symbol).splitlines() if line.startswith(f"{number}. "))


# --------------------------------------------------------------------------- defaults


def test_vix_is_last_in_the_default_desk_after_us10y():
    assert ID.PRODUCTION_INSTRUMENTS == ALL
    assert ID.PRODUCTION_INSTRUMENTS[-2:] == ("US10Y", "VIX")
    settings = Settings(_env_file=None)
    assert settings.desk_instruments == list(ALL)
    assert settings.desk_prompt_version == "desk:v9"
    assert all(P.has_profile(s, version=P.V9) for s in settings.desk_instruments)


def test_vix_gets_move_and_ask_only():
    assert ID.instrument_kinds(VIX) == {NoteKind.MOVE, NoteKind.ASK}
    assert ID.note_id(NoteKind.MOVE, "20261020T10", VIX) == "move:VIX:20261020T10"
    assert ID.parse_note_id("ask:VIX:20261020T100000").instrument == "VIX"
    assert VIX.label == LABEL


def test_vix_on_desk_v8_is_refused_before_anything_runs():
    base, _, _ = make(MOVE_AT)
    with pytest.raises(ValueError, match="no desk:v8 prompt for VIX"):
        DeskService(sources=base.sources, store=base.store, writer=V8Writer(),
                    instruments=("NQ", "VIX"))
    assert not P.has_profile("VIX", version=P.V8)


def test_vix_on_desk_v8_refuses_startup_with_the_fix():
    from core.main import DeskConfigError, check_desk_instruments
    settings = Settings(_env_file=None, desk_prompt_version="desk:v8",
                        desk_instruments=["NQ", "VIX"])
    with pytest.raises(DeskConfigError) as raised:
        check_desk_instruments(settings)
    message = str(raised.value)
    assert "desk_instruments includes VIX, but desk:v8 has no prompt for it" in message
    assert ("set OMARKETS_DESK_PROMPT_VERSION=desk:v9 or remove VIX from "
            "OMARKETS_DESK_INSTRUMENTS") in message


def test_vix_is_the_existing_cnbc_cash_index_on_volatility_index_defaults():
    # The live feed is the one the strip already uses: CNBC's .VIX (the Cboe
    # cash index, not VX futures), with no declared delay. No new feed.
    assert DEFAULT_ROUTES["VIX"] == "cnbc"
    assert cnbc.SYMBOL_MAP["VIX"] == ".VIX"
    assert "VIX" not in cnbc.EXPECTED_DELAY_SECONDS
    vix = get_instrument("VIX")
    assert vix.asset_class is AssetClass.VOLATILITY_INDEX and vix.venue == "CBOE"
    assert vix.quote_type is QuoteType.INDEX and vix.currency is None
    assert vix.unit == "index points" and vix.price_precision == 2
    assert vix.display_name == "CBOE Volatility Index"
    assert P.SPECS["VIX"].asset is AssetClass.VOLATILITY_INDEX
    stated = P.PROMPTS[P.V9]["VIX"].stated_facts
    # Cboe's hours, not the futures' 10-minute delay or 18:00 reopen, and no
    # exchange settle: an index has a close, which the shared location facts
    # still label "Prior settle".
    assert "10 minutes delayed" not in stated and "declares no delay" in stated
    assert "from 03:15 ET to 16:15 ET on trading days" in stated
    assert "over the next 30 days" in stated
    assert "not a futures contract" in stated and "no exchange sets a settle for it" in stated
    assert ("its change today is measured from the previous session's close, and its "
            '"Prior settle" level means that close') in stated


def test_volatility_index_defaults_leave_the_other_asset_classes_alone():
    assert set(P.ASSET_DEFAULTS) == {
        AssetClass.EQUITY_INDEX_FUTURE, AssetClass.COMMODITY, AssetClass.FX_INDEX,
        AssetClass.TREASURY_YIELD, AssetClass.VOLATILITY_INDEX}
    # Yields reach a volatility index through stocks; they are not a main driver.
    assert P.ASSET_DEFAULTS[AssetClass.VOLATILITY_INDEX].rates_rule is None


# --------------------------------------------------------------------------- prompt


@pytest.mark.parametrize("kind", [NoteKind.MOVE, NoteKind.ASK])
def test_vix_notes_use_vix_s_own_v9_prompt(kind):
    prompt = P.render(kind, VIX, {})
    assert prompt.instrument == "VIX" and prompt.system == system()
    assert all(prompt.system != system(s) for s in OTHERS)
    assert "move the CBOE Volatility Index (VIX)" in prompt.system
    assert f'In the facts it is called "{LABEL}"' in prompt.system
    assert "vix_price_location" in prompt.system and '"vix_is"' in prompt.system
    assert "nq_price_location" not in prompt.system
    assert "supportive or negative for VIX" in prompt.system
    assert "VIX" in prompt.task and "NQ has" not in prompt.task
    assert "megacap_participation" not in prompt.task


def test_vix_s_profile_has_no_premarket_at_all():
    assert set(P.PROMPTS[P.V9]["VIX"].tasks) == {NoteKind.MOVE, NoteKind.ASK}
    with pytest.raises(P.NoPromptProfile, match="no desk:v9 premarket prompt for VIX"):
        P.render(NoteKind.PREMARKET, VIX, {})
    # Event notes stay desk-wide on NQ's profile.
    assert P.render(NoteKind.FED, None, {}).system == system("NQ")


def test_vix_says_what_it_is():
    about = system().split("\n\n")[1]
    assert "VIX is the CBOE Volatility Index" in about
    assert "worked out from the prices of S&P 500 index options" in about
    assert "not a VIX futures price" in about
    assert "Its level is in index points (traders also call them vol points)" in about
    assert "VIX points are not NQ's or ES's points" in about


def test_vix_is_itself_the_fear_gauge_not_an_outside_input():
    # The equity notes read VIX as an input ("VXN above VIX means ..."); VIX's
    # own notes never ask what "the VIX" is doing, because VIX is the subject.
    profile = P.PROMPTS[P.V9]["VIX"]
    assert "VIX is itself the stock market's main fear gauge" in profile.system
    assert "never an outside input" in profile.system
    for words in (profile.system, *profile.tasks.values()):
        assert "the VIX" not in words
        assert "VXN above VIX" not in words
    assert "vxn_as_of" not in profile.system


def test_vix_s_tone_is_defined_without_advice_and_never_calls_higher_vix_good():
    about = system().split("\n\n")[1]
    assert '"supportive" means pushing VIX up' in about
    assert '"negative" means pushing it down (calmer markets)' in about
    assert "neither is advice, and neither means good or bad" in about
    assert "a higher VIX is not good news" in about
    assert "tendencies, not rules" in about
    assert "show VIX moving against them" in about
    assert "2. No trading advice." in system()


def test_vix_s_drivers_read_like_an_equity_vol_desk():
    about = system().split("\n\n")[1]
    assert "VIX usually moves the opposite way to the S&P 500" in about
    assert "most of all in a fast selloff" in about
    assert "ahead of a big scheduled event" in about
    assert "inflation report" in about and "geopolitical shocks" in about
    assert "mean reversion" in about
    assert "never predict when or whether it will fall back" in about
    assert "complacency" in about
    intro = system().split("\n\n")[0]
    assert "company earnings" not in intro
    assert ("stock-market swings, worry and appetite for risk, the Federal Reserve, economic "
            "data and world news move the CBOE Volatility Index (VIX)") in intro


def test_yields_are_not_a_main_driver_of_vix():
    profile = P.PROMPTS[P.V9]["VIX"]
    assert ("10. Bond yields are not a main driver of VIX; mention them only if the facts "
            "tie them to this note.") in profile.system
    assert "- rates_read: leave this field out; yields are not a main driver of VIX." \
        in profile.system
    assert not profile.rates_read_required
    assert P.render(NoteKind.ASK, VIX, {}).rates_read_required is False


def test_vix_s_forecast_rule_is_read_through_stocks():
    eleven = rule(11)
    assert "it does not move VIX by itself" in eleven
    assert "whether the surprise unsettles stocks" in eleven
    assert "helpful for rate cuts" not in eleven


def test_vix_s_gauge_rule_invents_no_other_gauge():
    sixteen = rule(16)
    assert ("the typical daily range (high to low, in index points and as a percentage of "
            "its level) and the current or last session's range compared with it (labelled "
            "with its date). It is itself the stock market's fear gauge") in sixteen
    assert "realised volatility" not in sixteen
    assert "MOVE index" in sixteen and "never invent one" in sixteen
    assert "never treat bond yields as a volatility gauge" in sixteen
    assert "VXN" in sixteen and "VVIX" in sixteen


def test_vix_prompt_says_borrowed_facts_are_about_other_markets():
    text_ = system()
    assert 'VIX notes get no "nq_vs_es"' in text_
    assert "say they are stock markets, not VIX" in text_
    assert 'VIX notes get no "megacap_participation"' in text_
    assert '"for_nq" line is written for NQ (the Nasdaq 100), not VIX' in text_
    assert "if you use it, say it is about NQ" in text_
    assert '"comparable"' not in text_


def test_vix_ask_asks_for_stocks_and_what_traders_hedge_for():
    task = P.PROMPTS[P.V9]["VIX"].tasks[NoteKind.ASK]
    assert "how stocks are doing (ES and NQ in markets_today)" in task
    assert "what yields and the dollar are doing" not in task
    assert "traders may be buying protection for" in task
    assert "say if VIX is moving against what stocks would usually suggest" in task


def test_vix_spec_has_no_participation_and_move_ask_kinds():
    spec = P.SPECS["VIX"]
    assert spec.participation is False and "VIX" not in F.PARTICIPATION_INSTRUMENTS
    assert spec.kinds == {NoteKind.MOVE, NoteKind.ASK}
    assert not spec.events


def test_vix_s_location_rule_speaks_of_cboe_hours_not_futures():
    fifteen = rule(15)
    assert fifteen.startswith('15. "vix_price_location" says where VIX is against fixed '
                              'index levels ("vix_is")')
    assert "futures" not in fifteen
    assert "(since Cboe began calculating it at 03:15 ET" in fifteen
    assert ("the prior day's high and low (the previous trading day; Cboe calculated it "
            "only from 03:15 ET to 16:15 ET, though the \"measured\" window is the full "
            "session from 18:00 ET to 17:00 ET)") in fifteen


def test_vix_s_prior_day_wording_matches_the_window_its_levels_are_measured_over():
    # Rule 15 tells the writer to trust each level's "measured" dates, so the
    # prompt must name the window key_levels really uses for VIX's prior day.
    rows = [r for day in past_days(5) for r in cash_rows(day)]
    levels = {lv.key: lv for lv in key_levels("VIX", vix_quote(), ohlc_of(rows), now=NOW).levels}
    day = levels["prior_day_high"]
    assert day.value is not None
    start, end = day.window_start.astimezone(NEW_YORK), day.window_end.astimezone(NEW_YORK)
    assert (start.strftime("%H:%M"), end.strftime("%H:%M")) == ("18:00", "17:00")
    assert "from 18:00 ET to 17:00 ET" in rule(15)


@pytest.mark.parametrize("kind", [NoteKind.MOVE, NoteKind.ASK])
def test_a_vix_prompt_never_states_the_futures_10_minute_delay(kind):
    prompt = P.render(kind, VIX, {})
    assert "10 minutes" not in prompt.system and "10 minutes" not in prompt.number_sources
    from core.desk.evaluation import checker
    note = {**NO_NUMBERS, "summary": "Prices are about 10 minutes delayed."}
    score = checker.score_draft(case_id="x", kind=kind.value, model="m",
                                created_at="2026-10-20T15:00:00+00:00", draft=note, facts={},
                                facts_complete=True, history=[], instrument="VIX",
                                prompt_version="desk:v9")
    assert score.numbers_not_in_facts == ["10"]


#: Digests of every desk:v9 profile on main before VIX (system, stated facts,
#: rates_read_required and every task). VIX's wording is its own: none of it may
#: leak into another instrument's prompt.
MAIN_PROFILE_DIGESTS = {
    "NQ": "cdcb8aff0a45c44a9d56e16eaaf1c3c441a45bfeaf6c7fa6693745112b0ff0ad",
    "ES": "87ea2d6b78494cc773aaa2289124f30023c80a464f69a9f46eb8af4531596b4c",
    "YM": "94aec356da80a4cad0d8f44c347ad884cd36c5e6ebb45e90d9d1767df24478a4",
    "GOLD": "8cc57beb3b1ef6fadb43b90a04eb040fd19c3513f5f6af2f64dcb860307344fa",
    "WTI": "9a4c618eb42ad28280cb1e2362f2b82c030bc0b7fc712ab8ebbc7469088372df",
    "DXY": "328e597edd3cbced3d48fe8f314700a55573eba80ce0f7f48a6f2d10ac576949",
    "US2Y": "ab61e3265ce19c5e8a2d3dce83fc4e714138b3124f475a17eb59baa282d76694",
    "US10Y": "6ce3f4d6390c5034cdb266f9152fa2bdc6d5d76762b9c3991432651de38af4a7",
}


def profile_digest(profile: P.Profile) -> str:
    parts = [profile.system, profile.stated_facts, repr(profile.rates_read_required)]
    parts += [f"{kind.value}\n{task}" for kind, task in
              sorted(profile.tasks.items(), key=lambda item: item[0].value)]
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


@pytest.mark.parametrize("symbol", OTHERS)
def test_other_v9_profiles_are_byte_identical_to_main(symbol):
    assert profile_digest(P.PROMPTS[P.V9][symbol]) == MAIN_PROFILE_DIGESTS[symbol]


# --------------------------------------------------------------------------- facts


def test_vix_has_no_other_gauge_and_no_nq_es_context():
    assert "VIX" not in F.VOL_INDEXES
    assert F.participation(SimpleNamespace(instruments={}), "VIX") == {}


def test_a_vix_note_reads_its_own_quote_from_the_strip_once():
    assert "VIX" in F.STRIP_SYMBOLS
    service, _, _ = desk(MOVE_AT)
    service.sources.market_state().instruments.update(
        NQ=inst("NQ", 29_000.0, at=MOVE_AT), VIX=vix_quote(at=MOVE_AT))
    own, _ = service._common(MOVE_AT, VIX)
    nq, _ = service._common(MOVE_AT)
    rows = [row["market"] for row in own["markets_today"]]
    assert rows.count(LABEL) == 1
    assert own["markets_today"] == nq["markets_today"]
    [row] = [row for row in own["markets_today"] if row["market"] == LABEL]
    assert row["level"] == "17.42" and row["change_today"] == "+3.63%"
    assert row["change_from"] == "the previous session's close"
    assert row["expected_delay"] == "none" and row["delayed"] is False


def test_vix_context_has_no_vxn_and_no_nq_es_blocks():
    service, _, _ = desk(MOVE_AT)
    service.sources.market_state().instruments.update(
        VXN=inst("VXN", 22.0, at=MOVE_AT, asset=AssetClass.VOLATILITY_INDEX),
        VIX=vix_quote(at=MOVE_AT))
    context = service._nq_context(MOVE_AT, VIX)
    assert "nq_vs_es" not in context and "megacap_participation" not in context
    normal = context.get("normal_move_size", {})
    assert not {"vix", "vxn", "vix_as_of", "vxn_as_of", "vxn_minus_vix"} & set(normal)


async def ask_facts(instruments, symbols=OTHERS):
    """What the ``symbols`` ask notes are written from, on a desk of ``instruments``."""
    writer = SentWriter()
    service, _, _ = desk(MOVE_AT, writer=writer, instruments=instruments, rows=cpi_rows())
    service.ask_daily_limit = len(symbols)  # eight asks; the default allows five a day
    service.sources.market_state().instruments.update({i.symbol: i for i in (
        inst("NQ", 30120.5, change_pct=0.62), inst("ES", 6810.25, change_pct=0.31),
        inst("YM", 47_100.0, change_pct=0.2),
        inst("GOLD", 4012.5, asset=AssetClass.COMMODITY),
        inst("WTI", 71.2, asset=AssetClass.COMMODITY, change_pct=-1.1),
        inst("DXY", 99.842, asset=AssetClass.FX_INDEX, quote=QuoteType.INDEX,
             feed=FeedStatus.LIVE, delay=0.0, change_pct=0.31, change=0.31),
        inst("US2Y", 3.912, asset=AssetClass.TREASURY_YIELD, quote=QuoteType.YIELD,
             feed=FeedStatus.LIVE, delay=0.0, change=0.031, change_pct=0.8),
        inst("US10Y", 4.281, asset=AssetClass.TREASURY_YIELD, quote=QuoteType.YIELD,
             feed=FeedStatus.LIVE, delay=0.0, change=0.031, change_pct=0.7),
        vix_quote(),
        inst("VXN", 21.3, asset=AssetClass.VOLATILITY_INDEX, quote=QuoteType.INDEX,
             feed=FeedStatus.LIVE, delay=0.0))})
    for symbol in symbols:
        await service.ask(ID.Instrument.of(symbol))
    return [P.facts_text(f) for _, _, f in writer.sent]


async def test_enabling_vix_leaves_every_other_instrument_s_facts_byte_for_byte():
    with_vix = await ask_facts(ALL)
    assert len(with_vix) == len(OTHERS)
    assert with_vix == await ask_facts(BEFORE)


# --------------------------------------------------------------------------- move size


def vix_size(**kwargs):
    """A normal VIX day: about 1 index point high to low on a 17 level (6%)."""
    values = dict(trade_date=MOVE_AT.date(), sessions=14, typical_range=1.04,
                  typical_range_pct=6.11, realised_vol_pct=None, today_range=1.62,
                  today_range_pct=9.3, today_partial=True, today_complete=False)
    return MoveSize(**{**values, **kwargs})


def test_vix_s_ranges_keep_their_index_point_decimals():
    facts = F.normal_move(vix_size(), None, None, trigger=4.0, trigger_scaled=True,
                          gauge="", benchmark=None, symbol="VIX")
    assert facts == {
        "typical_daily_range": "1.04 points (6.11%)",
        "typical_measured_over": "median of the last 14 complete sessions",
        "session_range_so_far": "Tue Oct 20 session: 1.62 points (9.30%), 152% of a typical day",
        "large_move_trigger": "4.00% in one hour, scaled to the typical daily range",
    }


def test_a_vix_note_states_its_range_at_quote_precision_and_nq_s_is_unchanged():
    from tests.desk.test_normal_move import size
    service, _, _ = desk(MOVE_AT)
    service.sources.market_state().instruments.update(VIX=vix_quote(at=MOVE_AT))
    service._move_size = lambda now, inst=ID.NQ: vix_size() if inst == VIX else size()
    vix = service._nq_context(MOVE_AT, VIX)["normal_move_size"]
    assert vix["typical_daily_range"] == "1.04 points (6.11%)"
    assert vix["large_move_trigger"] == "4.00% in one hour, scaled to the typical daily range"
    nq = service._nq_context(MOVE_AT)["normal_move_size"]
    assert nq["typical_daily_range"] == "451 points (1.46%)"
    assert nq["large_move_trigger"] == "0.80% in one hour, scaled to the typical daily range"


def cash_rows(day, *, base=17.0, swing=0.5):
    """VIX's hourly buckets for one trading day: Cboe computes it 03:15-16:15 ET
    only, so a full day has 13 hours of quotes and none overnight."""
    opens = datetime.combine(day, datetime.min.time(), tzinfo=NEW_YORK).replace(
        hour=3, minute=15).astimezone(UTC)
    return [bucket(opens + i * timedelta(hours=1), base + (swing if i % 2 else -swing),
                   contract=None) for i in range(13)]


NOW = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)  # Thu 11:00 ET, Cboe computing VIX


def test_a_full_cboe_day_is_a_complete_vix_session():
    # Measured over the futures' 23 hours, 13 hours of VIX would never count as
    # complete, and VIX would never get a typical range or a scaled trigger.
    rows = [r for day in past_days(20) for r in cash_rows(day)]
    size = MS.move_size("VIX", ohlc_of(rows), now=NOW)
    assert size.sessions == 20
    assert size.typical_range == pytest.approx(1.0)
    assert size.typical_range_pct == pytest.approx(1.0 / 16.5 * 100)  # of the last close


def test_a_gappy_cboe_day_is_still_skipped():
    days = past_days(20)
    rows = [r for day in days[:12] for r in cash_rows(day)]
    rows += cash_rows(days[12])[:5]  # feed gap
    assert MS.move_size("VIX", ohlc_of(rows), now=NOW).sessions == 12


def test_vix_gets_no_realised_volatility():
    # The volatility of a volatility index is not what its level means, and
    # its hours are not the futures' the yearly rate assumes.
    rows = [r for day in past_days(20) for r in cash_rows(day)]
    assert MS.move_size("VIX", ohlc_of(rows), now=NOW).realised_vol_pct is None


def test_nq_s_move_size_still_needs_the_whole_futures_session():
    # 13 hours of NQ quotes in a 23-hour session stay incomplete.
    rows = []
    for day in past_days(20):
        session = globex_session(day)
        rows += [r for r in session_rows(day) if r.start >= session.closes_at
                 - timedelta(hours=13)]
    assert MS.move_size("NQ", ohlc_of(rows), now=NOW).sessions == 0


def test_today_s_vix_range_is_final_after_the_cboe_close():
    rows = [r for day in past_days(20) for r in cash_rows(day)]
    today = datetime(2026, 10, 22).date()
    rows += cash_rows(today)
    after_close = datetime(2026, 10, 22, 20, 40, tzinfo=UTC)  # 16:40 ET
    size = MS.move_size("VIX", ohlc_of(rows), now=after_close)
    assert size.today_range == pytest.approx(1.0)
    assert size.today_partial is False and size.today_complete is True
    during = MS.move_size("VIX", ohlc_of(rows), now=NOW)
    assert during.today_partial is True


def test_vix_s_move_trigger_is_scaled_to_its_range_within_its_own_bounds():
    # 0.55 of a typical day, kept between 4% and 10%: VIX's ordinary hour is
    # far bigger than NQ's, so the stock-index 0.6%-1.5% bounds would fire
    # on routine VIX swings.
    assert vix_size().trigger_vol_pct() == 4.0
    assert vix_size(typical_range_pct=12.0).trigger_vol_pct() == 6.6
    assert vix_size(typical_range_pct=30.0).trigger_vol_pct() == 10.0
    assert vix_size(typical_range=None, typical_range_pct=None).trigger_vol_pct() == 5.0
    assert vix_size(typical_range=None, typical_range_pct=None).trigger_vol_pct(7.0) == 7.0
    # The percent trigger the stock indexes use is untouched.
    assert vix_size().trigger_pct(0.8) == 1.5


def test_the_vix_fallback_trigger_is_its_own_setting(monkeypatch):
    # OMARKETS_DESK_MOVE_THRESHOLD_PCT is sized for NQ and never reaches VIX.
    assert Settings(_env_file=None).desk_move_threshold_vol_pct == 5.0
    monkeypatch.setenv("OMARKETS_DESK_MOVE_THRESHOLD_VOL_PCT", "7.5")
    assert Settings(_env_file=None).desk_move_threshold_vol_pct == 7.5
    with pytest.raises(ValueError):
        Settings(_env_file=None, desk_move_threshold_vol_pct=0)


def test_the_vix_setting_is_the_fallback_inside_trigger_vol_pct():
    service, _, _ = desk(MOVE_AT, threshold_vol_pct=7.5)
    service.sources = dataclasses.replace(service.sources, ohlc=object())
    service._move_size = lambda now, inst=ID.NQ: vix_size(typical_range=None,
                                                          typical_range_pct=None)
    assert service._move_trigger(MOVE_AT, VIX) == (7.5, False)
    service._trigger = {}
    service._move_size = lambda now, inst=ID.NQ: vix_size(typical_range_pct=12.0)
    assert service._move_trigger(MOVE_AT, VIX) == (6.6, True)
    # NQ keeps the percent setting; the yields their basis points.
    service._trigger = {}
    service._move_size = lambda now, inst=ID.NQ: vix_size()
    assert service._move_trigger(MOVE_AT) == (1.5, True)


# --------------------------------------------------------------------------- moves


def spiking(start=17.0, step=0.001, now=MOVE_AT):
    """VIX rising ``step`` of its start level a minute over the last 80 minutes."""
    return [SimpleNamespace(timestamp=now - timedelta(minutes=80) + timedelta(minutes=i),
                            value=start * (1 + step * i)) for i in range(80)]


async def test_a_vix_move_fires_when_everything_else_is_flat():
    series = {"NQ": flat(30000.0), "ES": flat(6000.0), "DXY": flat(100.0),
              "US2Y": flat(3.9), "US10Y": flat(4.3), "VIX": spiking()}
    service, _, _ = desk(MOVE_AT, series)
    [note] = await service.tick()
    assert note.id == "move:VIX:20261020T10" and note.instrument == "VIX"
    assert note.title == "VIX up 7.36% in the last hour"
    assert note.tone_for == "VIX"
    [(kind, facts)] = service.writer.calls
    assert kind is NoteKind.MOVE
    assert facts["the_move"]["market"] == LABEL
    assert facts["the_move"]["change"] == "+7.36%"
    others = [m["market"] for m in facts["other_markets_same_window"]]
    assert others.count(LABEL) == 1
    assert "nq_vs_es" not in facts and "megacap_participation" not in facts


async def test_an_ordinary_vix_hour_does_not_fire():
    # About 1.5% in an hour is routine for VIX. NQ's 0.8% trigger must never
    # be applied to it.
    series = {"NQ": flat(30000.0), "VIX": spiking(step=0.0002)}
    service, _, _ = desk(MOVE_AT, series)
    assert await service.tick() == []
    assert service.writer.calls == []
    assert service._move_trigger(MOVE_AT, VIX) == (5.0, False)


async def test_nq_move_fires_when_vix_is_flat():
    series = {"NQ": falling(30000.0), "VIX": flat(17.0)}
    service, _, _ = desk(MOVE_AT, series)
    [note] = await service.tick()
    assert note.id == "move:20261020T10" and note.instrument == "NQ"


async def test_vix_has_its_own_three_hour_move_limit():
    series = {"NQ": flat(30000.0), "VIX": spiking()}
    service, clock, _ = desk(MOVE_AT, series)
    assert [n.id for n in await service.tick()] == ["move:VIX:20261020T10"]

    # An hour on, NQ falls and VIX jumps again: VIX is inside its 3 hours; NQ writes.
    later = MOVE_AT + timedelta(hours=1)
    series.update(NQ=falling(30000.0, later), VIX=spiking(now=later))
    clock["now"] = later
    assert [n.id for n in await service.tick()] == ["move:20261020T11"]

    # Past VIX's 3 hours but inside NQ's: only VIX writes again.
    last = MOVE_AT + timedelta(hours=3, minutes=5)
    series.update(NQ=falling(30000.0, last), VIX=spiking(now=last))
    clock["now"] = last
    [note] = await service.tick()
    assert note.id.startswith("move:VIX:") and note.id != "move:VIX:20261020T10"
    assert service.store.count_since("move", MOVE_AT.isoformat(), instrument="VIX") == 2
    assert service.store.count_since("move", MOVE_AT.isoformat(), instrument="NQ") == 1


async def test_vix_and_the_yield_moves_are_limited_separately():
    yield_fall = [SimpleNamespace(timestamp=MOVE_AT - timedelta(minutes=80) + timedelta(
        minutes=i), value=4.3 - 0.08 * i / 60) for i in range(80)]
    series = {"NQ": flat(30000.0), "US10Y": yield_fall, "VIX": spiking()}
    service, _, _ = desk(MOVE_AT, series)
    ids = sorted(n.id for n in await service.tick())
    assert ids == ["move:US10Y:20261020T10", "move:VIX:20261020T10"]


#: Cboe's close on Tuesday Oct 20, 2026: 16:15 ET.
CBOE_CLOSE = datetime(2026, 10, 20, 20, 15, tzinfo=UTC)
#: Friday Nov 27, 2026 closes early: Cboe stops calculating VIX at 13:15 ET.
EARLY_CLOSE = datetime(2026, 11, 27, 18, 15, tzinfo=UTC)


def vix_until(close, after):
    """NQ flat and VIX up about 9% in its last hour before ``close``, with no VIX
    quote after it (as at Cboe's close); the desk looks ``after`` the close."""
    now = close + after
    return now, {"NQ": flat(30000.0, now), "VIX": spiking(step=0.002, now=close)}


async def vix_notes(service):
    # The Friday of an early close also gets its week in review.
    return [n for n in await service.tick() if n.instrument == "VIX"]


async def test_no_vix_move_note_after_cboe_s_close():
    # DeepSeek's repro: at 16:55 ET a 15:40-16:15 move was titled "VIX up 6.43%
    # in the last hour", 40 minutes after VIX stopped being calculated.
    now, series = vix_until(CBOE_CLOSE, timedelta(minutes=40))
    service, _, _ = desk(now, series)
    assert await service.tick() == []
    assert service.writer.calls == []


async def test_a_vix_move_up_to_cboe_s_close_is_still_written_just_after_it():
    now, series = vix_until(CBOE_CLOSE, timedelta(minutes=5))
    service, _, _ = desk(now, series)
    [note] = await service.tick()
    assert note.instrument == "VIX" and note.title.endswith("in the last hour")


async def test_no_vix_move_note_from_a_stale_quote_inside_cboe_hours():
    # VIX's feed stopped 25 minutes ago: its "last hour" would be long over.
    service, _, _ = desk(MOVE_AT, {"NQ": flat(30000.0),
                                   "VIX": spiking(step=0.002, now=MOVE_AT - timedelta(minutes=25))})
    assert await service.tick() == []


async def test_no_vix_move_note_after_an_early_close():
    now, series = vix_until(EARLY_CLOSE, timedelta(minutes=25))
    service, _, _ = desk(now, series)
    assert await vix_notes(service) == []


async def test_a_vix_move_up_to_an_early_close_is_still_written_just_after_it():
    # Cboe's early close is the futures' too (13:15 ET), so the futures being
    # closed must not stop VIX's own grace period.
    now, series = vix_until(EARLY_CLOSE, timedelta(minutes=5))
    service, _, _ = desk(now, series)
    [note] = await vix_notes(service)
    assert note.title.endswith("in the last hour")


async def test_a_vix_move_is_not_written_when_vix_is_not_enabled():
    series = {"NQ": flat(30000.0), "VIX": spiking()}
    service, _, _ = desk(MOVE_AT, series, instruments=BEFORE)
    assert await service.tick() == []
    assert service.writer.calls == []


async def test_no_vix_premarket_is_scheduled():
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


def test_ask_writes_for_vix_when_enabled():
    service, _, _ = desk(MOVE_AT)
    response = client(service).post("/api/v1/desk/ask", params={"instrument": "VIX"})
    assert response.status_code == 200
    body = response.json()
    assert body["id"].startswith("ask:VIX:") and body["instrument"] == "VIX"
    assert body["tone_for"] == "VIX"
    [(kind, _)] = service.writer.calls
    assert kind is NoteKind.ASK


def test_ask_on_a_vix_focus_writes_vix_not_nq():
    service, _, _ = desk(MOVE_AT)
    response = client(service, focus="VIX").post("/api/v1/desk/ask")
    assert response.status_code == 200
    assert response.json()["id"].startswith("ask:VIX:")
    assert response.json()["instrument"] == "VIX"


def test_ask_for_vix_includes_vix_in_the_last_two_hours_once():
    series = {"VIX": [SimpleNamespace(timestamp=MOVE_AT - timedelta(minutes=180 - i),
                                      value=17.0 + i * 0.01) for i in range(180)]}
    service, _, _ = desk(MOVE_AT, series)
    assert client(service).post("/api/v1/desk/ask",
                                params={"instrument": "VIX"}).status_code == 200
    [(_, facts)] = service.writer.calls
    rows = [m for m in facts["last_two_hours"] if m["market"] == LABEL]
    assert len(rows) == 1 and rows[0]["change"].endswith("%")


def test_ask_refuses_vix_when_the_desk_does_not_write_for_it():
    service, _, _ = desk(MOVE_AT, instruments=BEFORE)
    response = client(service, focus="VIX").post("/api/v1/desk/ask")
    assert response.status_code == 400
    assert response.json()["detail"] == "The desk does not write for VIX"
    assert service.writer.calls == [] and service.store.recent(include_failed=True) == []


# --------------------------------------------------------------------------- snapshots


def vix_facts(writer: SentWriter, kind: NoteKind) -> dict:
    return next(f for k, s, f in writer.sent if k is kind and s == "VIX")


def quotes(service) -> None:
    service.sources.market_state().instruments.update({i.symbol: i for i in (
        inst("NQ", 30120.5, change_pct=-1.12), inst("ES", 6810.25, change_pct=-0.84),
        vix_quote(19.86, change=2.44, change_pct=14.01))})


async def test_v9_vix_snapshots():
    now = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
    writer = SentWriter()
    service, _, _ = desk(now, {"VIX": spiking(now=now)}, instruments=("NQ", "VIX"),
                         writer=writer, rows=cpi_rows())
    quotes(service)
    await service.tick()
    facts = vix_facts(writer, NoteKind.MOVE)
    assert facts["the_move"]["market"] == LABEL
    check("v9_vix_move.txt", text(P.render(NoteKind.MOVE, VIX, facts, version=P.V9)))

    writer = SentWriter()
    service, _, _ = desk(now, instruments=("NQ", "VIX"), writer=writer, rows=cpi_rows())
    quotes(service)
    await service.ask(VIX)
    facts = vix_facts(writer, NoteKind.ASK)
    check("v9_vix_ask.txt", text(P.render(NoteKind.ASK, VIX, facts, version=P.V9)))
