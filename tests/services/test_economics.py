from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from core.domain.economics import (
    EconomicProviderStatus,
    EconomicsMode,
    EventStatus,
    Importance,
    MacroCategory,
    MacroRegistry,
)
from core.domain.models import DataStatus
from core.providers.economic_base import EconomicBatch, EconomicProvider
from core.services.economics import (
    EconomicMailbox,
    EconomicMessage,
    EconomicMessageType,
    EconomicPolling,
    EconomicService,
    calendar_window,
)
from core.storage.economics import SQLiteEconomicRepository

T0 = datetime(2026, 9, 17, 12, tzinfo=UTC)
NY = ZoneInfo("America/New_York")


class FakeClock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


def observation(**overrides):
    from core.domain.economics import MacroObservation

    return MacroObservation.model_validate(
        {
            "id": "CPI_YOY",
            "period": "2026-08",
            "value": 3.2,
            "unit": "percent y/y",
            "source_series_id": "CUSR0000SA0",
            "provider": "bls",
            "received_at": T0,
            **overrides,
        }
    )


def event(**overrides):
    from core.domain.economics import EconomicEvent

    return EconomicEvent.model_validate(
        {
            "id": "cpi-release-2026-09",
            "event_code": "CPI",
            "name": "Consumer Price Index",
            "category": "inflation",
            "importance": "critical",
            "scheduled_at": T0,
            "unit": "percent m/m",
            "frequency": "monthly",
            "status": "scheduled",
            "provider": "bls",
            "source_event_id": "bls-cpi-123",
            "received_at": T0,
            "mode": "live",
            **overrides,
        }
    )


def batch(provider="bls", **overrides):
    return EconomicBatch.model_validate(
        {
            "provider": provider,
            "checked_at": T0,
            "status": "healthy",
            "observations": [observation()],
            "events": [event()],
            **overrides,
        }
    )


def definition():
    from core.domain.economics import MacroDefinition

    return MacroDefinition.model_validate(
        {
            "id": "CPI_YOY",
            "name": "CPI y/y",
            "category": "inflation",
            "unit": "percent y/y",
            "frequency": "monthly",
            "source_series_id": "CUSR0000SA0",
            "provider": "bls",
        }
    )


class StubProvider(EconomicProvider):
    def __init__(self, name: str, result: EconomicBatch | Exception) -> None:
        self._name = name
        self.result = result
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    async def fetch(self) -> EconomicBatch:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def registry():
    return MacroRegistry(definitions=[definition()])


def service(clock=None, **kwargs) -> EconomicService:
    if "repository" not in kwargs:
        kwargs["repository"] = SQLiteEconomicRepository(":memory:", EconomicsMode.LIVE)
        kwargs["repository"].initialize()
    return EconomicService(
        registry=registry(),
        mode=EconomicsMode.LIVE,
        clock=clock or FakeClock(),
        **kwargs,
    )


def test_disabled_mode_defaults() -> None:
    svc = EconomicService(clock=FakeClock())
    state = svc.current_state()
    assert state.mode == EconomicsMode.DISABLED
    assert state.series == []
    assert all(item.status == EconomicProviderStatus.NOT_CONFIGURED for item in state.providers)
    assert svc.provider_health() == []


def test_disabled_mode_rejects_providers() -> None:
    with pytest.raises(ValueError, match="disabled"):
        EconomicService(mode=EconomicsMode.DISABLED, providers=[StubProvider("bls", batch())])


def test_mode_mismatch_rejected(tmp_path) -> None:
    repo = SQLiteEconomicRepository(tmp_path / "econ.sqlite3", EconomicsMode.DEMO)
    with pytest.raises(ValueError, match="mode"):
        EconomicService(repository=repo, mode=EconomicsMode.LIVE)


def test_duplicate_provider_names_rejected() -> None:
    with pytest.raises(ValueError, match="unique"):
        EconomicService(
            providers=[StubProvider("bls", batch()), StubProvider("bls", batch())],
            mode=EconomicsMode.LIVE,
        )


