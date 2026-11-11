from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from core.providers.earnings_nasdaq import NasdaqEarningsCalendar

DAY = date(2026, 11, 11)
ROWS = {"data": {"rows": [{"symbol": "NVDA", "time": "time-after-hours", "epsForecast": "$1.20"}]}}


class Upstream:
    def __init__(self) -> None:
        self.status = 403
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return httpx.Response(self.status, json=ROWS if self.status == 200 else {})


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 11, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.mark.asyncio
async def test_failed_fetch_waits_before_asking_again() -> None:
    upstream, clock = Upstream(), Clock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        calendar = NasdaqEarningsCalendar(client=client, clock=clock)

        # One refusal covers the whole range: the other days are not requested.
        assert await calendar.between(DAY, DAY + timedelta(days=2)) == []
        assert upstream.calls == 1
        assert calendar.last_error is not None

        clock.now += timedelta(minutes=14)
        assert await calendar.day(DAY) == []
        assert upstream.calls == 1

        clock.now += timedelta(minutes=2)
        upstream.status = 200
        rows = await calendar.day(DAY)
        assert [r.symbol for r in rows] == ["NVDA"]
        assert upstream.calls == 2
        assert calendar.last_error is None


@pytest.mark.asyncio
async def test_cached_day_is_served_while_waiting() -> None:
    upstream, clock = Upstream(), Clock()
    upstream.status = 200
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        calendar = NasdaqEarningsCalendar(client=client, clock=clock)
        assert len(await calendar.day(DAY)) == 1

        clock.now += timedelta(hours=7)  # past the ttl
        upstream.status = 403
        assert len(await calendar.day(DAY)) == 1  # stale rows, not an empty list
        assert len(await calendar.day(DAY)) == 1
        assert upstream.calls == 2
