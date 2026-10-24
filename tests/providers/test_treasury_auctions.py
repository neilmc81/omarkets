"""TreasuryDirect auction adapter: validation at the boundary and polling cadence."""

from datetime import UTC, date, datetime, time, timedelta

import httpx
import pytest

from core.providers.treasury_auctions import TreasuryAuctions, parse, parse_row


def row(**overrides):
    base = {
        "cusip": "91282CRF0", "securityType": "Note", "type": "Note",
        "securityTerm": "9-Year 10-Month", "originalSecurityTerm": "10-Year",
        "reopening": "Yes", "inflationProtected": "No", "floatingRate": "No",
        "cashManagementBill": "No", "competitiveAuctionCloseDate": "2026-10-07T00:00:00",
        "competitiveAuctionCloseTime": "01:00 PM", "offeringAmount": "39000000000",
        "highYield": "5.300", "bidToCoverRatio": "2.77",
        "primaryDealersCompetitiveParAwarded": "984000000",
        "directBiddersCompetitiveParAwarded": "6618200000",
        "indirectBiddersCompetitiveParAwarded": "31063238000",
    }
    return {**base, **overrides}


def test_a_reopened_ten_year_with_results():
    auction = parse_row(row())
    assert (auction.kind, auction.years, auction.reopening) == ("note", 10, True)
    assert auction.label == "10-year note (reopening)"
    assert auction.auction_date == date(2026, 10, 7) and auction.close_time == time(13, 0)
    # 1:00 PM EDT is 17:00 UTC.
    assert auction.at == datetime(2026, 10, 7, 17, 0, tzinfo=UTC)
    assert auction.result.bid_to_cover == 2.77 and auction.result.high_yield == 5.3
    assert auction.result.dealer_share_pct == 2.5
    assert auction.offering_usd == 39e9


def test_tips_bonds_and_winter_time():
    tips = parse_row(row(inflationProtected="Yes", originalSecurityTerm="5-Year",
                         reopening="No"))
    assert tips.label == "5-year TIPS" and tips.series == "tips:5"
    bond = parse_row(row(securityType="Bond", originalSecurityTerm="30-Year",
                         competitiveAuctionCloseDate="2026-11-12T00:00:00"))
    assert bond.kind == "bond" and bond.at == datetime(2026, 11, 12, 18, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "change",
    [
        {"securityType": "Bill"},
        {"floatingRate": "Yes"},
        {"cashManagementBill": "Yes"},
        {"originalSecurityTerm": "3-Year", "securityTerm": "3-Year"},  # not tracked
        {"inflationProtected": "Yes", "originalSecurityTerm": "2-Year"},
        {"cusip": "not-a-cusip"},
        {"competitiveAuctionCloseDate": "soon"},
        {"originalSecurityTerm": "", "securityTerm": "<script>"},
    ],
)
def test_untracked_or_malformed_rows_are_dropped(change):
    assert parse_row(row(**change)) is None


def test_bad_numbers_mean_no_result_and_unannounced_time_is_none():
    for bad in ({"bidToCoverRatio": "nan"}, {"bidToCoverRatio": "0"}, {"highYield": ""},
                {"highYield": "99"}):
        assert parse_row(row(**bad)).result is None
    upcoming = parse_row(row(competitiveAuctionCloseTime="", highYield="", bidToCoverRatio=""))
    assert upcoming.close_time is None and upcoming.result is None
    assert parse_row(row(primaryDealersCompetitiveParAwarded="x")).result.dealer_share_pct is None


def test_parse_dedupes_and_ignores_non_lists():
    assert parse({"not": "a list"}) == [] and parse([None, 3, "x"]) == []
    pending = row(highYield="", bidToCoverRatio="")
    assert len(parse([pending, row()])) == 1 and parse([pending, row()])[0].result is not None


async def test_results_poll_quickly_only_while_an_auction_result_is_awaited():
    calls: list[str] = []
    clock = {"now": datetime(2026, 10, 7, 17, 5, tzinfo=UTC)}
    upcoming = [row(competitiveAuctionCloseTime="01:00 PM", highYield="", bidToCoverRatio="")]

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path.rsplit("/", 1)[-1])
        if request.url.path.endswith("upcoming"):
            return httpx.Response(200, json=upcoming)
        return httpx.Response(200, json=[] if len(calls) < 6 else [row()])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = TreasuryAuctions(client=client, clock=lambda: clock["now"])
    assert [a.label for a in await provider.upcoming()] == ["10-year note (reopening)"]
    assert await provider.results() == []  # 3 requests (Note, Bond, TIPS)
    clock["now"] += timedelta(minutes=1)
    await provider.results()  # cached
    assert calls.count("auctioned") == 3
    clock["now"] += timedelta(minutes=2)  # result awaited: poll again
    assert len(await provider.results()) == 1
    assert calls.count("auctioned") == 6
    clock["now"] += timedelta(minutes=10)  # result in: back to the slow cadence
    await provider.results()
    assert calls.count("auctioned") == 6
    await client.aclose()


async def test_failures_keep_the_last_good_data_and_report_an_error():
    good = {"ok": True}

    def handler(request):
        return httpx.Response(200, json=[row()]) if good["ok"] else httpx.Response(503)

    clock = {"now": datetime(2026, 10, 8, tzinfo=UTC)}
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = TreasuryAuctions(client=client, clock=lambda: clock["now"], ttl=timedelta(0))
    assert len(await provider.results()) == 1
    good["ok"] = False
    clock["now"] += timedelta(hours=7)
    assert len(await provider.results()) == 1
    assert "unavailable" in provider.last_error
    await client.aclose()


async def test_concurrent_readers_share_one_request():
    import asyncio

    calls: list[str] = []

    async def handler(request):
        calls.append(request.url.path)
        await asyncio.sleep(0.01)
        return httpx.Response(200, json=[row()])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = TreasuryAuctions(client=client)
    await asyncio.gather(provider.upcoming(), provider.upcoming())
    assert len(calls) == 1
    await client.aclose()
