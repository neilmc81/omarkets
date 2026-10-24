from datetime import UTC, datetime

import httpx

from core.providers.economic_bls_calendar import BLSCalendarProvider


def calendar(*events: str) -> str:
    return "\r\n".join(("BEGIN:VCALENDAR", "VERSION:2.0", *events, "END:VCALENDAR", ""))


def event(uid: str, summary: str, start: str, *, sequence: int = 0, status: str = "") -> str:
    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"SUMMARY:{summary}",
        f"DTSTART:{start}",
        f"SEQUENCE:{sequence}",
    ]
    if status:
        lines.append(f"STATUS:{status}")
    return "\r\n".join((*lines, "END:VEVENT"))


async def fetch(body: str, *, now: datetime = datetime(2026, 9, 29, tzinfo=UTC)):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body))
    ) as client:
        return await BLSCalendarProvider(client, clock=lambda: now).fetch()


async def test_cpi_payroll_schedule_identity_and_no_invented_release():
    body = calendar(
        event("cpi-aug", "Consumer Price Index for August 2026", "20260911T123000Z", sequence=2),
        event("jobs-sep", "Employment Situation for September 2026", "20261002T123000Z"),
        event("other", "Regional Employment", "20261001T123000Z"),
    )
    batch = await fetch(body)
    assert batch.status == "healthy"
    assert [item.event_code for item in batch.events] == ["CPI", "PAYROLLS"]
    cpi, jobs = batch.events
    assert cpi.period == "2026-08"
    assert cpi.status == "due"
    assert cpi.source_revision == "2"
    assert jobs.status == "scheduled"
    assert jobs.period == "2026-09"
    assert all(item.actual is item.consensus is item.released_at is None for item in batch.events)
    revised = await fetch(body.replace("SEQUENCE:2", "SEQUENCE:3"))
    assert revised.events[0].id == cpi.id
    assert revised.events[0].source_revision == "3"


async def test_dst_local_time_and_cancelled_slot():
    body = calendar(
        "\r\n".join(
            (
                "BEGIN:VEVENT",
                "UID:jobs-nov",
                "SUMMARY:Employment Situation for October 2026",
                "DTSTART;TZID=America/New_York:20261106T083000",
                "STATUS:CANCELLED",
                "END:VEVENT",
            )
        )
    )
    batch = await fetch(body)
    assert batch.status == "healthy"
    assert batch.events[0].scheduled_at == datetime(2026, 11, 6, 13, 30, tzinfo=UTC)
    assert batch.events[0].status == "cancelled"


async def test_ambiguous_timestamp_and_duplicate_identity_degrade_without_fabrication():
    body = calendar(
        event("cpi", "Consumer Price Index", "20261014T083000"),
        event("jobs", "Employment Situation", "20261002T123000Z"),
        event("jobs", "Employment Situation", "20261003T123000Z"),
    )
    batch = await fetch(body)
    assert batch.status == "degraded"
    assert [item.event_code for item in batch.events] == ["PAYROLLS"]
    assert len(batch.issues) == 2
    assert batch.events[0].period is None


async def test_http_rejection_fails_closed():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(403))
    ) as client:
        provider = BLSCalendarProvider(client, clock=lambda: datetime(2026, 9, 29, tzinfo=UTC))
        batch = await provider.fetch()
    assert batch.status == "unavailable"
    assert not batch.events