def test_series_state_live_stale_unavailable() -> None:
    clock = FakeClock()
    svc = service(clock=clock)
    svc.accept_batch(batch())
    state = svc.current_state()
    assert state.series[0].status == DataStatus.LIVE
    assert state.series[0].latest.value == 3.2
    assert state.series[0].last_checked_at == T0
    assert "reference-period timeliness" in state.series[0].latest.notes[0]


def test_recent_history_is_bounded_sorted_and_latest_period() -> None:
    clock = FakeClock()
    svc = service(clock=clock)
    rows = [observation(period=f"2025-{month:02d}", value=float(month)) for month in range(1, 13)]
    rows.append(observation(period="2026-01", value=1.0))
    rows.append(observation(period="2026-01", value=1.5, revision=2))
    svc.accept_batch(batch(observations=rows, events=[]))
    recent = svc.current_state().series[0].recent
    assert len(recent) <= 24
    assert [item.period for item in recent] == sorted(item.period for item in recent)
    assert recent[-1].period == "2026-01"
    assert recent[-1].value == 1.5


def test_recent_history_is_empty_without_data() -> None:
    svc = service(clock=FakeClock())
    assert svc.current_state().series[0].recent == []


@pytest.mark.parametrize("latest_value", [None, 0, 3.4])
def test_latest_observations_matches_state_without_calendar_projection(monkeypatch, latest_value):
    svc = service(clock=FakeClock())
    svc._observations = [
        observation(period="2026-07", revision=9),
        observation(period="2026-08", revision=1),
        observation(period="2026-08", revision=2, value=latest_value),
        observation(provider="other", revision=99),
        observation(source_series_id="other", revision=99),
        observation(unit="other", revision=99),
    ]
    expected = [item.latest for item in svc.current_state().series if item.latest is not None]

    def unexpected_calendar(**kwargs):
        raise AssertionError("release evidence must not project the calendar")

    monkeypatch.setattr(svc, "calendar_events", unexpected_calendar)
    latest = svc.latest_observations()
    assert latest == expected
    assert latest[0].period == "2026-08"
    assert latest[0].revision == 2
    assert latest[0].value == latest_value
    assert svc._observations[2].notes == []


def test_latest_observations_empty_without_registered_data():
    svc = service(clock=FakeClock())
    assert svc.latest_observations() == []
    svc._observations = [observation(provider="other")]
    assert svc.latest_observations() == []


def test_release_proximity_tightens_and_relaxes_polling() -> None:
    clock = FakeClock()
    svc = service(
        clock=clock,
        polling=EconomicPolling(
            default_seconds=21600,
            proximity_seconds=300,
            proximity_before_seconds=1800,
            proximity_after_seconds=1800,
        ),
    )
    event_provider = "bls"
    clock.now = T0 - timedelta(hours=1)
    svc.accept_batch(batch(events=[event(provider=event_provider, scheduled_at=T0)]))
    interval = svc.polling.interval(event_provider)
    assert interval == 86400
    assert svc._next_interval(event_provider, interval) == 86400
    clock.now = T0 - timedelta(minutes=10)
    svc.tick()
    assert svc._next_interval(event_provider, interval) == 900
    clock.now = T0 + timedelta(minutes=10)
    assert svc._next_interval(event_provider, interval) == 900
    clock.now = T0 + timedelta(hours=2)
    assert svc._next_interval(event_provider, interval) == 86400


def test_release_proximity_does_not_shorten_shorter_interval() -> None:
    clock = FakeClock()
    svc = service(clock=clock, polling=EconomicPolling(proximity_seconds=300))
    clock.now = T0 - timedelta(minutes=5)
    svc.accept_batch(batch(events=[event(scheduled_at=T0)]))
    assert svc._next_interval("demo", 30) == 30


def test_official_claims_polls_around_consensus_slot():
    clock = FakeClock()
    svc = service(clock=clock)
    svc.accept_batch(batch(provider="consensus", observations=[], events=[event(
        provider="consensus", event_code="INITIAL_CLAIMS", scheduled_at=T0
    )]))
    assert svc._next_interval("dol_claims", 900) == 60
    clock.now = T0 + timedelta(hours=2)
    assert svc._next_interval("dol_claims", 900) == 900


