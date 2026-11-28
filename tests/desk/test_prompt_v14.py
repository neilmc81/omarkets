"""desk:v14: every note gets Treasury's daily 10-year real yield and breakeven
inflation (``real_yields``). Everything else is desk:v13's, and nothing changes for
earlier versions."""

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from core.config.settings import Settings
from core.desk import facts as F
from core.desk import prompts as P
from core.desk.models import NoteKind
from core.providers.treasury_yields import (
    NOMINAL,
    REAL,
    DailyYields,
    TreasuryYields,
    combine,
    parse,
)
from tests.desk.test_prompt_v13 import NOW, move_facts

# ------------------------------------------------------------------ the provider


def page(rows, curve):
    """A Treasury XML month with ``rows`` of (day, {years: yield})."""
    prefix = "TC" if curve == REAL else "BC"
    entries = "".join(
        "<entry><content type='application/xml'><m:properties>"
        f"<d:NEW_DATE m:type='Edm.DateTime'>{day}T00:00:00</d:NEW_DATE>"
        + "".join(f"<d:{prefix}_{years}YEAR m:type='Edm.Double'>{value}</d:{prefix}_{years}YEAR>"
                  for years, value in values.items())
        + "</m:properties></content></entry>"
        for day, values in rows)
    return ("<?xml version='1.0' encoding='utf-8'?><feed "
            "xmlns:d='http://schemas.microsoft.com/ado/2007/08/dataservices' "
            "xmlns:m='http://schemas.microsoft.com/ado/2007/08/dataservices/metadata' "
            f"xmlns='http://www.w3.org/2005/Atom'>{entries}</feed>")


def test_parse_reads_days_and_yields_and_drops_bad_rows():
    xml = page([("2026-11-24", {5: "2.58", 10: "2.79"}),
                ("2026-11-25", {5: "", 10: "2.73"}),
                ("not-a-date", {10: "2.70"}),
                ("2026-11-26", {10: "nan"}),
                ("2026-11-27", {10: "99"})], REAL)
    assert parse(xml, REAL) == {date(2026, 11, 24): {5: 2.58, 10: 2.79},
                                date(2026, 11, 25): {10: 2.73}}
    assert parse("<not xml", REAL) == {}


def test_combine_keeps_days_with_a_real_yield_and_joins_that_days_nominal():
    rows = combine({date(2026, 11, 25): {10: 2.73}, date(2026, 11, 24): {10: 2.79}},
                   {date(2026, 11, 25): {10: 4.85}, date(2026, 11, 20): {10: 4.92}})
    assert [(r.day, r.real_10y, r.nominal_10y) for r in rows] == [
        (date(2026, 11, 24), 2.79, None), (date(2026, 11, 25), 2.73, 4.85)]


def transport(pages, calls):
    def handle(request):
        calls.append(request.url.params["data"])
        body = pages.get((request.url.params["data"],
                          request.url.params["field_tdr_date_value_month"]))
        if body is None:
            return httpx.Response(503)
        return httpx.Response(200, text=body)
    return httpx.MockTransport(handle)


PAGES = {(REAL, "202611"): page([("2026-11-25", {10: "2.73"})], REAL),
         (REAL, "202610"): page([("2026-10-30", {10: "2.90"})], REAL),
         (NOMINAL, "202611"): page([("2026-11-25", {10: "4.85"})], NOMINAL),
         (NOMINAL, "202610"): page([("2026-10-30", {10: "5.00"})], NOMINAL)}


async def test_rows_fetch_two_months_cache_and_keep_last_good_on_failure():
    clock = {"now": datetime(2026, 11, 27, 12, tzinfo=UTC)}
    calls: list[str] = []
    pages = dict(PAGES)
    client = httpx.AsyncClient(transport=transport(pages, calls))
    feed = TreasuryYields(client=client, clock=lambda: clock["now"])
    rows = await feed.rows()
    assert [(r.day, r.real_10y, r.nominal_10y) for r in rows] == [
        (date(2026, 10, 30), 2.90, 5.00), (date(2026, 11, 25), 2.73, 4.85)]
    assert len(calls) == 4 and feed.last_error is None
    await feed.rows()  # cached inside the ttl
    assert len(calls) == 4
    pages.clear()  # Treasury goes down
    clock["now"] += timedelta(hours=2)
    assert await feed.rows() == rows and feed.last_error
    assert feed.cached == rows
    await feed.rows()  # not retried before retry_after
    assert len(calls) == 8
    await client.aclose()


