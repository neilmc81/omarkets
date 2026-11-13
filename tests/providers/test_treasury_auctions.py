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


def _after_close_setup(handler, *, retry_pause=0.0, results_deadline=None, retries=None,
                       **kwargs):
    # 30-year bond closes at 1:00 PM ET on 2026-11-12 (18:00 UTC); two minutes later.
    clock = {"now": datetime(2026, 11, 12, 18, 2, tzinfo=UTC)}
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = TreasuryAuctions(client=client, clock=lambda: clock["now"], **kwargs)
    # Set after construction so the same tests also run against code that predates these knobs.
    provider._retry_pause = retry_pause
    if results_deadline is not None:
        provider._results_deadline = results_deadline
    if retries is not None:
        provider._retries = retries
    return provider, client, clock


def _bond_rows(with_result: bool):
    extra = {} if with_result else {"highYield": "", "bidToCoverRatio": ""}
    return [row(securityType="Bond", originalSecurityTerm="30-Year", reopening="No",
                cusip="912810UA4", competitiveAuctionCloseDate="2026-11-12T00:00:00", **extra)]


async def test_a_timeout_after_the_close_is_retried_for_only_the_failed_type():
    seen: list[str] = []
    bond_attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("upcoming"):
            return httpx.Response(200, json=_bond_rows(False))
        kind = request.url.params["type"]
        seen.append(kind)
        if kind == "Bond":
            bond_attempts["n"] += 1
            if bond_attempts["n"] <= 2:
                raise httpx.ReadTimeout("slow", request=request)
            return httpx.Response(200, json=_bond_rows(True))
        return httpx.Response(200, json=[])

    provider, client, _ = _after_close_setup(handler)
    await provider.upcoming()
    results = await provider.results()
    assert [a.label for a in results] == ["30-year bond"]
    assert results[0].result.bid_to_cover == 2.77
    assert seen.count("Bond") == 3 and seen.count("Note") == 1 and seen.count("TIPS") == 1
    assert provider.last_error is None
    await client.aclose()


async def test_a_type_that_keeps_failing_is_retried_on_the_next_read_not_the_slow_poll():
    state = {"bond_ok": False}
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("upcoming"):
            return httpx.Response(200, json=_bond_rows(False))
        kind = request.url.params["type"]
        seen.append(kind)
        if kind == "Bond":
            if not state["bond_ok"]:
                raise httpx.ReadTimeout("slow", request=request)
            return httpx.Response(200, json=_bond_rows(True))
        return httpx.Response(200, json=[row()])

    provider, client, clock = _after_close_setup(handler)
    await provider.upcoming()
    first = await provider.results()
    assert [a.years for a in first] == [10]  # the other types are kept, nothing invented
    assert "unavailable" in provider.last_error
    seen.clear()
    state["bond_ok"] = True
    clock["now"] += timedelta(seconds=20)  # well inside the 2-minute poll
    second = await provider.results()
    assert seen == ["Bond"]
    assert sorted(a.years for a in second) == [10, 30]
    assert provider.last_error is None
    await client.aclose()


async def test_a_non_list_body_is_a_failure_that_keeps_the_last_good_rows():
    state = {"bad": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["bad"]:
            return httpx.Response(200, json={"message": "error"})
        return httpx.Response(200, json=[row()])

    provider, client, clock = _after_close_setup(handler, ttl=timedelta(0))
    assert len(await provider.results()) == 1
    state["bad"] = True
    clock["now"] += timedelta(hours=1)
    assert len(await provider.results()) == 1  # not wiped by an error body
    assert "unavailable" in provider.last_error
    await client.aclose()


async def test_a_failed_type_waits_out_a_cooldown_and_readers_share_one_attempt():
    import asyncio

    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("upcoming"):
            return httpx.Response(200, json=_bond_rows(False))
        calls.append(request.url.params["type"])
        await asyncio.sleep(0.01)
        if request.url.params["type"] == "Bond":
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json=[row()])

    provider, client, clock = _after_close_setup(handler)
    await provider.upcoming()
    await provider.results()  # warm: Note and TIPS stored, Bond failed
    calls.clear()
    clock["now"] += timedelta(seconds=20)  # past the 15s cooldown, inside the 2-min poll
    await asyncio.gather(*(provider.results() for _ in range(5)))
    assert calls.count("Bond") == 3  # one shared attempt (1 + 2 retries), not 15
    calls.clear()
    clock["now"] += timedelta(seconds=5)  # inside the new cooldown
    for _ in range(5):
        await provider.results()
    assert calls == []
    await client.aclose()


