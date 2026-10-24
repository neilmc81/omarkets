"""Tests for MarketStateService lifecycle, seeding, storage, and fan-out."""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from core.domain.instruments import all_symbols
from core.domain.models import (
    DataStatus,
    MarketObservation,
    ProviderStatus,
)
from core.providers.base import MarketDataProvider, ProviderHealth
from core.providers.fake import FakeMarketDataProvider
from core.services.market_state import MarketStateService, StalePolicy


class StubProvider(MarketDataProvider):
    """A controllable provider for lifecycle tests. No sleeping, no randomness."""

    BASE = datetime(2026, 1, 2, 15, 30, tzinfo=UTC)

    def __init__(self, *, fail_after: int | None = None) -> None:
        self._closed = False
        self._fail_after = fail_after
        self.emitted = 0
        self._first = asyncio.Event()

    @property
    def name(self) -> str:
        return "stub"

    def now(self) -> datetime:
        """Clock consistent with the timestamps this stub emits."""
        return self.BASE + timedelta(seconds=self.emitted)

    @property
    def instruments(self):
        from core.domain.instruments import INSTRUMENTS

        return list(INSTRUMENTS.values())

    def _batch(self) -> list[MarketObservation]:
        self.emitted += 1
        ts = self.BASE + timedelta(seconds=self.emitted)
        obs = []
        for symbol in all_symbols():
            from core.domain.instruments import get_instrument

            unit = get_instrument(symbol).unit  # type: ignore[union-attr]
            obs.append(
                MarketObservation(
                    symbol=symbol,
                    timestamp=ts,
                    value=100.0 + self.emitted,
                    unit=unit,
                    source="stub",
                    received_at=ts,
                    metadata={"previous_close": 100.0},
                )
            )
        return obs

    async def fetch_snapshot(self):
        return self._batch()

    async def stream_updates(self):
        try:
            while not self._closed:
                if self._fail_after is not None and self.emitted >= self._fail_after:
                    raise RuntimeError("stub provider failure")
                yield self._batch()
                self._first.set()
                # Yield control so the service can process each batch, without
                # introducing wall-clock delay.
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise

    async def health(self):
        return ProviderHealth(
            name=self.name, healthy=not self._closed, checked_at=datetime.now(UTC)
        )

    async def aclose(self):
        self._closed = True


async def _wait_for(predicate, time_limit: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + time_limit
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("condition not met within timeout")


async def test_return_snapshot_refreshes_open_market(service_factory):
    provider = StubProvider()
    service = service_factory(provider, clock=provider.now)
    await service.refresh_snapshot()
    assert provider.emitted == 1
    assert service.current_state().instruments["NQ"].value == 101.0


async def test_return_snapshot_keeps_weekend_quiet(service_factory):
    provider = StubProvider()
    saturday = datetime(2026, 10, 3, 15, tzinfo=UTC)
    service = service_factory(provider, clock=lambda: saturday)
    await service.refresh_snapshot()
    assert provider.emitted == 0


@pytest.fixture
def service_factory(repo):
    created: list[MarketStateService] = []

    def factory(provider, policy=None, clock=None):
        # Keep the service clock consistent with the provider's synthetic time
        # unless a test injects its own.
        if clock is None and hasattr(provider, "now"):
            clock = provider.now
        service = MarketStateService(provider=provider, repository=repo, policy=policy, clock=clock)
        created.append(service)
        return service

    yield factory
    for service in created:
        if service.is_running:
            raise AssertionError("test leaked a running MarketStateService")


async def test_start_seeds_from_storage_and_stops_cleanly(service_factory, repo):
    ts = datetime(2026, 1, 2, 15, 29, tzinfo=UTC)
    repo.store(
        [
            MarketObservation(
                symbol="NQ",
                timestamp=ts,
                value=24000.0,
                unit="index points",
                source="seed",
                received_at=ts,
                metadata={"previous_close": 23900.0},
            )
        ]
    )

    provider = StubProvider()
    service = service_factory(provider)
    await service.start()
    assert service.is_running

    # Seeded instrument is visible before any provider tick is processed.
    nq = service.current_state(now=ts).instruments["NQ"]
    assert nq.value == 24000.0
    assert nq.source == "seed"

    await service.stop()
    assert not service.is_running
    assert service.provider_status is ProviderStatus.STOPPED


async def test_state_becomes_live_after_updates(service_factory, repo):
    provider = StubProvider()
    service = service_factory(provider)
    await service.start()

    await _wait_for(lambda: service.updates_published >= 1)
    state = service.current_state()
    assert state.live_count > 0
    assert state.provider == "stub"
    assert state.provider_status is ProviderStatus.OK
    assert len(state.instruments) == len(all_symbols())

    stored = repo.count()
    # Every processed batch persists exactly one observation per instrument.
    assert stored > 0
    assert stored % len(all_symbols()) == 0
    # The worker-thread write may finish the next batch before the event-loop
    # continuation increments updates_published for that batch.
    assert (
        service.updates_published
        <= stored // len(all_symbols())
        <= service.updates_published + 1
    )

    await service.stop()


async def test_unstarted_service_reports_unavailable(service_factory):
    service = service_factory(StubProvider())
    state = service.current_state()
    assert state.unavailable_count == len(all_symbols())
    assert all(s.status is DataStatus.UNAVAILABLE for s in state.instruments.values())


async def test_stale_after_threshold_is_reported(service_factory, repo):
    ts = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)
    repo.store(
        [
            MarketObservation(
                symbol="NQ",
                timestamp=ts,
                value=24000.0,
                unit="index points",
                source="seed",
                received_at=ts,
                metadata={},
            )
        ]
    )
    provider = StubProvider()
    service = service_factory(
        provider,
        policy=StalePolicy(default_seconds=1.0, overrides={}),  # NQ default is 5s
    )
    await service.start()

    # One minute after the observation, NQ is stale, not unavailable.
    late = ts + timedelta(minutes=1)
    nq = service.current_state(now=late).instruments["NQ"]
    assert nq.status is DataStatus.STALE
    assert nq.age_seconds == pytest.approx(60.0)
    assert nq.value == 24000.0

    await service.stop()