async def test_a_page_with_no_rows_keeps_the_last_good_rows():
    clock = {"now": datetime(2026, 11, 27, 12, tzinfo=UTC)}
    pages = dict(PAGES)
    client = httpx.AsyncClient(transport=transport(pages, []))
    feed = TreasuryYields(client=client, clock=lambda: clock["now"])
    rows = await feed.rows()
    for key in pages:
        pages[key] = page([], key[0])
    clock["now"] += timedelta(hours=2)
    assert await feed.rows() == rows and "no readable rows" in feed.last_error
    await client.aclose()


# ------------------------------------------------------------------ the fact


def day(d, real, nominal=None):
    return DailyYields(day=d, real_10y=real, nominal_10y=nominal)


WED = datetime(2026, 11, 26, 3, tzinfo=UTC)  # Wed Nov 25, 22:00 ET


def test_real_yields_gives_level_breakeven_and_day_and_week_changes():
    rows = [day(date(2026, 11, 18), 2.88, 5.07), day(date(2026, 11, 24), 2.79, 4.91),
            day(date(2026, 11, 25), 2.73, 4.85)]
    out = F.real_yields(rows, now=WED)
    assert out == {
        "what": F._REAL_YIELDS_ARE, "status": "available", "as_of": "Wed Nov 25 close",
        "ten_year_real_yield": "2.73%",
        "real_yield_change_prior_day": "-6.0bp", "real_yield_change_past_week": "-15.0bp",
        "ten_year_breakeven_inflation": "2.12%",
        "breakeven_change_prior_day": "+0.0bp", "breakeven_change_past_week": "-7.0bp",
    }


def test_missing_history_or_nominal_says_unavailable_not_zero():
    out = F.real_yields([day(date(2026, 11, 25), 2.73)], now=WED)
    assert out["real_yield_change_prior_day"] == "unavailable"
    assert out["real_yield_change_past_week"] == "unavailable"
    assert out["ten_year_breakeven_inflation"] == "unavailable"
    assert out["breakeven_change_prior_day"] == "unavailable"


def test_no_rows_or_an_old_curve_gives_no_number():
    assert F.real_yields([], now=WED) == {"what": F._REAL_YIELDS_ARE, "status": "unavailable"}
    old = F.real_yields([day(date(2026, 11, 13), 2.9, 5.0)], now=WED)
    assert old == {"what": F._REAL_YIELDS_ARE, "status": "stale", "as_of": "Fri Nov 13 close"}


def test_a_future_dated_row_is_never_quoted():
    rows = [day(date(2026, 11, 25), 2.73, 4.85), day(date(2026, 11, 27), 1.0, 9.0)]
    out = F.real_yields(rows, now=WED)  # Wed Nov 25 evening ET
    assert out["as_of"] == "Wed Nov 25 close" and out["ten_year_real_yield"] == "2.73%"
    assert F.real_yields(rows[1:], now=WED)["status"] == "unavailable"


def test_a_gap_in_the_data_gives_unavailable_changes_not_a_longer_one():
    rows = [day(date(2026, 10, 1), 3.28, 5.40), day(date(2026, 11, 25), 2.73, 4.85)]
    out = F.real_yields(rows, now=WED)
    assert out["real_yield_change_prior_day"] == "unavailable"
    assert out["real_yield_change_past_week"] == "unavailable"
    assert out["breakeven_change_past_week"] == "unavailable"


def test_the_week_change_survives_a_holiday_gap():
    # Nov 18 missing: the latest day 7 to 11 days back (Nov 17) is used.
    rows = [day(date(2026, 11, 17), 2.90, 5.10), day(date(2026, 11, 24), 2.79, 4.91),
            day(date(2026, 11, 25), 2.73, 4.85)]
    assert F.real_yields(rows, now=WED)["real_yield_change_past_week"] == "-17.0bp"


async def test_a_stalled_treasury_does_not_delay_notes():
    import asyncio

    class Stalled(StubYields):
        async def rows(self):
            self.fetches += 1
            await asyncio.sleep(3600)

    from tests.desk.test_prompt_v13 import falling, make
    service, _, _ = make(NOW, series={"NQ": falling(30000.0, NOW)}, instruments=("NQ",))
    service.prompt_version = P.V14
    service.real_yields = Stalled(ROWS)
    await asyncio.wait_for(service.tick(), timeout=5)
    facts = next(f for k, f in service.writer.calls if k is NoteKind.MOVE)
    assert facts["real_yields"]["status"] == "available"  # read from the cache
    service._yields_task.cancel()


