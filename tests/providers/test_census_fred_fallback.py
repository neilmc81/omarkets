import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from core.providers.economic_census_fallback import RELEASE_GRACE
from core.providers.economic_census_releases import CensusReleasesProvider
from core.providers.economic_census_retail import CensusRetailProvider

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 10, 13, 0, tzinfo=UTC)
BLOCK = b"<html><title>Attention Required! | Cloudflare</title>Sorry, you have been blocked</html>"
#: ALFRED vintages: first-release date -> levels as published that day.
VINTAGES = {
    "RSAFS": [
        ("2026-09-16", {"2026-07": 700000, "2026-08": 700000}),
        ("2026-10-15", {"2026-07": 700000, "2026-08": 700000, "2026-09": 707000}),
    ],
    "DGORDER": [
        ("2026-09-25", {"2026-06": 300000, "2026-07": 300000, "2026-08": 300300}),
        # The advance report (live levels): 345,371 / 339,117 -> +1.8%.
        ("2026-10-27", {"2026-07": 338700, "2026-08": 339117, "2026-09": 345371}),
        # The full M3 report revises it: 345,672 / 339,185 -> +1.9%.
        ("2026-11-03", {"2026-07": 338700, "2026-08": 339185, "2026-09": 345672}),
    ],
    "ADXTNO": [
        ("2026-09-25", {"2026-06": 200000, "2026-07": 200000, "2026-08": 200000}),
        ("2026-10-27", {"2026-07": 200000, "2026-08": 200000, "2026-09": 199000}),
    ],
    "HSN1F": [
        ("2026-09-24", {"2026-07": 690, "2026-08": 680}),
        ("2026-10-27", {"2026-08": 680, "2026-09": 676}),
    ],
}
#: FRED release schedules (fred/release/dates, 2026), including the M3 full-report
#: dates (rid 95, early month) and the retail annual revision (Sep 28).
SCHEDULES = {
    "9": ["2026-08-14", "2026-09-16", "2026-09-28", "2026-10-15", "2026-11-17", "2026-12-16"],
    "95": ["2026-09-02", "2026-09-25", "2026-10-02", "2026-10-27", "2026-11-03", "2026-11-25"],
    "97": ["2026-08-25", "2026-09-24", "2026-10-27", "2026-11-25", "2026-12-23"],
}


def census_marts_body(value="0.8"):
    header = [
        "cell_value", "data_type_code", "category_code", "seasonally_adj", "error_data", "time"
    ]
    row = [value, "MPCSM", "44X72", "yes", "no", "2026-09"]
    return json.dumps([header, row]).encode()


class Net:
    """Mock network. Census answers per ``census``; FRED serves ALFRED vintages
    first released on or before ``posted`` and the release schedules."""

    def __init__(self, census, posted="2026-11-10", fred_ok=True, schedule_ok=True):
        self.census, self.posted = census, posted
        self.fred_ok, self.schedule_ok = fred_ok, schedule_ok
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if request.url.host == "api.census.gov":
            return self.census(request)
        if not self.fred_ok:
            return httpx.Response(500)
        params = request.url.params
        if request.url.path == "/fred/release/dates":
            if not self.schedule_ok:
                return httpx.Response(500)
            dates = [d for d in SCHEDULES[params["release_id"]] if d >= params["realtime_start"]]
            body = {"release_dates": [{"release_id": params["release_id"], "date": d}
                                      for d in dates]}
            return httpx.Response(200, content=json.dumps(body).encode())
        vintages = [v for v in VINTAGES[params["series_id"]] if v[0] <= self.posted]
        if params.get("output_type") == "4":
            first = {}
            for released, levels in vintages:
                for period, value in levels.items():
                    first.setdefault(period, (released, value))
            rows = [(p, *first[p]) for p in sorted(first, reverse=True)]
        else:
            day = params.get("realtime_start")
            released, levels = [v for v in vintages if day is None or v[0] <= day][-1]
            rows = [(p, released, levels[p]) for p in sorted(levels, reverse=True)]
        observations = [
            {"realtime_start": r, "realtime_end": "9999-12-31", "date": f"{p}-01", "value": str(v)}
            for p, r, v in rows
        ]
        return httpx.Response(200, content=json.dumps({"observations": observations}).encode())

    @property
    def hosts(self):
        return {r.url.host for r in self.requests}


