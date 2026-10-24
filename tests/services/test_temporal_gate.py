"""Bounded clock/recovery regressions; synthetic evidence and isolated storage."""

from datetime import UTC, datetime, timedelta

import pytest

from core.domain.instruments import INSTRUMENTS
from core.domain.models import DataStatus, MarketObservation
from core.services.calendar_projection import project_calendar
from core.services.economics import EconomicService, calendar_window
from core.services.market_state import MarketStateService
from tests.services.test_economics import T0, event
from tests.services.test_market_state_service import StubProvider


def test_statement_notice_waits_for_receipt_at_fixed_cutoff_and_reprojects_safely():
    schedule = event(
        id="schedule",
        provider="bea_calendar",
        event_code="BEA_RELEASE",
        name="U.S. International Trade, August 2026",
        period="2026-08",
        unit="release",
        received_at=T0 - timedelta(days=1),
    )
    notice = event(
        id="notice",
        provider="bea_releases",
        event_code="TRADE",
        name="International Trade, August 2026",
        period=None,
        scheduled_at=None,
        released_at=T0,
        received_at=T0 + timedelta(seconds=600),
        status="released",
        unit="release",
    )
    inputs = [schedule, notice]
    before = project_calendar(inputs, now=T0 + timedelta(seconds=599))
    assert len(before) == 1
    assert before[0].released_at is None
    assert before[0].status == "scheduled"
    assert "notice" not in before[0].occurrence_aliases
    arrived = project_calendar(inputs, now=T0 + timedelta(seconds=600))
    assert len(arrived) == 1
    assert arrived[0].status == "released"
    assert arrived[0].actual is None
    assert arrived[0].released_at == T0
    immediate = notice.model_copy(update={"received_at": T0})
    regular = project_calendar([schedule, immediate], now=T0 + timedelta(seconds=600))
    for field in ("id", "scheduled_at", "released_at", "actual", "status", "period"):
        assert getattr(arrived[0], field) == getattr(regular[0], field)
    # Replaying an enriched occurrence must flatten and re-filter its source receipts.
    assert project_calendar(arrived, now=T0 + timedelta(seconds=599)) == before
    assert schedule.status == "scheduled" and schedule.actual is None


@pytest.mark.parametrize("provider", ["bea_releases", "ism_newswire", "nar_releases", "bls"])
def test_calendar_list_does_not_expose_future_received_source(provider):
    future = event(provider=provider, received_at=T0 + timedelta(seconds=1))
    service = EconomicService(mode="live", clock=lambda: T0)
    service._events = [future]
    assert service.calendar_events() == []
    assert service.calendar_occurrence(future.id) is None


@pytest.mark.parametrize(
    "now,start,end",
    [
        # UTC midnight still belongs to the preceding New York calendar day.
        (
            datetime(2026, 10, 15, 0, tzinfo=UTC),
            datetime(2026, 10, 14, 4, tzinfo=UTC),
            datetime(2026, 10, 15, 4, tzinfo=UTC),
        ),
        # Local midnight advances the day, with end-exclusive event selection.
        (
            datetime(2026, 10, 15, 4, tzinfo=UTC),
            datetime(2026, 10, 15, 4, tzinfo=UTC),
            datetime(2026, 10, 16, 4, tzinfo=UTC),
        ),
        # DST calendar days contain 23 / 25 hours rather than a fixed 24 hours.
        (
            datetime(2026, 3, 8, 12, tzinfo=UTC),
            datetime(2026, 3, 8, 5, tzinfo=UTC),
            datetime(2026, 3, 9, 4, tzinfo=UTC),
        ),
        (
            datetime(2026, 11, 1, 12, tzinfo=UTC),
            datetime(2026, 11, 1, 4, tzinfo=UTC),
            datetime(2026, 11, 2, 5, tzinfo=UTC),
        ),
    ],
)
def test_calendar_today_uses_new_york_midnight_and_dst(now, start, end):
    assert calendar_window(now, "today") == (start, end)
    service = EconomicService(mode="live", clock=lambda: now)
    service._events = [
        event(id=str(i), scheduled_at=slot, received_at=start - timedelta(days=1))
        for i, slot in enumerate(
            [start - timedelta(microseconds=1), start, end - timedelta(microseconds=1), end]
        )
    ]
    assert [row.id for row in service.calendar_events(start=start, end=end)] == ["1", "2"]


async def test_btc_receipts_do_not_refresh_stalled_delayed_quotes_or_rates(repo):
    clock = [T0]
    service = MarketStateService(provider=StubProvider(), repository=repo, clock=lambda: clock[0])

    def quote(symbol, timestamp, **updates):
        return MarketObservation(
            symbol=symbol,
            timestamp=timestamp,
            received_at=clock[0],
            value=100,
            unit=INSTRUMENTS[symbol].unit,
            source=f"synthetic-{symbol}",
            **updates,
        )

    nq = quote(
        "NQ",
        T0 - timedelta(seconds=600),
        metadata={
            "feed_status": "delayed",
            "expected_delay_seconds": 600,
        },
    )
    rate = quote("US2Y", T0)
    await service._handle_batch([nq, rate, quote("BTC", T0)])
    assert service.current_state().instruments["NQ"].status is DataStatus.LIVE
    for seconds in (61, 120):
        clock[0] = T0 + timedelta(seconds=seconds)
        await service._handle_batch([quote("BTC", clock[0])])
        state = service.current_state()
        assert state.instruments["BTC"].status is DataStatus.LIVE
        assert state.instruments["NQ"].status is DataStatus.STALE
        assert state.instruments["NQ"].feed_status.value == "delayed"
        assert state.instruments["NQ"].received_at == T0
        assert state.instruments["NQ"].excess_age_seconds == seconds
        assert state.instruments["US2Y"].status is DataStatus.STALE
        assert state.instruments["US2Y"].received_at == T0
        assert service.health_detail().last_update_at == clock[0]
    assert service.observation_for("NQ") == nq
    assert service.observation_for("US2Y") == rate


@pytest.mark.parametrize("future_field", ["timestamp", "received_at"])
async def test_market_state_api_hides_future_evidence_until_cutoff(repo, future_field):
    import httpx
    from fastapi import FastAPI

    from core.api.routes import router

    clock = [T0]
    service = MarketStateService(provider=StubProvider(), repository=repo, clock=lambda: clock[0])
    cutoff = T0 + timedelta(seconds=600)
    fields = {"timestamp": T0, "received_at": T0, future_field: cutoff}
    observation = MarketObservation(
        symbol="NQ", value=100, unit=INSTRUMENTS["NQ"].unit, source="synthetic", **fields
    )
    await service._handle_batch([observation])
    app = FastAPI()
    app.include_router(router)
    app.state.service = service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        clock[0] = cutoff - timedelta(microseconds=1)
        response = await client.get("/api/v1/state")
        assert response.status_code == 200
        row = response.json()["instruments"]["NQ"]
        assert row["status"] == "unavailable"
        assert row["value"] is row["change"] is row["timestamp"] is None
        clock[0] = cutoff
        arrived = (await client.get("/api/v1/state")).json()["instruments"]["NQ"]
        assert arrived["value"] == 100
        assert arrived["status"] != "unavailable"
    # Projection qualifies evidence; original durable facts remain untouched.
    assert service.observation_for("NQ") == observation
    assert service.history("NQ")[0] == observation
