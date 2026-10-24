from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from core.providers.economic_fred_calendar import FREDReleaseCalendarProvider, parse_calendar


def page(release_id: int, date: str, time: str = "7:30 am") -> bytes:
    return (
        "<html><table><tbody>"
        f'<tr class="odd"><td><span>{date}</span></td></tr>'
        f'<tr><td>{time}</td><td><a href="/release?rid={release_id}">Release</a></td></tr>'
        "</tbody></table><p>All times are US Central Time.</p></html>"
    ).encode()


EMPTY_PAGE = "<html><p>All times are US Central Time.</p></html>"


def test_central_time_dst_and_source_identity():
    march = parse_calendar(page(10, "Thursday March 12, 2026"), 10, 2026)
    november = parse_calendar(page(50, "Friday November 06, 2026"), 50, 2026)
    assert march == [datetime(2026, 3, 12, 12, 30, tzinfo=UTC)]
    assert november == [datetime(2026, 11, 6, 13, 30, tzinfo=UTC)]
    assert not parse_calendar(page(50, "Friday November 06, 2026"), 10, 2026)


def test_calendar_without_declared_timezone_fails_closed():
    body = page(10, "Wednesday October 14, 2026").replace(
        b"All times are US Central Time.", b"Times may vary."
    )
    with pytest.raises(ValueError):
        parse_calendar(body, 10, 2026)


@pytest.mark.parametrize(
    "body",
    [
        page(10, "Wednesday October 14, 2026", "unknown"),
        page(10, "Wednesday October 14, 2025"),
        page(10, "Sunday November 01, 2026", "1:30 am"),
        page(10, "Wednesday October 14, 2026") * 2,
    ],
)
def test_ambiguous_or_invalid_calendar_fails_closed(body):
    with pytest.raises(ValueError):
        parse_calendar(body, 10, 2026)


async def test_provider_keeps_schedule_separate_from_observed_release():
    def handler(request):
        query = request.url.params
        if query["y"] != "2026":
            return httpx.Response(200, text=EMPTY_PAGE)
        if query["rid"] == "10":
            return httpx.Response(200, content=page(10, "Wednesday October 14, 2026"))
        return httpx.Response(200, content=page(50, "Friday October 02, 2026"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await FREDReleaseCalendarProvider(
            client, clock=lambda: datetime(2026, 9, 29, tzinfo=UTC)
        ).fetch()
    assert batch.status == "healthy"
    assert [event.event_code for event in batch.events] == ["CPI", "PAYROLLS"]
    assert all(event.status == "scheduled" for event in batch.events)
    assert all(
        event.actual is event.consensus is event.released_at is None
        for event in batch.events
    )
    assert all(event.period is None for event in batch.events)
    assert batch.events[0].scheduled_at == datetime(2026, 10, 14, 12, 30, tzinfo=UTC)
    assert batch.events[0].source_event_id == "10:2026-10-14"


async def test_partial_access_failure_degrades_without_inventing_missing_slots():
    def handler(request):
        if request.url.params["rid"] in {"50", "192"}:
            return httpx.Response(403)
        if request.url.params["y"] == "2026":
            return httpx.Response(200, content=page(10, "Wednesday October 14, 2026"))
        return httpx.Response(200, text=EMPTY_PAGE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await FREDReleaseCalendarProvider(
            client, clock=lambda: datetime(2026, 9, 29, tzinfo=UTC)
        ).fetch()
    assert batch.status == "degraded"
    assert [event.event_code for event in batch.events] == ["CPI"]
    assert len(batch.issues) == 6


async def test_historical_calendar_rows_are_retained_as_schedule_only_evidence():
    def handler(request):
        if request.url.params["y"] != "2026":
            return httpx.Response(200, text=EMPTY_PAGE)
        rid = int(request.url.params["rid"])
        date = "Friday September 11, 2026" if rid == 10 else "Friday October 02, 2026"
        return httpx.Response(200, content=page(rid, date))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await FREDReleaseCalendarProvider(
            client, clock=lambda: datetime(2026, 9, 29, tzinfo=UTC)
        ).fetch()
    assert batch.status == "healthy"
    assert [item.event_code for item in batch.events] == ["CPI", "PAYROLLS", "JOLTS"]
    assert all(item.actual is None and item.consensus is None
               and item.released_at is None for item in batch.events)


async def test_recent_jolts_slot_retains_source_identity_and_due_status():
    def handler(request):
        if request.url.params["rid"] == "192" and request.url.params["y"] == "2026":
            return httpx.Response(
                200, content=page(192, "Tuesday September 29, 2026", "9:00 am")
            )
        return httpx.Response(200, text=EMPTY_PAGE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await FREDReleaseCalendarProvider(
            client, clock=lambda: datetime(2026, 10, 1, tzinfo=UTC)
        ).fetch()
    assert len(batch.events) == 1
    event = batch.events[0]
    assert event.event_code == "JOLTS"
    assert event.scheduled_at == datetime(2026, 9, 29, 14, tzinfo=UTC)
    assert event.status == "due"
    assert event.source_event_id == "192:2026-09-29"
    assert event.actual is None


async def test_source_recovers_after_rejection_with_stable_event_identity():
    rejected = True

    def handler(request):
        if rejected:
            return httpx.Response(403)
        if request.url.params["y"] == "2026":
            rid = int(request.url.params["rid"])
            date = "Wednesday October 14, 2026" if rid == 10 else "Friday October 02, 2026"
            return httpx.Response(200, content=page(rid, date))
        return httpx.Response(200, text=EMPTY_PAGE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = FREDReleaseCalendarProvider(
            client, clock=lambda: datetime(2026, 9, 29, tzinfo=UTC)
        )
        unavailable = await provider.fetch()
        rejected = False
        recovered = await provider.fetch()
        steady = await provider.fetch()
    assert unavailable.status == "degraded"
    assert unavailable.events == []
    assert recovered.status == steady.status == "healthy"
    assert [item.id for item in recovered.events] == [item.id for item in steady.events]


async def test_backfill_retains_35_days_excludes_over_90_without_extra_requests():
    requests = []
    def handler(request):
        requests.append(request.url)
        if request.url.params["y"] != "2026":
            return httpx.Response(200, text=EMPTY_PAGE)
        rid = int(request.url.params["rid"])
        date = "Tuesday September 08, 2026" if rid == 10 else "Wednesday July 01, 2026"
        return httpx.Response(200, content=page(rid, date))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await FREDReleaseCalendarProvider(
            client, clock=lambda: datetime(2026, 10, 13, tzinfo=UTC)).fetch()
    assert len(requests) == 9
    assert [e.event_code for e in result.events] == ['CPI']
    assert result.events[0].status == 'due'
    assert result.events[0].actual is None and result.events[0].released_at is None


async def test_maintenance_page_is_named_and_stops_further_requests():
    # Captured Oct 22, 2026: fred.stlouisfed.org/releases/calendar answered
    # HTTP 200 with this holding page for every release and year.
    body = (
        Path(__file__).parents[1] / "fixtures/economics/fred-maintenance-2026-10-22.html"
    ).read_bytes()
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await FREDReleaseCalendarProvider(
            client, clock=lambda: datetime(2026, 10, 22, tzinfo=UTC)
        ).fetch()
    assert batch.status == "degraded" and batch.events == []
    assert batch.issues == [
        "FRED is down for maintenance (St. Louis Fed holding page); retried next poll.",
        "FRED calendar contains no supported release slots",
    ]
    assert len(calls) == 1
