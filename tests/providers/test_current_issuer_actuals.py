import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from core.providers.economic_census_retail import CensusRetailProvider
from core.providers.economic_census_retail import parse_series as census
from core.providers.economic_fed_g17 import FedG17Provider
from core.providers.economic_fed_g17 import parse_release as fed

NOW = datetime(2026, 10, 13, 16, tzinfo=UTC)
#: Real Census API response, captured Oct 23, 2026 (key not included).
CENSUS_BODY = (
    Path(__file__).parents[1] / "fixtures/economics/census-marts-retail-2026-10-23.json"
).read_bytes()
CENSUS = json.loads(CENSUS_BODY)[:-1]  # through August 2026, as seen on NOW


def census_body(table) -> bytes:
    return json.dumps(table).encode()


G17 = b"""  FEDERAL RESERVE STATISTICAL RELEASE
G.17 (419) For release at 9:15 a.m. (EDT)
September 18, 2026
Industrial Production and Capacity Utilization
Industrial production (IP) was unchanged in August after increasing 0.2 percent in July.
Industrial Production and Capacity Utilization:  Summary
Seasonally adjusted
  | 2017=100 | Percent change
Industrial production | Mar.[r] Apr.[r] Aug.[p] | Mar.[r] Apr.[r] Aug.[p]
 Total index | 101.7 102.6 102.6 102.8 103.0 103.1 | -.2 .8 .1 .2 .2 .0 | 1.4
 Capacity utilization
"""


def test_official_release_values_period_units_and_publication_provenance():
    retail = census(census_body(CENSUS), NOW)
    industrial = fed(G17, NOW)
    assert retail.actual == 1.3 and industrial.actual == 0.0  # current Census vintage
    assert retail.period == industrial.period == "2026-08"
    assert retail.actual_basis == industrial.actual_basis == "source_release"
    assert retail.released_at is None  # The Census API has no publication time.
    assert industrial.released_at == datetime(2026, 9, 18, 13, 15, tzinfo=UTC)
    assert len(retail.source_revision) == len(industrial.source_revision) == 64


def _edit(row: int, column: int, value):
    table = [list(item) for item in CENSUS]
    table[row][column] = value
    return table


@pytest.mark.parametrize(
    "table",
    [
        CENSUS + [CENSUS[-1]],  # the same month twice
        _edit(-1, 2, "44000"),  # retail excluding food services
        _edit(-1, 3, "no"),  # not seasonally adjusted
        _edit(-1, 1, "E_MPCSM"),  # sampling error, not the estimate
        _edit(-1, -1, "2026-10"),  # the month is not over yet
        _edit(-1, 0, "n/a"),
        CENSUS[:1],
        [CENSUS[0][1:], *CENSUS[1:]],
        {"error": "unknown variable"},
    ],
)
def test_census_rejects_other_series_ambiguous_future_or_malformed_rows(table):
    with pytest.raises((ValueError, KeyError, TypeError)):
        census(census_body(table), NOW)


def test_census_reads_the_latest_month_from_the_real_api_response():
    event = census(CENSUS_BODY, datetime(2026, 10, 23, 2, tzinfo=UTC))
    assert (event.period, event.actual, event.source_event_id) == (
        "2026-09", 0.8, "2026-09:RETAIL_SALES"
    )
    assert event.released_at is None


async def test_census_sends_key_and_reports_missing_key_without_a_request():
    seen = []

    def handler(request):
        seen.append(request.url)
        return httpx.Response(200, content=CENSUS_BODY)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        missing = await CensusRetailProvider(client, clock=lambda: NOW).fetch()
        assert missing.status == "not_configured" and not seen
        batch = await CensusRetailProvider(
            client, api_key="test-key", clock=lambda: datetime(2026, 10, 23, 2, tzinfo=UTC)
        ).fetch()
    assert batch.status == "healthy" and batch.events[0].actual == 0.8
    assert seen[0].host == "api.census.gov"
    assert seen[0].params["key"] == "test-key"
    assert seen[0].params["category_code"] == "44X72"
    assert seen[0].params["time"] == "from 2025-10"


@pytest.mark.parametrize(
    "body",
    [
        G17.replace(b".2 .0 |", b".2 .1 |"),
        G17.replace(b"(EDT)", b"(EST)"),
        G17.replace(b"in August after", b"in July after"),
        G17.replace(b"Seasonally adjusted", b"Not seasonally adjusted"),
    ],
)
def test_g17_rejects_conflicting_table_time_or_period(body):
    with pytest.raises(ValueError):
        fed(body, NOW)


async def test_provider_unavailable_remains_missing_never_invents_actual():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(403))
    ) as client:
        batch = await CensusRetailProvider(client, api_key="k", clock=lambda: NOW).fetch()
    assert batch.status == "unavailable" and batch.events == []


async def test_current_release_provider_fetch():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=G17))
    ) as client:
        batch = await FedG17Provider(client, clock=lambda: NOW).fetch()
    assert batch.status == "healthy" and batch.events[0].actual == 0.0


def test_g17_reads_the_official_text_release_without_a_headline_sentence():
    """Regression (Oct 21, 2026): the official g17.txt has tables only, no
    "Industrial production (IP) rose ..." sentence, so the provider never parsed."""
    from pathlib import Path

    fixture = Path(__file__).parents[1] / "fixtures/economics/g17-2026-10-16-summary.txt"
    body = fixture.read_bytes()
    assert b"Industrial production (IP)" not in body
    event = fed(body, datetime(2026, 10, 21, tzinfo=UTC))
    assert event.period == "2026-09"
    assert event.actual == 0.7
    assert event.released_at == datetime(2026, 10, 16, 13, 15, tzinfo=UTC)
    with pytest.raises(ValueError):  # a missing table is still never guessed around
        fed(body.replace(b"Total index", b"Totals"), datetime(2026, 10, 21, tzinfo=UTC))