async def test_the_cooldown_outside_the_awaiting_window_is_the_normal_poll():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params["type"])
        raise httpx.ReadTimeout("slow", request=request)

    clock = {"now": datetime(2026, 11, 14, 12, 0, tzinfo=UTC)}  # no auction pending
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = TreasuryAuctions(client=client, clock=lambda: clock["now"])
    provider._retry_pause = 0.0
    await provider.results()
    assert sorted(calls) == ["Bond", "Note", "TIPS"]  # single attempt each
    calls.clear()
    for _ in range(10):  # a desk tick every 60s for 1m50s
        clock["now"] += timedelta(seconds=11)
        await provider.results()
    assert calls == []
    clock["now"] += timedelta(minutes=1)
    await provider.results()
    assert len(calls) == 3
    await client.aclose()


async def test_the_schedule_is_served_while_a_results_refresh_is_slow():
    import asyncio

    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("upcoming"):
            return httpx.Response(200, json=_bond_rows(False))
        await release.wait()
        return httpx.Response(200, json=[row()])

    provider, client, _ = _after_close_setup(handler)
    await provider.upcoming()
    refresh = asyncio.create_task(provider.results())
    await asyncio.sleep(0.01)
    assert len(await asyncio.wait_for(provider.upcoming(), 0.5)) == 1
    release.set()
    await refresh
    await client.aclose()


async def test_a_results_refresh_is_bounded_by_an_overall_deadline():
    import asyncio

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["type"] == "Bond":
            await asyncio.sleep(5)
        return httpx.Response(200, json=[row()])

    provider, client, _ = _after_close_setup(handler, results_deadline=0.1)
    results = await asyncio.wait_for(provider.results(), 1.0)
    assert [a.years for a in results] == [10]  # Note/TIPS kept; Bond timed out
    assert "unavailable" in provider.last_error
    await client.aclose()


async def test_the_cooldown_starts_when_a_slow_failing_attempt_ends():
    calls: list[str] = []
    holder: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("upcoming"):
            return httpx.Response(200, json=_bond_rows(False))
        kind = request.url.params["type"]
        calls.append(kind)
        if kind == "Bond":
            holder["clock"]["now"] += timedelta(seconds=30)  # a slow failure
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json=[row()])

    provider, client, clock = _after_close_setup(handler, retries=0)
    holder["clock"] = clock
    await provider.upcoming()
    await provider.results()
    calls.clear()
    clock["now"] += timedelta(seconds=10)  # 10s after the attempt ended: still cooling down
    await provider.results()
    assert "Bond" not in calls
    clock["now"] += timedelta(seconds=10)  # past the 15s cooldown; Note/TIPS are fresh
    await provider.results()
    assert calls == ["Bond"]
    await client.aclose()


async def test_readers_queued_behind_a_slow_failing_refresh_share_its_outcome():
    import asyncio

    calls: list[str] = []
    holder: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("upcoming"):
            return httpx.Response(200, json=_bond_rows(False))
        calls.append(request.url.params["type"])
        await asyncio.sleep(0.01)
        holder["clock"]["now"] += timedelta(seconds=20)  # the attempt takes 20s
        raise httpx.ReadTimeout("slow", request=request)

    provider, client, clock = _after_close_setup(handler, retries=0)
    holder["clock"] = clock
    await provider.upcoming()
    # Cold readers: nothing cached, so each queues behind the running refresh.
    outcomes = await asyncio.gather(*(provider.results() for _ in range(5)))
    assert outcomes == [[]] * 5
    assert sorted(calls) == ["Bond", "Note", "TIPS"]  # one refresh, not five
    assert "unavailable" in provider.last_error
    await client.aclose()


async def test_schedule_and_results_errors_are_tracked_separately():
    state = {"schedule_ok": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("upcoming"):
            if state["schedule_ok"]:
                return httpx.Response(200, json=_bond_rows(False))
            return httpx.Response(503)
        return httpx.Response(200, json=[row()])

    provider, client, clock = _after_close_setup(handler)
    await provider.upcoming()
    await provider.results()  # results succeed...
    assert "schedule" in provider.last_error  # ...but do not hide the schedule failure
    state["schedule_ok"] = True
    clock["now"] += timedelta(hours=7)
    await provider.upcoming()
    assert provider.last_error is None
    await client.aclose()


async def test_the_three_types_are_fetched_concurrently():
    import asyncio

    active = {"now": 0, "peak": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        active["now"] += 1
        active["peak"] = max(active["peak"], active["now"])
        await asyncio.sleep(0.02)
        active["now"] -= 1
        return httpx.Response(200, json=[row()])

    provider, client, _ = _after_close_setup(handler)
    await provider.results()
    assert active["peak"] == 3
    await client.aclose()
