"""Tests for the deterministic synthetic provider.

Everything here is driven by an injected frozen clock and an explicit seed, so
the tests never touch the wall clock or the network. The provider is the only
Phase 1 data source, so its determinism and lifecycle behavior are part of the
core contract.
"""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime

import pytest

from core.domain.instruments import AssetClass, Instrument, QuoteType
from core.providers.base import UnavailableInstrumentError
from core.providers.fake import _SIM, FakeMarketDataProvider

FROZEN = datetime(2026, 1, 2, 15, 30, 0, tzinfo=UTC)


def _frozen_clock() -> datetime:
    return FROZEN


def _provider(seed: int = 7, **kwargs: object) -> FakeMarketDataProvider:
    return FakeMarketDataProvider(seed=seed, clock=_frozen_clock, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


async def test_same_seed_and_clock_produce_identical_sequences() -> None:
    a = _provider(seed=7)
    b = _provider(seed=7)

    for _ in range(5):
        batch_a = await a.fetch_snapshot()
        batch_b = await b.fetch_snapshot()
        assert batch_a == batch_b

    # Also exercise the synchronous tick path and confirm it stays in lockstep.
    for _ in range(3):
        assert a._tick() == b._tick()


async def test_same_seed_identical_value_timestamp_metadata_tuples() -> None:
    a = _provider(seed=7)
    b = _provider(seed=7)

    for _ in range(4):
        batch_a = await a.fetch_snapshot()
        batch_b = await b.fetch_snapshot()
        tuples_a = [(o.value, o.timestamp, o.metadata) for o in batch_a]
        tuples_b = [(o.value, o.timestamp, o.metadata) for o in batch_b]
        assert tuples_a == tuples_b


async def test_different_seeds_produce_different_values() -> None:
    a = _provider(seed=7)
    b = _provider(seed=8)

    batch_a = await a.fetch_snapshot()
    batch_b = await b.fetch_snapshot()

    values_a = [o.value for o in batch_a]
    values_b = [o.value for o in batch_b]
    assert values_a != values_b


# ---------------------------------------------------------------------------
# Snapshot shape
# ---------------------------------------------------------------------------


async def test_fetch_snapshot_returns_one_observation_per_instrument() -> None:
    provider = _provider()
    batch = await provider.fetch_snapshot()

    assert len(batch) == len(provider.instruments)
    assert len(batch) == 18
    assert {o.symbol for o in batch} == {i.symbol for i in provider.instruments}


async def test_fetch_snapshot_timestamps_use_frozen_clock() -> None:
    provider = _provider()
    batch = await provider.fetch_snapshot()

    assert all(o.timestamp == FROZEN for o in batch)
    assert all(o.received_at == FROZEN for o in batch)


async def test_fetch_snapshot_values_are_finite() -> None:
    provider = _provider()
    batch = await provider.fetch_snapshot()

    assert all(math.isfinite(o.value) for o in batch)


async def test_fetch_snapshot_units_match_instruments() -> None:
    provider = _provider()
    batch = await provider.fetch_snapshot()

    for obs in batch:
        instrument = next(i for i in provider.instruments if i.symbol == obs.symbol)
        assert obs.unit == instrument.unit


# ---------------------------------------------------------------------------
# Plausibility
# ---------------------------------------------------------------------------


async def test_yields_stay_positive_and_plausible() -> None:
    provider = _provider()
    for _ in range(200):
        batch = await provider.fetch_snapshot()
        for obs in batch:
            if obs.symbol in ("US2Y", "US10Y"):
                assert obs.value > 0.0
                assert obs.value < 20.0


async def test_volatility_indices_stay_positive() -> None:
    provider = _provider()
    for _ in range(200):
        batch = await provider.fetch_snapshot()
        for obs in batch:
            if obs.symbol in ("VIX", "VXN"):
                assert obs.value > 0.0


# ---------------------------------------------------------------------------
# Construction errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("interval", [0, -1.0, -0.001])
def test_non_positive_update_interval_raises_value_error(interval: float) -> None:
    with pytest.raises(ValueError):
        FakeMarketDataProvider(update_interval=interval, clock=_frozen_clock)


def test_instrument_without_simulation_parameters_raises_unavailable() -> None:
    unknown = Instrument(
        symbol="ZZZ",
        display_name="Unknown instrument",
        asset_class=AssetClass.EQUITY,
        quote_type=QuoteType.PRICE,
        unit="USD",
    )
    with pytest.raises(UnavailableInstrumentError) as excinfo:
        FakeMarketDataProvider(seed=1, instruments=[unknown], clock=_frozen_clock)
    assert excinfo.value.symbol == "ZZZ"
    assert excinfo.value.provider == "fake"


# ---------------------------------------------------------------------------
# Streaming lifecycle
# ---------------------------------------------------------------------------


async def test_stream_updates_yields_complete_batches_and_reports_healthy() -> None:
    provider = _provider(update_interval=0.001)

    batches: list[list] = []
    async for batch in provider.stream_updates():
        batches.append(batch)
        if len(batches) == 3:
            break

    assert len(batches) == 3
    for batch in batches:
        assert len(batch) == 18
        assert {o.symbol for o in batch} == {i.symbol for i in provider.instruments}
        assert all(o.timestamp == FROZEN for o in batch)

    health = await provider.health()
    assert health.healthy is True
    assert health.name == "fake"


async def test_stream_updates_yields_to_event_loop_between_batches() -> None:
    """A concurrent task must make progress, proving the stream awaits."""
    provider = _provider(update_interval=0.001)
    progress = 0

    async def spin() -> None:
        nonlocal progress
        while True:
            progress += 1
            await asyncio.sleep(0)

    spin_task = asyncio.create_task(spin())
    try:
        batches = []
        async for batch in provider.stream_updates():
            batches.append(batch)
            if len(batches) == 3:
                break
    finally:
        spin_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await spin_task

    assert progress > 0


async def test_aclose_marks_unhealthy_and_blocks_snapshot() -> None:
    provider = _provider()

    health_before = await provider.health()
    assert health_before.healthy is True

    await provider.aclose()

    health_after = await provider.health()
    assert health_after.healthy is False
    assert health_after.detail == "stopped"

    with pytest.raises(RuntimeError):
        await provider.fetch_snapshot()


async def test_stream_updates_is_cancellable_cleanly() -> None:
    provider = _provider(update_interval=0.001)

    async def consume() -> None:
        async for _batch in provider.stream_updates():
            pass

    task = asyncio.create_task(consume())
    # Let the stream emit at least once before cancelling.
    await asyncio.sleep(0.005)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelled()


# ---------------------------------------------------------------------------
# Metadata and step size
# ---------------------------------------------------------------------------


async def test_metadata_contains_previous_close() -> None:
    provider = _provider()
    batch = await provider.fetch_snapshot()

    for obs in batch:
        assert "previous_close" in obs.metadata
        assert isinstance(obs.metadata["previous_close"], float)
        assert math.isfinite(obs.metadata["previous_close"])


async def test_values_move_in_small_steps_relative_to_sigma() -> None:
    provider = _provider(seed=7)
    previous: dict[str, float] = {}
    max_ratio = 0.0

    for _ in range(100):
        batch = await provider.fetch_snapshot()
        for obs in batch:
            if obs.symbol in previous:
                sigma = _SIM[obs.symbol].sigma
                ratio = abs(obs.value - previous[obs.symbol]) / sigma
                max_ratio = max(max_ratio, ratio)
            previous[obs.symbol] = obs.value

    # A random walk can occasionally exceed 3 sigma; 8x is a generous bound
    # that still fails loudly if the provider emits white noise or jumps.
    assert max_ratio < 8.0