def blocked(request):
    return httpx.Response(403, content=BLOCK)


def html_200(request):
    return httpx.Response(200, content=BLOCK)


async def _no_sleep(_):
    return None


async def run(provider_cls, net, now=NOW, api_key="census-key", fred_api_key="fred-key"):
    async with httpx.AsyncClient(transport=httpx.MockTransport(net)) as client:
        provider = provider_cls(
            client, api_key=api_key, fred_api_key=fred_api_key, clock=lambda: now,
            sleep=_no_sleep,
        )
        return await provider.fetch()


def et(*args):
    return datetime(*args, tzinfo=NY).astimezone(UTC)


def leaked(batch, *secrets):
    text = json.dumps([batch.issues, [e.model_dump(mode="json") for e in batch.events]])
    return [s for s in secrets if s in text]


# --- census_retail ---------------------------------------------------------------


async def test_retail_census_403_uses_fred_with_matching_value_and_provenance():
    batch = await run(CensusRetailProvider, Net(blocked))
    assert batch.status == "healthy"
    assert batch.issues == ["Census blocked (HTTP 403); using FRED fallback"]
    (event,) = batch.events
    assert (event.event_code, event.period, event.actual, event.unit) == (
        "RETAIL_SALES", "2026-09", 1.0, "percent")
    assert event.source_url == "https://alfred.stlouisfed.org/series?seid=RSAFS"
    assert "census.gov" not in event.source_url
    assert any("first-release vintage of 2026-10-15" in note for note in event.notes)


async def test_retail_census_ok_never_calls_fred():
    net = Net(lambda r: httpx.Response(200, content=census_marts_body()))
    batch = await run(CensusRetailProvider, net)
    assert batch.status == "healthy" and batch.events[0].actual == 0.8
    assert net.hosts == {"api.census.gov"}


async def test_retail_html_200_body_is_treated_as_blocked_and_falls_back():
    batch = await run(CensusRetailProvider, Net(html_200))
    assert batch.status == "healthy" and batch.events[0].actual == 1.0
    assert batch.issues == ["Census blocked (non-JSON response); using FRED fallback"]


async def test_retail_both_fail_degraded_with_clear_detail_and_no_secrets():
    batch = await run(CensusRetailProvider, Net(blocked, fred_ok=False))
    assert batch.status == "degraded" and batch.events == []
    assert batch.issues == ["Census blocked (HTTP 403); FRED fallback failed"]
    assert not leaked(batch, "census-key", "fred-key", "api.stlouisfed.org", "api.census.gov")


async def test_retail_blocked_without_fred_key_is_degraded_with_clear_detail():
    batch = await run(CensusRetailProvider, Net(blocked), fred_api_key=None)
    assert batch.status == "degraded"
    assert batch.issues == [
        "Census blocked (HTTP 403); FRED fallback not configured (OMARKETS_FRED_API_KEY)"
    ]


async def test_retail_without_census_key_still_falls_back_to_fred():
    net = Net(blocked)
    batch = await run(CensusRetailProvider, net, api_key=None)
    assert batch.status == "healthy" and batch.events[0].actual == 1.0
    assert batch.issues == [
        "Census API key not configured (OMARKETS_CENSUS_API_KEY); using FRED fallback"
    ]
    assert net.hosts == {"api.stlouisfed.org"}
    assert not leaked(batch, "fred-key")


async def test_retail_without_any_key_is_not_configured_and_sends_nothing():
    net = Net(blocked)
    batch = await run(CensusRetailProvider, net, api_key=None, fred_api_key=None)
    assert batch.status == "not_configured" and net.requests == []


# --- census_releases -------------------------------------------------------------