@pytest.mark.asyncio
async def test_sleep_reconsiders_release_proximity(monkeypatch):
    clock = FakeClock()
    clock.now = T0 - timedelta(hours=1)
    svc = service(clock=clock)
    svc.accept_batch(batch(provider="consensus", observations=[],
                           checked_at=clock.now, events=[event(
        provider="consensus", event_code="INITIAL_CLAIMS", scheduled_at=T0
    )]))
    sleeps = []

    async def advance(delay):
        sleeps.append(delay)
        clock.advance(seconds=delay)

    monkeypatch.setattr("core.services.economics.asyncio.sleep", advance)
    await svc._wait_until_due("consensus", 86400)
    assert sleeps
    assert max(sleeps) <= svc.polling.transition_seconds
    assert clock.now <= T0 - timedelta(minutes=29)


def test_series_without_data_is_unavailable() -> None:
    empty = MacroRegistry(definitions=[definition()])
    svc = EconomicService(registry=empty, mode=EconomicsMode.LIVE, clock=FakeClock())
    state = svc.current_state()
    assert state.series[0].status == DataStatus.UNAVAILABLE
    assert state.series[0].latest is None


def test_provider_health_stale_check_degrades() -> None:
    clock = FakeClock()
    svc = service(clock=clock)
    svc.accept_batch(batch())
    clock.advance(hours=72)
    health = {item.name: item for item in svc.provider_health()}
    assert health["bls"].status == EconomicProviderStatus.DEGRADED


def test_provider_failure_keeps_cached_data() -> None:
    clock = FakeClock()
    failing = StubProvider("fred", RuntimeError("network down"))
    svc = service(clock=clock, providers=[failing])
    asyncio.run(svc.check_provider(failing))
    health = {item.name: item for item in svc.provider_health()}
    assert health["fred"].status == EconomicProviderStatus.UNAVAILABLE
    assert "retained" in health["fred"].detail


def test_event_due_transition_derived_not_persisted() -> None:
    clock = FakeClock()
    svc = service(clock=clock)
    clock.now = T0 - timedelta(seconds=1)
    svc.accept_batch(batch())
    assert svc.event_state(event()).status == EventStatus.SCHEDULED
    clock.advance(hours=1)
    state = svc.current_state()
    assert state.upcoming_events == []
    due = svc.event_state(event())
    assert due.status == EventStatus.DUE
    assert due.revision == 1
    stored = svc.repository.events("cpi-release-2026-09", all_versions=True)
    assert stored[0].status == EventStatus.SCHEDULED


def test_calendar_filters_and_half_open_window() -> None:
    clock = FakeClock()
    svc = service(clock=clock)
    svc.accept_batch(batch())
    assert svc.calendar_events(start=T0, end=T0) == []
    window = svc.calendar_events(start=T0, end=T0 + timedelta(seconds=1))
    assert {item.id for item in window} == {"cpi-release-2026-09", "observed:CPI_YOY:2026-08"}
    assert (
        svc.calendar_events(start=T0, end=T0 + timedelta(days=1), importance=Importance.LOW) == []
    )
    assert (
        svc.calendar_events(start=T0, end=T0 + timedelta(days=1), category=MacroCategory.HOUSING)
        == []
    )
    released = svc.calendar_events(
        start=T0, end=T0 + timedelta(days=1), status=EventStatus.RELEASED
    )
    assert {item.id for item in released} == {"observed:CPI_YOY:2026-08"}
    assert (
        len(svc.calendar_events(start=T0, end=T0 + timedelta(days=1), status=EventStatus.DUE)) == 1
    )
    assert svc.calendar_events(start=T0, end=T0 + timedelta(days=1), limit=0 if False else 1)