async def test_subscriber_receives_immediate_snapshot(service_factory):
    service = service_factory(StubProvider())
    client_id, subscription = service.subscribe()
    state = await asyncio.wait_for(subscription.get(), timeout=1.0)
    assert state.unavailable_count == len(all_symbols())
    assert service.subscriber_count == 1
    service.unsubscribe(client_id)
    assert service.subscriber_count == 0


async def test_subscriber_receives_updates(service_factory):
    provider = StubProvider()
    service = service_factory(provider)
    await service.start()

    client_id, subscription = service.subscribe()
    # First message is the current snapshot; later ones are live updates.
    await asyncio.wait_for(subscription.get(), timeout=1.0)
    state = await asyncio.wait_for(subscription.get(), timeout=1.0)
    assert state.live_count > 0

    service.unsubscribe(client_id)
    await service.stop()


async def test_late_observation_reaches_technical_subscriber_without_replacing_latest(
    service_factory,
):
    service = service_factory(StubProvider())
    newer = MarketObservation(
        symbol="NQ", timestamp=datetime(2026, 1, 2, 15, 31, tzinfo=UTC),
        value=101.0, unit="index points", source="stub",
        received_at=datetime(2026, 1, 2, 15, 31, tzinfo=UTC),
    )
    older = newer.model_copy(update={
        "timestamp": newer.timestamp - timedelta(seconds=30),
        "value": 99.0,
        "received_at": newer.received_at + timedelta(seconds=1),
    })
    await service._handle_batch([newer])
    client_id, subscription = service.subscribe_observations()
    await service._handle_batch([older])

    assert service.observation_for("NQ") == newer
    assert await subscription.get() == [older]
    service.unsubscribe_observations(client_id)


async def test_subscriber_mailbox_keeps_latest(service_factory):
    """Slow clients must not accumulate a backlog; newest snapshot wins."""
    service = service_factory(StubProvider())
    client_id, subscription = service.subscribe()
    await subscription.get()  # drain the initial snapshot
    for _ in range(10):
        service._publish(service.current_state())
    # Only the most recent state remains queued.
    assert subscription.queue.qsize() == 1
    service.unsubscribe(client_id)


async def test_service_survives_provider_failure(service_factory, repo):
    provider = StubProvider(fail_after=2)
    service = service_factory(provider)
    await service.start()

    await _wait_for(lambda: service.provider_status is ProviderStatus.ERROR)
    # Storage still holds the ticks that succeeded before the failure.
    assert repo.count() >= len(all_symbols())

    await service.stop()


async def test_stop_is_idempotent(service_factory):
    service = service_factory(StubProvider())
    await service.start()
    await service.stop()
    await service.stop()
    assert not service.is_running


async def test_stop_waits_for_inflight_worker_write_without_blocking_loop(
    service_factory, repo, monkeypatch
):
    writing = threading.Event()
    original_store = repo.store

    def slow_store(observations):
        writing.set()
        time.sleep(0.15)
        return original_store(observations)

    monkeypatch.setattr(repo, "store", slow_store)
    service = service_factory(StubProvider())
    await service.start()
    await _wait_for(writing.is_set)
    stopping = asyncio.create_task(service.stop())
    await asyncio.sleep(0.02)
    assert not stopping.done()
    await stopping
    assert not service.is_running


async def test_stop_preserves_cancellation_when_inflight_write_fails(
    service_factory, repo, monkeypatch
):
    from core.storage.base import StorageError

    writing = threading.Event()
    release = threading.Event()

    def failing_store(_observations):
        writing.set()
        if not release.wait(timeout=2):
            raise AssertionError("test did not release storage worker")
        raise StorageError("simulated inflight write failure")

    monkeypatch.setattr(repo, "store", failing_store)
    service = service_factory(StubProvider())
    await service.start()
    await _wait_for(writing.is_set)
    stopping = asyncio.create_task(service.stop())
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(stopping, timeout=2)
    assert not service.is_running