async def test_releases_census_403_uses_fred_series_in_census_units():
    batch = await run(CensusReleasesProvider, Net(blocked))
    assert batch.status == "healthy"
    assert batch.issues == ["Census blocked (HTTP 403); using FRED fallback"]
    by_code = {e.event_code: e for e in batch.events}
    durable = by_code["DURABLE_GOODS"]
    core, homes = by_code["CORE_DURABLE_GOODS"], by_code["NEW_HOME_SALES"]
    assert (durable.period, durable.actual, durable.previous) == ("2026-09", 1.8, 0.1)
    assert (core.actual, core.previous) == (-0.5, 0.0)
    assert (homes.actual, homes.previous, homes.unit) == (676000.0, 680000.0, "value")
    for event in batch.events:
        assert event.source_url.startswith("https://alfred.stlouisfed.org/series?seid=")
        assert any("ALFRED first-release" in n for n in event.notes)


async def test_releases_html_200_keeps_blocked_reason_when_fred_succeeds():
    batch = await run(CensusReleasesProvider, Net(html_200))
    assert batch.status == "healthy" and len(batch.events) == 3
    assert batch.issues == ["Census blocked (non-JSON response); using FRED fallback"]


async def test_releases_census_ok_never_calls_fred():
    def census(request):
        if request.url.path.endswith("advm3"):
            code = request.url.params["category_code"]
            header = ["cell_value", "seasonally_adj", "error_data", "time_slot_id",
                      "data_type_code", "category_code", "time", "us"]
            rows = [[v, "yes", "no", "0", "MPCNO", code, t, "1"]
                    for v, t in (("0.1", "2026-08"), ("1.8", "2026-09"))]
        else:
            header = ["cell_value", "seasonally_adj", "error_data", "time_slot_id",
                      "data_type_code", "category_code", "geo_level_code", "time"]
            rows = [[v, "yes", "no", "0", "TOTAL", "ASOLD", "US", t]
                    for v, t in (("680", "2026-08"), ("676", "2026-09"))]
        return httpx.Response(200, content=json.dumps([header, *rows]).encode())

    net = Net(census)
    batch = await run(CensusReleasesProvider, net)
    assert batch.status == "healthy" and len(batch.events) == 3 and batch.issues == []
    assert net.hosts == {"api.census.gov"}


async def test_releases_both_fail_degraded_with_clear_detail():
    batch = await run(CensusReleasesProvider, Net(blocked, fred_ok=False))
    assert batch.status == "degraded" and batch.events == []
    assert batch.issues == [
        f"{code}: Census blocked (HTTP 403); FRED fallback failed"
        for code in ("DURABLE_GOODS", "CORE_DURABLE_GOODS", "NEW_HOME_SALES")
    ]


async def test_releases_without_census_key_falls_back_and_leaks_no_key():
    net = Net(blocked)
    batch = await run(CensusReleasesProvider, net, api_key=None, fred_api_key="fred-secret")
    assert batch.status == "healthy" and len(batch.events) == 3
    assert batch.issues == [
        "Census API key not configured (OMARKETS_CENSUS_API_KEY); using FRED fallback"
    ]
    assert net.hosts == {"api.stlouisfed.org"}
    assert not leaked(batch, "fred-secret", "api_key")


async def test_releases_blocked_without_fred_key_is_degraded_and_leaks_no_key():
    batch = await run(CensusReleasesProvider, Net(blocked), fred_api_key=None)
    assert batch.status == "degraded" and batch.events == []
    assert all(i.endswith("FRED fallback not configured (OMARKETS_FRED_API_KEY)")
               for i in batch.issues)
    assert not leaked(batch, "census-key")


async def test_releases_without_any_key_is_not_configured_and_sends_nothing():
    net = Net(blocked)
    batch = await run(CensusReleasesProvider, net, api_key=None, fred_api_key=None)
    assert batch.status == "not_configured" and net.requests == []


# --- first-release vintage -------------------------------------------------------


