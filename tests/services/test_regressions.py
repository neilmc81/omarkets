"""Regression tests for defects found during independent Phase 1 testing.

Each test pins a specific failure mode that was found by review/testing and
subsequently fixed in ``core/``. They exist to stop those defects from
returning: out-of-order tick handling, naive datetime query params, service
restart after stop, and subscriber release on shutdown.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from core.api.app import build_service, create_app
from core.config.settings import Settings
from core.domain.instruments import INSTRUMENTS, get_instrument
from core.domain.models import MarketObservation
from core.providers.base import MarketDataProvider, ProviderHealth
from core.providers.fake import FakeMarketDataProvider
from core.services.market_state import MarketStateService, SubscriptionClosed
from core.storage.duckdb_repo import DuckDBObservationRepository

BASE = datetime(2026, 1, 2, 15, 30, tzinfo=UTC)


class ScriptedProvider(MarketDataProvider):
    """Emits a fixed list of batches, then idles. No wall clock, no randomness."""

    def __init__(self, batches: list[list[MarketObservation]]) -> None:
        self._batches = batches
        self._closed = False
        self._index = 0

    @property
    def name(self) -> str:
        return "scripted"

    @property
    def instruments(self):
        return list(INSTRUMENTS.values())

    async def fetch_snapshot(self):
        return []

    async def stream_updates(self):
        while not self._closed:
            if self._index < len(self._batches):
                yield self._batches[self._index]
                self._index += 1
            else:
                await asyncio.sleep(0.001)

    async def health(self):
        return ProviderHealth(name=self.name, healthy=True, checked_at=BASE)

    async def aclose(self):
        self._closed = True


def _obs(symbol: str, ts: datetime, value: float) -> MarketObservation:
    return MarketObservation(
        symbol=symbol,
        timestamp=ts,
        value=value,
        unit=get_instrument(symbol).unit,  # type: ignore[union-attr]
        source="scripted",
        received_at=ts,
        metadata={"previous_close": 100.0},
    )


async def _wait_for(predicate, time_limit: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + time_limit
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("condition not met within timeout")


async def test_out_of_order_tick_does_not_overwrite_newer_cached_observation():
    """An older tick must not replace a newer cached value.

    Storage keeps the latest by event time; the in-memory cache must use the
    same rule so the API and storage cannot disagree.
    """
    newer = BASE + timedelta(seconds=10)
    older = BASE + timedelta(seconds=5)
    provider = ScriptedProvider(
        [
            [_obs("NQ", newer, 200.0)],
            [_obs("NQ", older, 100.0)],
        ]
    )
    repo = DuckDBObservationRepository(":memory:")
    service = MarketStateService(provider=provider, repository=repo, clock=lambda: newer)
    await service.start()
    try:
        await _wait_for(lambda: service.updates_published >= 2)

        api_value = service.current_state(now=newer).instruments["NQ"].value
        stored_value = repo.latest_per_instrument()["NQ"].value

        assert stored_value == 200.0
        assert api_value == stored_value, (
            "in-memory state served an older out-of-order tick as current: "
            f"api={api_value}, storage={stored_value}"
        )
    finally:
        await service.stop()


async def test_full_tie_keeps_greatest_value_in_cache_and_storage():
    """On identical event and receipt times, cache and storage agree on the value.

    Storage breaks the tie deterministically by value; the in-memory cache must
    use the same rule or a restart could surface a different value.
    """
    provider = ScriptedProvider(
        [
            [_obs("NQ", BASE, 100.0)],
            [_obs("NQ", BASE, 300.0)],
        ]
    )
    repo = DuckDBObservationRepository(":memory:")
    service = MarketStateService(provider=provider, repository=repo, clock=lambda: BASE)
    await service.start()
    try:
        await _wait_for(lambda: service.updates_published >= 2)
        api_value = service.current_state(now=BASE).instruments["NQ"].value
        stored_value = repo.latest_per_instrument()["NQ"].value
        assert stored_value == 300.0
        assert api_value == stored_value
    finally:
        await service.stop()


def test_naive_history_query_is_a_client_error():
    """A naive datetime query param is rejected with 422, not a 500.

    The API boundary must reject naive datetimes; storage must never be asked
    to interpret a timezone-less instant.
    """
    with tempfile.TemporaryDirectory() as td:
        settings = Settings(
            duckdb_path=Path(td) / "naive.duckdb",
            provider_update_interval=99.0,
            provider_seed=1,
        )
        repo = DuckDBObservationRepository(settings.resolved_duckdb_path)
        provider = FakeMarketDataProvider(seed=1, update_interval=99.0)
        service = build_service(settings=settings, provider=provider, repository=repo)
        with TestClient(create_app(service=service), raise_server_exceptions=False) as client:
            response = client.get("/api/v1/history/NQ", params={"start": "2026-01-02T15:00:00"})
            assert response.status_code == 422, (
                "naive datetime query param should be a client error, "
                f"got HTTP {response.status_code}"
            )


async def test_service_can_restart_after_stop():
    """The service can be started again after ``stop()``.

    The provider interface documents start/stop as repeatable during the
    lifecycle, which the FastAPI lifespan relies on."""
    provider = FakeMarketDataProvider(seed=7, update_interval=0.001)
    repo = DuckDBObservationRepository(":memory:")
    service = MarketStateService(provider=provider, repository=repo)
    await service.start()
    try:
        await _wait_for(lambda: service.updates_published >= 2)
        first = service.updates_published
    finally:
        await service.stop()

    await service.start()
    try:
        try:
            await _wait_for(lambda: service.updates_published > first, time_limit=0.5)
        except AssertionError:
            pytest.fail(
                "restart after stop() produced no updates: "
                f"updates_published stayed at {first}, is_running={service.is_running}"
            )
    finally:
        await service.stop()


async def test_subscriber_is_released_on_service_stop():
    """A subscriber blocked on ``get()`` is released when the service stops."""
    provider = ScriptedProvider([[_obs("NQ", BASE, 100.0)]])
    repo = DuckDBObservationRepository(":memory:")
    service = MarketStateService(provider=provider, repository=repo, clock=lambda: BASE)
    await service.start()
    try:
        await _wait_for(lambda: service.updates_published >= 1)
        _client_id, subscription = service.subscribe()
        await subscription.get()  # drain the initial snapshot

        await service.stop()

        with pytest.raises(SubscriptionClosed):
            await asyncio.wait_for(subscription.get(), timeout=0.3)
    finally:
        if service.is_running:
            await service.stop()