def test_calendar_window_boundaries() -> None:
    local_noon = datetime(2026, 1, 2, 12, tzinfo=NY)
    start, end = calendar_window(local_noon, "today")
    assert start == datetime(2026, 1, 2, 5, tzinfo=UTC)
    assert end == datetime(2026, 1, 3, 5, tzinfo=UTC)
    week_start, week_end = calendar_window(local_noon, "week")
    assert week_start.weekday() == 0
    assert (week_end - week_start) == timedelta(days=7)
    dst_start, dst_end = calendar_window(datetime(2026, 7, 15, 12, tzinfo=NY), "today")
    assert dst_start == datetime(2026, 7, 15, 4, tzinfo=UTC)


async def test_upcoming_publishes_once_not_every_tick() -> None:
    clock = FakeClock()
    clock.now = T0 - timedelta(seconds=30)
    svc = service(clock=clock)
    svc.accept_batch(batch())
    mailbox = svc.subscribe()
    svc.tick()
    assert not mailbox.pending
    clock.advance(seconds=30)
    svc.tick()
    first = await asyncio.wait_for(mailbox.get(), 1)
    assert first.type == EconomicMessageType.DUE
    svc.tick()
    assert not mailbox.pending
    assert len(svc.repository.events(all_versions=True)) == 1
    svc.unsubscribe(mailbox)


def test_provider_status_change_message() -> None:
    clock = FakeClock()
    failing = StubProvider("fred", RuntimeError("down"))
    svc = service(clock=clock, providers=[failing])
    mailbox = svc.subscribe()
    asyncio.run(svc.check_provider(failing))
    message = asyncio.run(mailbox.get())
    assert message.type == EconomicMessageType.PROVIDER
    assert message.data.ids == ["bls", "fred"]
    svc.unsubscribe(mailbox)


def test_mailbox_coalesces_and_overflows() -> None:
    mailbox = EconomicMailbox(max_ids=3)
    mailbox.offer(
        EconomicMessage(
            type=EconomicMessageType.SERIES, data={"ids": ["a", "b"], "occurred_at": T0}
        )
    )
    mailbox.offer(
        EconomicMessage(
            type=EconomicMessageType.SERIES, data={"ids": ["b", "c"], "occurred_at": T0}
        )
    )
    first = asyncio.run(mailbox.get())
    assert first.data.ids == ["a", "b", "c"]
    mailbox.offer(
        EconomicMessage(
            type=EconomicMessageType.SERIES, data={"ids": ["d", "e", "f", "g"], "occurred_at": T0}
        )
    )
    second = asyncio.run(mailbox.get())
    assert second.data.ids == ["*"]
    assert first.data.occurred_at <= second.data.occurred_at


def test_mailbox_keeps_types_separate() -> None:
    mailbox = EconomicMailbox()
    mailbox.offer(
        EconomicMessage(type=EconomicMessageType.SERIES, data={"ids": ["a"], "occurred_at": T0})
    )
    mailbox.offer(
        EconomicMessage(type=EconomicMessageType.DUE, data={"ids": ["e1"], "occurred_at": T0})
    )
    types = {asyncio.run(mailbox.get()).type, asyncio.run(mailbox.get()).type}
    assert types == {EconomicMessageType.SERIES, EconomicMessageType.DUE}


def test_released_message_on_actual_change() -> None:
    clock = FakeClock()
    svc = service(clock=clock)
    svc.accept_batch(batch())
    mailbox = svc.subscribe()
    svc.accept_batch(batch(events=[event(status="released", actual=0.3)]))
    assert EconomicMessageType.RELEASED in mailbox.pending
    message = mailbox.pending[EconomicMessageType.RELEASED]
    assert message.type == EconomicMessageType.RELEASED
    assert message.data.ids == ["cpi-release-2026-09"]
    svc.unsubscribe(mailbox)


def test_revised_message_on_second_version() -> None:
    clock = FakeClock()
    svc = service(clock=clock, repository=SQLiteEconomicRepository(":memory:", EconomicsMode.LIVE))
    svc.repository.initialize()
    svc.accept_batch(batch())
    mailbox = svc.subscribe()
    svc.accept_batch(batch(events=[event(actual=0.4)]))
    message = asyncio.run(mailbox.get())
    assert message.type == EconomicMessageType.REVISED
    assert message.data.ids == ["cpi-release-2026-09"]
    svc.unsubscribe(mailbox)


