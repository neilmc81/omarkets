from datetime import UTC, datetime

import httpx

from core.providers.economic_consensus import (
    ForexFactoryProvider,
    classify,
    parse_number,
)

NOW = datetime(2026, 9, 17, 15, tzinfo=UTC)


def payload():
    return [
        {
            "title": "CPI m/m",
            "country": "USD",
            "date": "2026-09-18T08:30:00-04:00",
            "impact": "High",
            "forecast": "0.3%",
            "previous": "0.2%",
            "actual": "",
        },
        {
            "title": "Retail Sales m/m",
            "country": "USD",
            "date": "2026-09-16T08:30:00-04:00",
            "impact": "Medium",
            "forecast": "0.8%",
            "previous": "-0.6%",
            "actual": "0.9%",
        },
        {
            "title": "Federal Funds Rate",
            "country": "USD",
            "date": "2026-09-16T14:00:00-04:00",
            "impact": "High",
            "forecast": "4.00%",
            "previous": "3.75%",
            "actual": "",
        },
        {
            "title": "Unemployment Claims",
            "country": "USD",
            "date": "2026-09-17T08:30:00-04:00",
            "impact": "Medium",
            "forecast": "207K",
            "previous": "206K",
            "actual": "224K",
        },
        {
            "title": "Some European Thing",
            "country": "EUR",
            "date": "2026-09-18T08:30:00+02:00",
            "impact": "High",
            "forecast": "1",
            "previous": "1",
            "actual": "",
        },
        {
            "title": "Obscure Unmapped Release",
            "country": "USD",
            "date": "2026-09-18T09:00:00-04:00",
            "impact": "High",
            "forecast": "1",
            "previous": "1",
            "actual": "",
        },
    ]


async def fetch(body, now=NOW):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    ) as client:
        return await ForexFactoryProvider(client, clock=lambda: now).fetch()


async def test_classifies_filters_and_parses_consensus():
    batch = await fetch(payload())
    assert batch.provider == "consensus"
    codes = {event.event_code for event in batch.events}
    assert codes == {"CPI", "RETAIL_SALES", "FOMC", "INITIAL_CLAIMS"}
    cpi = next(event for event in batch.events if event.event_code == "CPI")
    assert cpi.scheduled_at is not None
    assert cpi.scheduled_at > NOW
    assert cpi.status == "scheduled"
    assert cpi.actual is None
    assert cpi.consensus == 0.3
    assert cpi.previous == 0.2
    assert cpi.unit == "percent"
    assert cpi.raw_surprise is None
    retail = next(event for event in batch.events if event.event_code == "RETAIL_SALES")
    assert retail.scheduled_at is not None and retail.scheduled_at < NOW
    assert retail.status == "released"
    assert retail.actual == 0.9
    assert retail.consensus == 0.8
    assert retail.raw_surprise is not None and round(retail.raw_surprise, 6) == 0.1
    assert retail.released_at is not None
    claims = next(event for event in batch.events if event.event_code == "INITIAL_CLAIMS")
    assert claims.actual == 224000.0
    assert claims.consensus == 207000.0
    assert claims.unit == "value"


async def test_past_event_without_actual_is_due_not_released():
    body = [
        {
            "title": "CPI m/m",
            "country": "USD",
            "date": "2026-09-16T08:30:00-04:00",
            "impact": "High",
            "forecast": "0.3%",
            "previous": "0.2%",
            "actual": "",
        }
    ]
    batch = await fetch(body)
    event = batch.events[0]
    assert event.status == "due"
    assert event.actual is None
    assert event.released_at is None


async def test_provider_never_invents_values():
    body = [
        {
            "title": "CPI m/m",
            "country": "USD",
            "date": "2026-09-18T08:30:00-04:00",
            "impact": "High",
            "forecast": "",
            "previous": "",
            "actual": "",
        }
    ]
    batch = await fetch(body)
    event = batch.events[0]
    assert event.consensus is None
    assert event.previous is None
    assert event.actual is None
    assert event.raw_surprise is None


async def test_http_error_and_malformed_payload_are_contained():
    def boom(request):
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as client:
        batch = await ForexFactoryProvider(client, clock=lambda: NOW).fetch()
    assert batch.status == "unavailable"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"bad": "shape"}))
    ) as client:
        batch = await ForexFactoryProvider(client, clock=lambda: NOW).fetch()
    assert batch.status == "unavailable"


def test_classify_and_parse_number_units():
    assert classify("Core CPI m/m")[0] == "CORE_CPI"
    assert classify("Non-Farm Payrolls")[0] == "PAYROLLS"
    assert classify("ADP Non-Farm Employment Change")[0] == "ADP"
    assert classify("Final GDP Price Index q/q") == ("GDP_PRICE_INDEX", "inflation", "high")
    assert classify("Final GDP q/q") == ("GDP", "growth", "high")
    assert classify("ISM Manufacturing PMI")[0] == "ISM_MFG"
    assert classify("ISM Manufacturing Prices")[0] == "ISM_MFG_PRICES"
    assert classify("Random thing") is None
    assert parse_number("207K") == (207000.0, "value")
    assert parse_number("1.40M") == (1400000.0, "value")
    assert parse_number("146.3B") == (146300000000.0, "value")
    assert parse_number("0.3%") == (0.3, "percent")
    assert parse_number("") == (None, None)
    assert parse_number("—") == (None, None)
    assert parse_number("garbage") == (None, None)
