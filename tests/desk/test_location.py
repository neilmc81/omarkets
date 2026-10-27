"""NQ price location: key levels handed to the desk writer as ready-made text."""

from __future__ import annotations

from types import SimpleNamespace

from core.analysis.levels import key_levels
from core.desk import facts as F
from core.desk.models import NoteDraft, NoteKind
from core.desk.prompts import SYSTEM, user_message
from core.desk.service import DeskService, DeskSources
from core.desk.store import DeskStore
from core.desk.writer import invented_numbers
from tests.analysis.test_levels import AFTER_OPEN, PREMARKET, at, nq, obs, seed


def location(repo, *, now=PREMARKET, value=29_175.0, **kwargs):
    return F.price_location(key_levels("NQ", nq(value=value, **kwargs), repo.ohlc, now=now),
                            value)


def test_location_states_side_and_distance_for_each_level(memory_repo):
    seed(memory_repo)
    result = location(memory_repo)
    rows = {row["level"]: (row["value"], row["nq_is"]) for row in result["levels"]}
    assert result["nq_price"] == "29,175.00"
    assert rows == {
        "Prior settle": ("29,150.00", "25.00 points above"),
        "Overnight high": ("29,220.00", "45.00 points below"),
        "Overnight low": ("29,080.00", "95.00 points above"),
        "Prior day high": ("29,960.00", "785.00 points below"),
        "Prior day low": ("29,910.00", "735.00 points below"),
        "Prior week high": ("30,212.00", "1,037.00 points below"),
        "Prior week low": ("29,142.00", "33.00 points above"),
    }
    assert [row["level"] for row in result["levels"]][0] == "Prior settle"
    assert result["overnight_so_far"] is True


def test_overnight_is_complete_after_the_cash_open(memory_repo):
    seed(memory_repo)
    assert "overnight_so_far" not in location(memory_repo, now=AFTER_OPEN)


def test_exactly_at_a_level_and_fractional_points(memory_repo):
    seed(memory_repo)
    rows = {r["level"]: r["nq_is"] for r in location(memory_repo, value=29_150.0)["levels"]}
    assert rows["Prior settle"] == "at"
    rows = {r["level"]: r["nq_is"] for r in location(memory_repo, value=29_150.25)["levels"]}
    assert rows["Prior settle"] == "0.25 points above"


def test_unmeasured_levels_are_left_out_never_zero(memory_repo):
    # Only last week's quotes stored, and no settle from the quote source.
    memory_repo.store([obs(at(2026, 10, 13, 15, 0), 30_212.0)])
    result = location(memory_repo, settle=None)
    assert [row["level"] for row in result["levels"]] == ["Prior week high", "Prior week low"]


def test_no_price_or_no_levels_gives_nothing(memory_repo):
    seed(memory_repo)
    assert location(memory_repo, value=None) is None
    assert location(memory_repo, value=float("nan")) is None
    assert F.price_location(SimpleNamespace(symbol="NQ", levels=[]), 29_175.0) is None


def test_quoted_levels_pass_the_writer_number_check(memory_repo):
    seed(memory_repo)
    message = user_message(NoteKind.PREMARKET, {"nq_price_location": location(memory_repo)})
    draft = NoteDraft(
        headline="NQ is below yesterday's high", summary="NQ sits 785 points below 29,960.",
        what_it_means="It is 25 points above the prior settle of 29,150.00.",
        market_reaction="Still below last week's high of 30,212.", rates_read="Yields flat.",
        learn="A settle is the official close.", nq_tone="neutral")
    assert invented_numbers(draft, SYSTEM + message) == []


def service(ohlc, instruments):
    return DeskService(
        sources=DeskSources(market_state=lambda: SimpleNamespace(instruments=instruments),
                            history=lambda *a, **k: [], calendar_events=lambda s, e: [],
                            news_events=lambda: [], ohlc=ohlc),
        store=DeskStore(":memory:"), writer=None)


def test_service_adds_location_only_when_available(memory_repo):
    seed(memory_repo)
    quote = SimpleNamespace(**vars(nq()), timestamp=PREMARKET)
    facts = service(memory_repo.ohlc, {"NQ": quote})._location(PREMARKET)
    assert facts["nq_price_location"]["nq_price"] == "29,175.00"
    assert facts["nq_price_location"]["as_of"] == "Thu Oct 22, 08:00 ET"
    assert service(None, {"NQ": quote})._location(PREMARKET) == {}
    assert service(memory_repo.ohlc, {})._location(PREMARKET) == {}


def test_storage_failure_omits_location():
    def broken(*args, **kwargs):
        raise RuntimeError("storage down")

    quote = SimpleNamespace(**vars(nq()), timestamp=PREMARKET)
    assert service(broken, {"NQ": quote})._location(PREMARKET) == {}


async def test_premarket_note_facts_carry_the_location(memory_repo):
    from tests.desk.test_service import FakeWriter

    seed(memory_repo)
    writer = FakeWriter()
    quote = SimpleNamespace(**vars(nq()), timestamp=PREMARKET, status="live", change=25.0,
                            change_pct=0.09, display_name="NQ", market_closed=False)
    desk = service(memory_repo.ohlc, {"NQ": quote})
    desk.writer = writer
    await desk.build_premarket("premarket:test", PREMARKET)
    [(kind, facts)] = writer.calls
    assert kind is NoteKind.PREMARKET
    assert facts["nq_price_location"]["levels"][0]["nq_is"] == "25.00 points above"