def test_polling_intervals_and_validation() -> None:
    polling = EconomicPolling()
    assert polling.interval("bls") == 86400
    assert polling.interval("nyfed") == 3600
    assert polling.interval("bea_calendar") == 21600
    assert polling.interval("fed_calendar") == 21600
    assert polling.interval("fed_releases") == 1800
    assert polling.interval("fred") == 21600
    assert polling.interval("unknown") == 21600
    assert EconomicPolling(overrides={"bls": 90}).interval("bls") == 90
    with pytest.raises(ValueError):
        EconomicPolling(overrides={"bls": 10}).interval("bls")
    with pytest.raises(ValueError):
        EconomicPolling(overrides={"bls": float("inf")}).interval("bls")


def test_service_validates_polling_names() -> None:
    with pytest.raises(ValueError):
        EconomicService(mode=EconomicsMode.LIVE, polling=EconomicPolling(overrides={"bogus": 10}))


async def test_start_stop_lifecycle_and_resumption(tmp_path) -> None:
    clock = FakeClock()
    provider = StubProvider("bls", batch())
    repo = SQLiteEconomicRepository(tmp_path / "econ.sqlite3", EconomicsMode.LIVE)
    svc = service(
        clock=clock,
        providers=[provider],
        repository=repo,
        polling=EconomicPolling(default_seconds=30),
    )
    await svc.start()
    await asyncio.sleep(0)
    assert svc.current_state().series[0].status == DataStatus.LIVE
    await svc.stop()
    assert provider.calls == 1

    clock.advance(hours=1)
    reopened = SQLiteEconomicRepository(tmp_path / "econ.sqlite3", EconomicsMode.LIVE)
    svc2 = service(
        clock=clock,
        providers=[provider],
        repository=reopened,
        polling=EconomicPolling(default_seconds=30),
    )
    await svc2.start()
    await asyncio.sleep(0)
    assert svc2.current_state().series[0].status == DataStatus.LIVE
    assert provider.calls == 1
    await svc2.stop()


def test_start_failure_closes_resources(tmp_path) -> None:
    class Boom(EconomicProvider):
        @property
        def name(self) -> str:
            return "bls"

        async def fetch(self) -> EconomicBatch:
            raise RuntimeError("boom")

    svc = EconomicService(
        providers=[Boom()], mode=EconomicsMode.LIVE, clock=FakeClock(), registry=registry()
    )
    with pytest.raises(ValueError, match="repository"):
        asyncio.run(svc.start())
    assert svc.subscriber_count == 0


@pytest.mark.parametrize("latest_value, expected_status", [(None, "unavailable"), (0, "live")])
async def test_latest_missing_or_zero_survives_older_period_revision(latest_value, expected_status):
    svc = service()
    try:
        svc.accept_batch(batch(observations=[observation(period="2026-09", value=latest_value)]))
        assert svc.current_state().series[0].status == expected_status
        svc.accept_batch(batch(observations=[observation(period="2026-08", value=9)]))
        svc.accept_batch(batch(observations=[observation(period="2026-08", value=10)]))
        state = svc.current_state().series[0]
        assert state.latest.period == "2026-09"
        assert state.latest.value == latest_value
        assert state.status == ("unavailable" if latest_value is None else "stale")
    finally:
        await svc.stop()


@pytest.mark.parametrize(
    "day,hours",
    [(datetime(2026, 3, 8, 16, tzinfo=UTC), 23), (datetime(2026, 11, 1, 16, tzinfo=UTC), 25)],
)
def test_calendar_day_spans_dst_transition(day, hours):
    start, end = calendar_window(day, "today")
    assert end - start == timedelta(hours=hours)
    assert start.astimezone(NY).hour == end.astimezone(NY).hour == 0


