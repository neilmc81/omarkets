"""Deterministic synthetic market-data provider.

This is the only provider in Phase 1. It requires no network access and is
fully deterministic given a seed and a clock, which makes it usable in tests.

The simulation is intentionally simple and honest about being synthetic:

* prices follow a small-step random walk,
* yields and volatility indices mean-revert around a starting level,

so that series look plausible rather than like white noise. ``previous_close``
is generated deterministically from the seed and carried on each observation's
metadata; the core never invents a baseline of its own.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from core.domain.instruments import AssetClass, Instrument, get_instrument
from core.domain.models import MarketObservation, utc_now
from core.providers.base import (
    MarketDataProvider,
    ProviderHealth,
    UnavailableInstrumentError,
)

Clock = Callable[[], datetime]


@dataclass(frozen=True)
class _Sim:
    """Per-instrument simulation parameters."""

    start: float
    sigma: float
    model: str  # "walk" | "mean_revert"
    spread: float = 0.0  # typical bid/ask half-spread, for realism only


# Plausible Phase 1 levels. Exact numbers do not matter; shape does.
_SIM: dict[str, _Sim] = {
    # Primary and index futures (index points)
    "NQ": _Sim(start=24800.0, sigma=12.0, model="walk"),
    "ES": _Sim(start=6350.0, sigma=3.0, model="walk"),
    "YM": _Sim(start=44800.0, sigma=20.0, model="walk"),
    # Treasury yields (percentage points)
    "US2Y": _Sim(start=3.91, sigma=0.012, model="mean_revert"),
    "US10Y": _Sim(start=4.28, sigma=0.010, model="mean_revert"),
    # Dollar index
    "DXY": _Sim(start=101.40, sigma=0.06, model="mean_revert"),
    # Volatility indices
    "VIX": _Sim(start=16.8, sigma=0.10, model="mean_revert"),
    "VXN": _Sim(start=20.5, sigma=0.14, model="mean_revert"),
    # Commodities
    "WTI": _Sim(start=71.2, sigma=0.08, model="walk"),
    "GOLD": _Sim(start=2685.0, sigma=1.6, model="walk"),
    # Crypto
    "BTC": _Sim(start=96500.0, sigma=55.0, model="walk"),
    # Technology context (USD)
    "NVDA": _Sim(start=138.20, sigma=0.35, model="walk"),
    "MSFT": _Sim(start=428.50, sigma=0.80, model="walk"),
    "AAPL": _Sim(start=232.10, sigma=0.45, model="walk"),
    "AMZN": _Sim(start=201.30, sigma=0.42, model="walk"),
    "META": _Sim(start=612.40, sigma=1.30, model="walk"),
    "GOOGL": _Sim(start=176.80, sigma=0.38, model="walk"),
    "AVGO": _Sim(start=171.60, sigma=0.55, model="walk"),
    "TSLA": _Sim(start=248.90, sigma=0.95, model="walk"),
}


class FakeMarketDataProvider(MarketDataProvider):
    """A deterministic, offline synthetic provider."""

    def __init__(
        self,
        *,
        seed: int | None = 7,
        update_interval: float = 2.0,
        instruments: Sequence[Instrument] | None = None,
        clock: Clock | None = None,
    ) -> None:
        if update_interval <= 0:
            raise ValueError("update_interval must be positive")
        self._update_interval = update_interval
        self._clock: Clock = clock or utc_now

        if instruments is None:
            instruments = [get_instrument(sym) for sym in _SIM]
        self._instruments: tuple[Instrument, ...] = tuple(instruments)

        # Two independent seeded RNGs keep the baseline stable regardless of
        # how many ticks have been emitted.
        baseline_rng = random.Random(seed)
        self._levels: dict[str, float] = {}
        self._baselines: dict[str, float] = {}
        for instrument in self._instruments:
            sim = _SIM.get(instrument.symbol)
            if sim is None:
                raise UnavailableInstrumentError("fake", instrument.symbol)
            self._levels[instrument.symbol] = sim.start
            # A small deterministic overnight move, so change != 0 at start.
            drift = baseline_rng.gauss(0.0, sim.sigma * 4.0)
            self._baselines[instrument.symbol] = sim.start - drift

        self._rng = random.Random(seed)
        self._closed = False

    @property
    def name(self) -> str:
        return "fake"

    @property
    def instruments(self) -> Sequence[Instrument]:
        return self._instruments

    def _sim(self, symbol: str) -> _Sim:
        sim = _SIM.get(symbol)
        if sim is None:
            raise UnavailableInstrumentError(self.name, symbol)
        return sim

    def _step(self, instrument: Instrument) -> None:
        sim = self._sim(instrument.symbol)
        current = self._levels[instrument.symbol]
        shock = self._rng.gauss(0.0, sim.sigma)
        if sim.model == "mean_revert":
            # Ornstein-Uhlenbeck-like pull back toward the start level.
            pull = 0.05 * (sim.start - current)
            nxt = current + pull + shock
            if instrument.asset_class in (
                AssetClass.TREASURY_YIELD,
                AssetClass.VOLATILITY_INDEX,
            ):
                nxt = max(nxt, 0.01)
        else:
            nxt = current + shock
            nxt = max(nxt, 0.01)
        self._levels[instrument.symbol] = nxt

    def _observe(self, instrument: Instrument, timestamp: datetime) -> MarketObservation:
        sim = self._sim(instrument.symbol)
        self._step(instrument)
        level = self._levels[instrument.symbol]
        half_spread = sim.spread if sim.spread else max(level * 1e-5, 1e-6)
        return MarketObservation(
            symbol=instrument.symbol,
            timestamp=timestamp,
            value=round(level, 4),
            unit=instrument.unit,
            source=self.name,
            received_at=self._clock(),
            metadata={
                "previous_close": round(self._baselines[instrument.symbol], 4),
                "bid": round(level - half_spread, 4),
                "ask": round(level + half_spread, 4),
                "simulated": True,
            },
        )

    def _tick(self) -> list[MarketObservation]:
        # One shared event timestamp per batch, matching a real snapshot.
        timestamp = self._clock()
        return [self._observe(instrument, timestamp) for instrument in self._instruments]

    async def start(self) -> None:
        self._closed = False

    async def fetch_snapshot(self) -> list[MarketObservation]:
        if self._closed:
            raise RuntimeError("provider is closed")
        return self._tick()

    async def stream_updates(self) -> AsyncIterator[list[MarketObservation]]:
        try:
            while not self._closed:
                yield self._tick()
                await asyncio.sleep(self._update_interval)
        except asyncio.CancelledError:
            raise

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            name=self.name,
            healthy=not self._closed,
            checked_at=self._clock(),
            detail="stopped" if self._closed else "synthetic data (no network)",
        )

    async def aclose(self) -> None:
        self._closed = True


__all__ = ["FakeMarketDataProvider", "Clock"]
