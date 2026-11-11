"""desk:v13: NQ, ES and YM notes are told how chip stocks did, and move notes get
chip stocks and each megacap over the move's own window. Other instruments' text
is desk:v12's, and nothing changes for earlier versions."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from core.config.settings import Settings
from core.desk import facts as F
from core.desk import prompts as P
from core.desk.models import NoteKind
from core.domain.instruments import AssetClass, get_instrument
from core.domain.models import DataStatus
from core.providers.cnbc import EXPECTED_DELAY_SECONDS, SYMBOL_MAP
from core.providers.registry import DEFAULT_ROUTES
from tests.desk.test_complete_inputs import inst, state
from tests.desk.test_service import make

NOW = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)  # Tue 11:00 ET


def chips(**kw):
    return inst("SMH", asset=AssetClass.EQUITY, at=NOW, **kw)


# ------------------------------------------------------------------ the instrument


def test_the_chip_fund_is_a_real_time_nasdaq_stock_from_cnbc():
    smh = get_instrument("SMH")
    assert smh.asset_class is AssetClass.EQUITY and smh.venue == "NASDAQ"
    assert smh.session_calendar == get_instrument("NVDA").session_calendar
    assert SYMBOL_MAP["SMH"] == "SMH" and DEFAULT_ROUTES["SMH"] == "cnbc"
    assert "SMH" not in EXPECTED_DELAY_SECONDS  # served in real time: lag turns it stale
    assert "SMH" not in F.MEGACAPS  # a fund is not counted among the eight megacaps


# ------------------------------------------------------------------ chip_stocks


def test_chip_stocks_live_gives_the_change_and_what_it_is_measured_from():
    out = F.chip_stocks(state(chips(change_pct=3.754)))
    assert out == {"what": F._CHIPS_ARE, "status": "live", "change_today": "+3.75%",
                   "change_from": "the previous session's close",
                   "as_of": "Tue Oct 20, 11:00 ET"}


def test_chip_stocks_closed_and_stale_are_told_apart_and_never_called_live():
    closed = F.chip_stocks(state(chips(change_pct=-1.0, status=DataStatus.STALE, closed=True)))
    stale = F.chip_stocks(state(chips(change_pct=-1.0, status=DataStatus.STALE)))
    assert closed["status"] == "closed" and stale["status"] == "stale"
    assert closed["change_today"] == "-1.00%"  # the last session's change, labelled closed


def test_chip_stocks_without_a_quote_gives_no_number():
    assert F.chip_stocks(state()) == {"what": F._CHIPS_ARE, "status": "unavailable"}
    no_value = F.chip_stocks(state(chips(value=None, change_pct=None, change=None,
                                         status=DataStatus.UNAVAILABLE)))
    assert no_value == {"what": F._CHIPS_ARE, "status": "unavailable"}


def test_participation_adds_chips_only_when_asked_and_only_for_index_futures():
    assert "chip_stocks" not in F.participation(state(), "NQ")
    assert set(F.participation(state(), "YM", chips=True)) == {
        "nq_vs_es", "megacap_participation", "chip_stocks"}
    assert F.participation(state(), "GOLD", chips=True) == {}


# ------------------------------------------------------------------ same window


START = NOW - timedelta(minutes=60)


def quotes(first, last, *, begin=START, end=NOW, step=5):
    """Quotes from ``first`` at ``begin`` to ``last`` at ``end``."""
    count = int((end - begin) / timedelta(minutes=step))
    return [SimpleNamespace(timestamp=begin + timedelta(minutes=step * i),
                            value=first + (last - first) * i / count)
            for i in range(count + 1)]


def history_of(series):
    def history(symbol, *, start=None, end=None, limit=500):
        return [o for o in series.get(symbol, [])
                if (start is None or o.timestamp >= start) and (end is None or o.timestamp <= end)]
    return history


def test_stocks_are_measured_over_the_window_and_missing_ones_are_named():
    out = F.tech_stocks_same_window(
        history_of({"SMH": quotes(200.0, 208.0), "NVDA": quotes(100.0, 99.0)}),
        start=START, end=NOW)
    assert out["available"] is True
    assert out["stocks"] == [
        {"stock": "chip stocks (SMH)", "change": "+4.00%",
         "from": "Tue Oct 20, 10:00 ET", "to": "Tue Oct 20, 11:00 ET"},
        {"stock": "NVDA", "change": "-1.00%",
         "from": "Tue Oct 20, 10:00 ET", "to": "Tue Oct 20, 11:00 ET"}]
    assert out["not_measured"] == [s for s in F.MEGACAPS if s != "NVDA"]


def test_a_window_with_no_stock_quotes_says_so_and_gives_no_number():
    out = F.tech_stocks_same_window(history_of({}), start=START, end=NOW)
    assert out["available"] is False and "stocks" not in out and "04:00" in out["why_not"]
    # Quotes that stopped before the window began (stocks shut) are not a move either.
    shut = quotes(200.0, 208.0, begin=START - timedelta(hours=3),
                  end=START - timedelta(hours=1))
    assert F.tech_stocks_same_window(history_of({"SMH": shut}), start=START,
                                     end=NOW)["available"] is False


def test_a_stock_whose_quotes_stop_inside_the_window_shows_where_they_stopped():
    part = quotes(200.0, 204.0, end=NOW - timedelta(minutes=30))
    row = F.tech_stocks_same_window(history_of({"SMH": part}), start=START, end=NOW)["stocks"][0]
    assert row["to"] == "Tue Oct 20, 10:30 ET" and row["change"] == "+2.00%"


# ------------------------------------------------------------------ the service


def falling(symbol_value, now):
    return [SimpleNamespace(timestamp=now - timedelta(minutes=80) + timedelta(minutes=i),
                            value=symbol_value * (1 - 0.0002 * i)) for i in range(80)]


async def move_facts(version, instruments=("NQ",), symbol="NQ", extra=None):
    now = NOW
    series = {symbol: falling(30000.0, now), "SMH": falling(200.0, now), **(extra or {})}
    service, _, _ = make(now, series=series, instruments=instruments)
    service.prompt_version = version
    await service.tick()
    return next(f for k, f in service.writer.calls if k is NoteKind.MOVE)


async def test_a_v13_nq_move_note_gets_chips_today_and_stocks_over_its_window():
    facts = await move_facts(P.V13)
    assert facts["chip_stocks"]["status"] == "unavailable"  # no live state in this fixture
    window = facts["tech_stocks_same_window"]
    assert window["available"] is True
    assert [row["stock"] for row in window["stocks"]] == ["chip stocks (SMH)"]
    assert window["stocks"][0]["from"] == facts["the_move"]["from"]
    assert window["stocks"][0]["to"] == facts["the_move"]["to"]
    # The stocks are context for the writer only: the note's own moves list is unchanged.
    assert all(m["market"] != F.LABELS["SMH"] for m in facts["other_markets_same_window"])


async def test_v12_notes_and_non_equity_notes_get_neither_fact():
    v12 = await move_facts(P.V12)
    assert "chip_stocks" not in v12 and "tech_stocks_same_window" not in v12
    gold = await move_facts(P.V13, instruments=("GOLD",), symbol="GOLD")
    assert "chip_stocks" not in gold and "tech_stocks_same_window" not in gold


def test_the_writer_drops_both_facts_below_v13():
    facts = {"chip_stocks": {}, "tech_stocks_same_window": {}, "now": "x"}
    assert set(P.facts_at(P.V12, facts)) == {"now"}
    assert set(P.facts_at(P.V13, facts)) == set(facts)


# ------------------------------------------------------------------ the prompt


def test_v13_is_selectable_layered_on_v12_and_not_the_default():
    assert P.PROMPT_VERSION == P.V9 and P.CHIP_VERSIONS == {P.V13}
    assert Settings(_env_file=None, desk_prompt_version=P.V13).desk_prompt_version == P.V13
    for versions in (P.WHY_VERSIONS, P.EVENT_VERSIONS, P.FRESH_VERSIONS, P.HEDGE_VERSIONS,
                     P.RECENT_LESSONS_VERSIONS):
        assert P.V13 in versions
    assert set(P.PROMPTS[P.V13]) == set(P.PROMPTS[P.V12])


@pytest.mark.parametrize("symbol", sorted(P.SPECS))
def test_v13_adds_one_rule_for_participation_instruments_and_nothing_else(symbol):
    v12, v13 = P.PROMPTS[P.V12][symbol], P.PROMPTS[P.V13][symbol]
    assert (v13.tasks, v13.stated_facts, v13.event_task, v13.hedge_likely) == (
        v12.tasks, v12.stated_facts, v12.event_task, v12.hedge_likely)
    if symbol in F.PARTICIPATION_INSTRUMENTS:
        added = P._V13_CHIP_RULE.format(symbol=symbol)
        assert v13.system == v12.system.replace(
            "\n\nReturn a single JSON object", added + "\n\nReturn a single JSON object", 1)
        assert '"chip_stocks"' in added and '"tech_stocks_same_window"' in added
        assert "{" not in added
    else:
        assert v13.system == v12.system


def test_earlier_versions_never_mention_the_new_facts():
    for version in (P.V9, P.V10, P.V11, P.V12):
        for profile in P.PROMPTS[version].values():
            assert "chip_stocks" not in profile.system
            assert "tech_stocks_same_window" not in profile.system