async def test_per_series_partial_success_failure_recovery_restart(tmp_path):
    clock = FakeClock()
    repo = SQLiteEconomicRepository(tmp_path / "partial.sqlite3", "live")
    definitions = MacroRegistry(
        definitions=(
            definition(),
            definition().model_copy(update={"id": "OTHER"}),
        )
    )
    svc = EconomicService(repository=repo, registry=definitions, mode="live", clock=clock)
    await svc.start()
    try:
        svc.accept_batch(batch(observations=[observation(), observation(id="OTHER", value=0)]))
        clock.advance(seconds=60)
        svc.accept_batch(
            batch(
                checked_at=clock(),
                status="degraded",
                checked_series_ids=["CPI_YOY"],
                observations=[observation()],
                events=[],
            )
        )
        states = svc.current_state().series
        assert [item.status for item in states] == ["live", "stale"]
        assert [item.last_checked_at for item in states] == [clock(), T0]
        assert states[1].latest.value == 0
        checks = {item.id: item for item in repo.series_checks()}
        assert checks["OTHER"].attempted_at == clock()
        assert checks["OTHER"].last_success_at == T0
        await svc.stop()
        await svc.start()
        assert [item.status for item in svc.current_state().series] == ["live", "stale"]
        clock.advance(seconds=60)
        await svc.check_provider(StubProvider("bls", RuntimeError("failed")))
        assert [item.status for item in svc.current_state().series] == ["stale", "stale"]
        assert [item.last_checked_at for item in svc.current_state().series] == [
            T0 + timedelta(seconds=60),
            T0,
        ]
        clock.advance(seconds=60)
        svc.accept_batch(
            batch(
                checked_at=clock(),
                status="degraded",
                checked_series_ids=["OTHER"],
                observations=[],
                events=[],
            )
        )
        assert [item.status for item in svc.current_state().series] == ["stale", "live"]
        assert svc.current_state().series[1].last_checked_at == clock()
        assert svc.current_state().series[1].latest.value == 0
        clock.advance(days=3)
        assert [item.status for item in svc.current_state().series] == ["stale", "stale"]
    finally:
        await svc.stop()


@pytest.mark.parametrize("aggregate", ["healthy", "degraded"])
async def test_legacy_batch_infers_only_observed_series(aggregate):
    svc = service()
    try:
        svc.accept_batch(batch(status=aggregate, observations=[observation(value=0)]))
        assert svc.current_state().series[0].status == "live"
        svc.clock.advance(seconds=30)
        svc.accept_batch(batch(status=aggregate, checked_at=svc.clock(), observations=[]))
        current = svc.current_state().series[0]
        assert current.status == "stale"
        assert current.last_checked_at == T0
        svc.accept_batch(
            batch(status=aggregate, checked_at=svc.clock(), observations=[observation(value=None)])
        )
        assert svc.current_state().series[0].status == "unavailable"
    finally:
        await svc.stop()


async def test_service_poll_uses_configured_bls_interval(monkeypatch):
    clock = FakeClock()
    provider = StubProvider("bls", batch())
    svc = service(clock=clock, polling=EconomicPolling(overrides={"bls": 90}))
    delays = []

    async def sleep(seconds):
        delays.append(seconds)
        clock.advance(seconds=seconds)
        if len(delays) == 6:
            raise asyncio.CancelledError

    monkeypatch.setattr("core.services.economics.asyncio.sleep", sleep)
    try:
        with pytest.raises(asyncio.CancelledError):
            await svc._poll(provider)
        assert provider.calls == 2
        assert delays == [30] * 6
    finally:
        await svc.stop()


