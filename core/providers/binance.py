"""Binance spot adapter for BTC.

Free, no key, JSON over HTTPS. Scope is deliberately narrow: the canonical
``BTC`` instrument is USD-quoted spot via ``BTCUSDT``. No other symbol is
routed here, so this adapter never silently absorbs an instrument that belongs
to another provider.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import datetime

import httpx

from core.domain.instruments import Instrument, get_instrument
from core.domain.models import MarketObservation, utc_now
from core.providers.base import MarketDataProvider, ProviderHealth

BINANCE_URL = "https://api.binance.com/api/v3/ticker/24hr"

Clock = Callable[[], datetime]


def _number(value: object) -> float | None:
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


class BinanceCryptoProvider(MarketDataProvider):
    """Polls Binance spot for BTC/USDT."""

    def __init__(
        self,
        *,
        update_interval: float = 10.0,
        clock: Clock = utc_now,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if update_interval <= 0:
            raise ValueError("update_interval must be positive")
        self._update_interval = update_interval
        self._clock = clock
        self._client = client
        self._owned = client is None
        self._instrument = get_instrument("BTC")
        if self._instrument is None:  # pragma: no cover - registry is static
            raise ValueError("BTC is not a registered instrument")
        self._closed = False
        self._last_success: datetime | None = None
        self._last_error: str | None = None
        self._failures = 0

    @property
    def name(self) -> str:
        return "binance"

    @property
    def instruments(self) -> Sequence[Instrument]:
        return (self._instrument,)

    async def _collect(self) -> list[MarketObservation]:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=10.0,
                limits=httpx.Limits(max_connections=2),
                headers={"User-Agent": "Omarkets/0.3 (+local workstation)"},
            )
        response = await self._client.get(BINANCE_URL, params={"symbol": "BTCUSDT"}, timeout=10.0)
        if response.status_code == 429:
            raise RuntimeError("Binance rate limited the request")
        if response.status_code != 200:
            raise RuntimeError(f"Binance returned HTTP {response.status_code}")
        payload = response.json()
        value = _number(payload.get("lastPrice"))
        if value is None or value <= 0:
            raise RuntimeError("Binance payload missing a usable price")
        previous = _number(payload.get("prevClosePrice"))
        now = self._clock()
        metadata: dict = {
            "provider_symbol": "BTCUSDT",
            "session_status": "CRYPTO",
            "feed_status": "live",
        }
        if previous is not None:
            metadata["previous_close"] = previous
        return [
            MarketObservation(
                symbol="BTC",
                timestamp=now,
                value=value,
                unit=self._instrument.unit,
                source=self.name,
                received_at=now,
                metadata=metadata,
            )
        ]

    async def start(self) -> None:
        self._closed = False

    async def fetch_snapshot(self) -> list[MarketObservation]:
        if self._closed:
            raise RuntimeError("provider is closed")
        return await self._collect()

    async def stream_updates(self) -> AsyncIterator[list[MarketObservation]]:
        backoff = self._update_interval
        try:
            while not self._closed:
                try:
                    observations = await self._collect()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._failures += 1
                    self._last_error = type(exc).__name__
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60.0)
                    continue
                backoff = self._update_interval
                self._last_success = self._clock()
                self._last_error = None
                self._failures = 0
                yield observations
                await asyncio.sleep(self._update_interval)
        except asyncio.CancelledError:
            raise

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            name=self.name,
            healthy=not self._closed and self._last_error is None,
            checked_at=self._clock(),
            detail="stopped" if self._closed else (self._last_error or "Binance spot poll"),
        )

    async def aclose(self) -> None:
        self._closed = True
        if self._owned and self._client is not None:
            await self._client.aclose()
            self._client = None


__all__ = ["BinanceCryptoProvider"]