async def test_durable_goods_uses_advance_vintage_not_revised_current_vintage():
    # FRED's current vintage (after the Nov 3 full M3 report) gives 1.9; the advance
    # headline Census published on Oct 27 was 1.8.
    assert round((345672 / 339185 - 1) * 100, 1) == 1.9
    net = Net(blocked)
    batch = await run(CensusReleasesProvider, net)
    durable = next(e for e in batch.events if e.event_code == "DURABLE_GOODS")
    assert durable.actual == 1.8
    vintage_days = {
        r.url.params.get("realtime_start")
        for r in net.requests
        if r.url.params.get("series_id") == "DGORDER" and "output_type" not in r.url.params
    }
    assert vintage_days == {"2026-10-27"}


async def test_vintage_is_cached_per_first_release():
    net = Net(blocked)
    async with httpx.AsyncClient(transport=httpx.MockTransport(net)) as client:
        provider = CensusRetailProvider(
            client, api_key="c", fred_api_key="f", clock=lambda: NOW, sleep=_no_sleep
        )
        await provider.fetch()
        first = len(net.requests)
        batch = await provider.fetch()
    assert batch.events[0].actual == 1.0
    assert len(net.requests) - first == 2  # Census + the initial-release check only


# --- release grace ---------------------------------------------------------------


async def test_overdue_month_stays_degraded_across_month_rollover():
    # September retail sales were due Oct 15 08:30 ET; FRED still ends in August.
    net_args = {"census": blocked, "posted": "2026-10-14"}
    for now in (et(2026, 10, 31, 10), et(2026, 11, 1, 10), et(2026, 11, 16, 10)):
        batch = await run(CensusRetailProvider, Net(**net_args), now=now)
        assert batch.status == "degraded", now
        assert batch.issues == [
            "Census blocked (HTTP 403); using FRED fallback; "
            "FRED has not published 2026-09 (due 2026-10-15 08:30 ET)"
        ]
        assert batch.events[0].period == "2026-08"  # last good month still reported


async def test_new_home_sales_minutes_after_release_is_pending():
    # The September release was Oct 27 10:00 ET (not a fixed day 26).
    net = Net(blocked, posted="2026-10-26")
    batch = await run(CensusReleasesProvider, net, now=et(2026, 10, 27, 10, 5))
    assert batch.status == "healthy"
    assert "NEW_HOME_SALES: 2026-09 release pending (due 2026-10-27 10:00 ET)" in batch.issues


async def test_durables_skip_the_full_m3_date_before_the_advance_release():
    # Oct 2 is the full M3 report for August, not the September advance report:
    # before Oct 27 nothing is due, and minutes after 08:30 ET it is pending.
    early = await run(CensusReleasesProvider, Net(blocked, posted="2026-10-26"),
                      now=et(2026, 10, 10, 12))
    assert early.status == "healthy"
    assert early.issues == ["Census blocked (HTTP 403); using FRED fallback"]
    batch = await run(CensusReleasesProvider, Net(blocked, posted="2026-10-26"),
                      now=et(2026, 10, 27, 8, 35))
    assert batch.status == "healthy"
    assert "DURABLE_GOODS: 2026-09 release pending (due 2026-10-27 08:30 ET)" in batch.issues


async def test_release_grace_boundary():
    due = et(2026, 10, 15, 8, 30)
    for now, status in (
        (due - timedelta(minutes=1), "healthy"),
        (due + timedelta(minutes=5), "healthy"),
        (due + RELEASE_GRACE - timedelta(seconds=1), "healthy"),
        (due + RELEASE_GRACE, "degraded"),
    ):
        batch = await run(CensusRetailProvider, Net(blocked, posted="2026-10-14"), now=now)
        assert batch.status == status, now
        assert batch.events[0].period == "2026-08"


async def test_release_grace_ends_once_fred_posts_the_month():
    batch = await run(CensusRetailProvider, Net(blocked), now=et(2026, 10, 15, 9, 0))
    assert batch.status == "healthy" and batch.events[0].period == "2026-09"
    assert batch.issues == ["Census blocked (HTTP 403); using FRED fallback"]


async def test_lagging_month_with_unavailable_schedule_is_degraded():
    net = Net(blocked, posted="2026-10-14", schedule_ok=False)
    batch = await run(CensusRetailProvider, net, now=et(2026, 10, 10, 12))
    assert batch.status == "degraded"
    assert batch.issues[0].endswith("2026-09 release schedule unavailable")

