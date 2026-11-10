import json
from datetime import UTC, datetime

import httpx

from core.providers.economic_census_releases import CensusReleasesProvider
from core.providers.economic_census_retail import CensusRetailProvider

NOW = datetime(2026, 10, 29, 13, 0, tzinfo=UTC)
BLOCK = b"<html><title>Attention Required! | Cloudflare</title>Sorry, you have been blocked</html>"
FRED_DATA = {
    "RSAFS": [("2026-07-01", "700000"), ("2026-08-01", "700000"), ("2026-09-01", "707000")],
    "DGORDER": [
        ("2026-06-01", "300000"),
        ("2026-07-01", "300000"),
        ("2026-08-01", "300300"),
        ("2026-09-01", "305700"),
    ],
    "ADXTNO": [
        ("2026-06-01", "200000"),
        ("2026-07-01", "200000"),
        ("2026-08-01", "200000"),
        ("2026-09-01", "199000"),
    ],
    "HSN1F": [("2026-08-01", "680"), ("2026-09-01", "676")],
}


def fred_body(series_id, data=FRED_DATA):
    rows = [
        {"realtime_start": "2026-10-29", "realtime_end": "2026-10-29", "date": d, "value": v}
        for d, v in reversed(data[series_id])
    ]
    return json.dumps({"observations": rows}).encode()


def census_marts_body(value="0.8"):
    header = [
        "cell_value", "data_type_code", "category_code", "seasonally_adj", "error_data", "time"
    ]
    row = [value, "MPCSM", "44X72", "yes", "no", "2026-09"]
    return json.dumps([header, row]).encode()


class Net:
    """Mock network: Census answers per ``census``, FRED serves FRED_DATA."""

    def __init__(self, census, fred_data=FRED_DATA, fred_ok=True):
        self.census, self.fred_data, self.fred_ok = census, fred_data, fred_ok
        self.hosts = []

    def __call__(self, request):
        self.hosts.append(request.url.host)
        if request.url.host == "api.census.gov":
            return self.census(request)
        if not self.fred_ok:
            return httpx.Response(500)
        series_id = request.url.params["series_id"]
        return httpx.Response(200, content=fred_body(series_id, self.fred_data))


def blocked(request):
    return httpx.Response(403, content=BLOCK)


async def run(provider_cls, net, **kwargs):
    async with httpx.AsyncClient(transport=httpx.MockTransport(net)) as client:
        provider = provider_cls(client, api_key="census-key", clock=lambda: NOW, **kwargs)
        provider.sleep = _no_sleep
        return await provider.fetch()


async def _no_sleep(_):
    return None


async def test_retail_census_403_uses_fred_with_matching_value_and_provenance():
    net = Net(blocked)
    batch = await run(CensusRetailProvider, net, fred_api_key="fred-key")
    assert batch.status == "healthy"
    (event,) = batch.events
    assert (event.event_code, event.period, event.actual, event.unit) == (
        "RETAIL_SALES", "2026-09", 1.0, "percent")
    assert any("FRED" in note for note in event.notes)
    assert any("FRED fallback" in issue for issue in batch.issues)


async def test_retail_census_ok_never_calls_fred():
    net = Net(lambda r: httpx.Response(200, content=census_marts_body()))
    batch = await run(CensusRetailProvider, net, fred_api_key="fred-key")
    assert batch.status == "healthy" and batch.events[0].actual == 0.8
    assert set(net.hosts) == {"api.census.gov"}


async def test_retail_html_200_body_is_treated_as_blocked_and_falls_back():
    net = Net(lambda r: httpx.Response(200, content=BLOCK))
    batch = await run(CensusRetailProvider, net, fred_api_key="fred-key")
    assert batch.events[0].actual == 1.0


async def test_retail_both_fail_degraded_with_clear_detail_and_no_secrets():
    batch = await run(CensusRetailProvider, Net(blocked, fred_ok=False), fred_api_key="fred-key")
    assert batch.status == "degraded" and batch.events == []
    detail = "; ".join(batch.issues)
    assert "upstream blocked (HTTP 403)" in detail
    assert "census-key" not in detail and "fred-key" not in detail and "http" not in detail.replace(
        "(HTTP 403)", "")


async def test_retail_blocked_without_fred_key_is_degraded_with_clear_detail():
    batch = await run(CensusRetailProvider, Net(blocked))
    assert batch.status == "degraded"
    assert "upstream blocked (HTTP 403)" in "; ".join(batch.issues)


async def test_releases_census_403_uses_fred_series_in_census_units():
    batch = await run(CensusReleasesProvider, Net(blocked), fred_api_key="fred-key")
    assert batch.status == "healthy"
    by_code = {e.event_code: e for e in batch.events}
    durable = by_code["DURABLE_GOODS"]
    core, homes = by_code["CORE_DURABLE_GOODS"], by_code["NEW_HOME_SALES"]
    assert (durable.period, durable.actual, durable.previous) == ("2026-09", 1.8, 0.1)
    assert (core.actual, core.previous) == (-0.5, 0.0)
    assert (homes.actual, homes.previous, homes.unit) == (676000.0, 680000.0, "value")
    assert all(any("FRED" in n for n in e.notes) for e in batch.events)


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
    batch = await run(CensusReleasesProvider, net, fred_api_key="fred-key")
    assert batch.status == "healthy" and len(batch.events) == 3
    assert set(net.hosts) == {"api.census.gov"}


async def test_releases_both_fail_degraded_with_clear_detail():
    batch = await run(CensusReleasesProvider, Net(blocked, fred_ok=False), fred_api_key="k")
    assert batch.status == "degraded" and batch.events == []
    assert any("upstream blocked (HTTP 403)" in issue for issue in batch.issues)
    assert not any("request or validation failed" in issue for issue in batch.issues)


def stale(data, *codes):
    return {k: [r for r in v if r[0] < "2026-09-01"] if k in codes else v for k, v in data.items()}


async def run_at(provider_cls, now, data):
    async with httpx.AsyncClient(transport=httpx.MockTransport(Net(blocked, data))) as client:
        return await provider_cls(
            client, api_key="c", fred_api_key="f", clock=lambda: now, sleep=_no_sleep
        ).fetch()


async def test_releases_fred_lag_inside_release_grace_is_pending_not_degraded():
    # Durable goods are due ~Oct 28; FRED still ends in August a few hours later.
    data = stale(FRED_DATA, "DGORDER", "ADXTNO")
    batch = await run_at(CensusReleasesProvider, NOW, data)
    assert batch.status == "healthy"
    assert {e.event_code for e in batch.events} >= {"NEW_HOME_SALES"}


async def test_releases_fred_stale_long_after_release_is_degraded():
    data = stale(FRED_DATA, "DGORDER", "ADXTNO")
    batch = await run_at(CensusReleasesProvider, datetime(2026, 10, 31, 14, tzinfo=UTC), data)
    assert batch.status == "degraded"
    assert any("DURABLE_GOODS" in issue for issue in batch.issues)


async def test_retail_fred_lag_pending_then_degraded():
    data = stale(FRED_DATA, "RSAFS")
    early = await run_at(CensusRetailProvider, datetime(2026, 10, 15, 14, tzinfo=UTC), data)
    assert early.status == "healthy" and early.events == []
    late = await run_at(CensusRetailProvider, NOW, data)
    assert late.status == "degraded"