async def test_fred_truncated_refresh_marks_cached_newer_period_stale(tmp_path):
    import httpx

    from core.domain.macro_registry import MACRO_DEFINITIONS
    from core.providers.economic_fred import FREDEconomicProvider

    clock = FakeClock()
    rows = [
        {
            "date": "2026-08-01",
            "value": "121",
            "realtime_start": "2026-09-01",
            "realtime_end": "2026-09-17",
        },
        {
            "date": "2026-07-01",
            "value": "110",
            "realtime_start": "2026-09-01",
            "realtime_end": "2026-09-17",
        },
        {
            "date": "2026-06-01",
            "value": "100",
            "realtime_start": "2026-09-01",
            "realtime_end": "2026-09-17",
        },
    ]
    definition = next(item for item in MACRO_DEFINITIONS if item.id == "RETAIL_SALES")
    calls = []

    def handler(request):
        calls.append(request.url.params["series_id"])
        return httpx.Response(
            200,
            json={
                "observations": rows
                if request.url.params["series_id"] == definition.source_series_id
                else []
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = FREDEconomicProvider(client, clock=clock, api_key="fixture")
        repo = SQLiteEconomicRepository(tmp_path / "fred.sqlite3", "live")
        svc = EconomicService(
            repository=repo,
            registry=MacroRegistry(definitions=(definition,)),
            mode="live",
            clock=clock,
        )
        await svc.start()
        try:
            await svc.check_provider(provider)
            assert svc.current_state().series[0].status == "live"
            assert svc.current_state().series[0].latest.period == "2026-08"
            full = rows.pop(0)
            clock.advance(seconds=60)
            await svc.check_provider(provider)
            assert calls.count(definition.source_series_id) == 2
            state = svc.current_state().series[0]
            assert state.status == "stale"
            assert state.latest.period == "2026-08" and state.latest.value == 10
            assert repo.series_checks()[0].latest_verified_period == "2026-07"
            await svc.stop()
            await svc.start()
            assert svc.current_state().series[0].status == "stale"
            rows.insert(0, full)
            clock.advance(seconds=60)
            await svc.check_provider(provider)
            assert svc.current_state().series[0].status == "live"
            assert repo.series_checks()[0].latest_verified_period == "2026-08"
        finally:
            await svc.stop()


def test_publish_only_ids_listed() -> None:
    clock = FakeClock()
    svc = service(clock=clock)
    mailbox = svc.subscribe()
    svc._publish(EconomicMessageType.SERIES, [])
    assert not mailbox.ready.is_set()
    svc.unsubscribe(mailbox)


def test_observed_release_publishes_released_then_revised() -> None:
    clock = FakeClock()
    svc = service(clock=clock)
    mailbox = svc.subscribe()
    svc.accept_batch(batch(observations=[observation()], events=[]))
    assert EconomicMessageType.RELEASED in mailbox.pending
    assert mailbox.pending[EconomicMessageType.RELEASED].data.ids == ["observed:CPI_YOY:2026-08"]
    mailbox.pending.clear()
    clock.advance(seconds=60)
    svc.accept_batch(
        batch(checked_at=clock(), observations=[observation(value=3.3, revision=2)], events=[])
    )
    assert EconomicMessageType.REVISED in mailbox.pending
    assert mailbox.pending[EconomicMessageType.REVISED].data.ids == ["observed:CPI_YOY:2026-08"]
    svc.unsubscribe(mailbox)


async def test_observed_release_survives_repository_reopen(tmp_path) -> None:
    path = tmp_path / "release.sqlite3"
    clock = FakeClock()
    repo = SQLiteEconomicRepository(path, EconomicsMode.LIVE)
    repo.initialize()
    svc = service(clock=clock, repository=repo)
    svc.accept_batch(batch())
    before = {item.id: item.actual for item in svc.observed_releases()}
    assert before == {"observed:CPI_YOY:2026-08": 3.2}
    await svc.stop()
    reopened = SQLiteEconomicRepository(path, EconomicsMode.LIVE)
    svc2 = service(clock=clock, repository=reopened)
    await svc2.start()
    try:
        after = {item.id: item.actual for item in svc2.observed_releases()}
        assert after == before
    finally:
        await svc2.stop()


def test_official_historical_schedule_backfill_does_not_publish_due_notification():
    svc = service()
    old = event(scheduled_at=T0-timedelta(days=35), provider='fred_release_calendar',
                status='due')
    svc._events = [old]
    mailbox = svc.subscribe()
    svc.tick()
    assert EconomicMessageType.DUE not in mailbox.pending
    assert svc.calendar_events()[0].status == EventStatus.DUE