async def test_fake_provider_end_to_end(service_factory, repo):
    provider = FakeMarketDataProvider(
        seed=11, update_interval=0.01, clock=lambda: datetime.now(UTC)
    )
    service = service_factory(provider)
    await service.start()

    await _wait_for(lambda: service.updates_published >= 2, time_limit=3.0)
    state = service.current_state()
    assert state.live_count == len(all_symbols())
    assert state.provider == "fake"
    assert service.cached_instrument_count() == len(all_symbols())

    history = service.history("NQ", limit=10)
    assert len(history) >= 2
    assert [h.timestamp for h in history] == sorted(h.timestamp for h in history)

    await service.stop()


async def test_updates_iterator_unsubscribes_when_closed(service_factory):
    """The convenience iterator must release its subscription on close."""
    provider = StubProvider()
    service = service_factory(provider)
    await service.start()
    await _wait_for(lambda: service.updates_published >= 1)

    iterator = service.updates()
    first = await asyncio.wait_for(iterator.__anext__(), timeout=1.0)
    assert first.provider == "stub"
    assert service.subscriber_count == 1

    await iterator.aclose()
    assert service.subscriber_count == 0

    await service.stop()


async def test_health_detail_reports_components(service_factory):
    provider = StubProvider()
    service = service_factory(provider)
    await service.start()
    await _wait_for(lambda: service.updates_published >= 1)

    detail = service.health_detail()
    assert detail.provider == "stub"
    assert detail.provider_status.value == "ok"
    assert detail.provider_error is None
    assert detail.stream_running is True
    assert detail.cached_instruments == len(all_symbols())
    assert detail.updates_published >= 1
    assert detail.last_update_at is not None
    assert detail.storage_healthy is True

    await service.stop()
    stopped = service.health_detail()
    assert stopped.provider_status.value == "stopped"
    assert stopped.stream_running is False


async def test_storage_failure_does_not_kill_stream(repo, monkeypatch, service_factory):
    provider = StubProvider()
    service = service_factory(provider)

    def boom(_observations):
        from core.storage.base import StorageError

        raise StorageError("simulated write failure")

    monkeypatch.setattr(repo, "store", boom)
    await service.start()

    await _wait_for(lambda: service.updates_published >= 1)
    assert service.is_running
    assert service.current_state().live_count > 0

    await service.stop()


@pytest.mark.parametrize(
    ("store_seconds", "expected"),
    [(0.3, "DEBUG"), (0.0, None)],
)
async def test_an_ordinary_slow_disk_sync_is_not_a_warning(
    service_factory, repo, monkeypatch, caplog, store_seconds, expected
):
    """Oct 21, 2026: the insert takes ms; DuckDB's disk sync on this machine
    often takes 0.2-1 s off the event loop, and stalls 2-3.6 s a few times an
    hour (Oct 22). Only >= 4 s, or Omarkets itself holding the batch up, is a
    warning."""
    import core.services.market_state as market_state

    original_store = repo.store

    def store(observations):
        time.sleep(store_seconds)
        return original_store(observations)

    monkeypatch.setattr(repo, "store", store)
    service = service_factory(StubProvider())
    observation = MarketObservation(
        symbol="NQ", timestamp=datetime(2026, 1, 2, 15, 31, tzinfo=UTC),
        value=101.0, unit="index points", source="stub",
        received_at=datetime(2026, 1, 2, 15, 31, tzinfo=UTC),
    )
    with caplog.at_level("DEBUG", logger="core.services.market_state"):
        await service._handle_batch([observation])
    levels = [r.levelname for r in caplog.records if "market_batch" in r.getMessage()
              and "duration_ms" in r.getMessage()]
    assert levels == ([expected] if expected else [])

    caplog.clear()
    monkeypatch.setattr(market_state, "SLOW_STORE_MS", 100.0)
    monkeypatch.setattr(repo, "store", lambda obs: (time.sleep(0.3), original_store(obs))[1])
    with caplog.at_level("DEBUG", logger="core.services.market_state"):
        await service._handle_batch([observation.model_copy(update={"value": 102.0})])
    levels = [r.levelname for r in caplog.records if "market_batch" in r.getMessage()
              and "duration_ms" in r.getMessage()]
    assert levels == ["WARNING"]


def test_disk_sync_stall_seen_on_this_machine_is_below_the_warning_limit():
    """Oct 22, 2026: a bare fsync on this disk stalled 2.2 s and DuckDB commits
    reached 3.6 s with no Omarkets cause; 2 s warned ~25 times a night."""
    import core.services.market_state as market_state

    assert 3650 < market_state.SLOW_STORE_MS