async def test_the_whole_refresh_has_a_deadline():
    import asyncio

    async def slow(request):
        await asyncio.sleep(5)
        return httpx.Response(200, text="")

    client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    feed = TreasuryYields(client=client, deadline=0.05,
                          clock=lambda: datetime(2026, 11, 27, tzinfo=UTC))
    assert await asyncio.wait_for(feed.rows(), timeout=2) == []
    assert "TimeoutError" in feed.last_error
    await client.aclose()


def test_a_long_weekend_is_not_stale():
    # Thanksgiving: Wed's curve is still the latest on the following Monday morning.
    monday = datetime(2026, 11, 30, 14, tzinfo=UTC)
    assert F.real_yields([day(date(2026, 11, 25), 2.73, 4.85)], now=monday)["status"] \
        == "available"


# ------------------------------------------------------------------ the service


class StubYields:
    def __init__(self, rows):
        self.cached = rows
        self.fetches = 0

    async def rows(self):
        self.fetches += 1
        return self.cached


async def v14_facts(version, rows, instruments=("NQ",), symbol="NQ"):
    stub = StubYields(rows)

    async def run():
        from tests.desk.test_prompt_v13 import falling, make
        series = {symbol: falling(30000.0, NOW), "SMH": falling(200.0, NOW)}
        service, _, _ = make(NOW, series=series, instruments=instruments)
        service.prompt_version = version
        service.real_yields = stub
        await service.tick()
        return next(f for k, f in service.writer.calls if k is NoteKind.MOVE)
    return await run(), stub


ROWS = [day(date(2026, 10, 16), 2.40, 4.50), day(date(2026, 10, 19), 2.45, 4.52)]


async def test_a_v14_move_note_gets_real_yields_and_still_gets_v13_facts():
    facts, stub = await v14_facts(P.V14, ROWS)
    assert facts["real_yields"]["ten_year_real_yield"] == "2.45%"
    assert facts["real_yields"]["breakeven_change_prior_day"] == "-3.0bp"
    assert "tech_stocks_same_window" in facts and stub.fetches == 1


async def test_gold_gets_real_yields_too():
    facts, _ = await v14_facts(P.V14, ROWS, instruments=("GOLD",), symbol="GOLD")
    assert facts["real_yields"]["status"] == "available"


async def test_v13_neither_fetches_nor_gets_real_yields():
    facts, stub = await v14_facts(P.V13, ROWS)
    assert "real_yields" not in facts and stub.fetches == 0
    assert "real_yields" not in await move_facts(P.V13)


async def test_without_a_feed_v14_says_unavailable():
    facts = await move_facts(P.V14)
    assert facts["real_yields"]["status"] == "unavailable"


def test_the_writer_drops_real_yields_below_v14():
    facts = {"real_yields": {}, "now": "x"}
    assert set(P.facts_at(P.V13, facts)) == {"now"}
    assert set(P.facts_at(P.V14, facts)) == set(facts)


# ------------------------------------------------------------------ the prompt


def test_v14_is_selectable_layered_on_v13_and_not_the_default():
    assert P.PROMPT_VERSION == P.V9 and P.REAL_YIELD_VERSIONS >= {P.V14}
    assert P.V13 not in P.REAL_YIELD_VERSIONS
    assert Settings(_env_file=None, desk_prompt_version=P.V14).desk_prompt_version == P.V14
    for versions in (P.WHY_VERSIONS, P.EVENT_VERSIONS, P.FRESH_VERSIONS, P.HEDGE_VERSIONS,
                     P.RECENT_LESSONS_VERSIONS, P.CHIP_VERSIONS):
        assert P.V14 in versions
    assert set(P.PROMPTS[P.V14]) == set(P.PROMPTS[P.V13])


@pytest.mark.parametrize("symbol", sorted(P.SPECS))
def test_v14_adds_only_the_real_yield_rule(symbol):
    v13, v14 = P.PROMPTS[P.V13][symbol], P.PROMPTS[P.V14][symbol]
    assert (v14.tasks, v14.stated_facts, v14.event_task, v14.hedge_likely) == (
        v13.tasks, v13.stated_facts, v13.event_task, v13.hedge_likely)
    assert P._V14_REAL_YIELD_RULE in v14.system
    assert v14.system.replace(P._V14_REAL_YIELD_RULE, "", 1) == v13.system
    assert "{" not in P._V14_REAL_YIELD_RULE
