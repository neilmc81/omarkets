"""Composite market-data provider.

Omarkets can use more than one vendor at once (for example CNBC for futures,
indices, yields and equities plus Binance for crypto). This adapter presents
them as a single :class:`~core.providers.base.MarketDataProvider` by running each
child's stream concurrently and merging the batches onto one queue.

Properties that matter:

* each child keeps its own reconnect/backoff logic; one failing child must not
  stop the others,
* batches carry the observations only; the source identity travels on each
  observation and in the routing registry, not in a merged provider name,
* shutdown cancels every child task and closes every child exactly once.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence

from core.domain.instruments import Instrument
from core.domain.models import MarketObservation, utc_now
from core.providers.base import MarketDataProvider, ProviderHealth

logger = logging.getLogger(__name__)


class CompositeMarketDataProvider(MarketDataProvider):
    """Merges several providers into one stream."""

    def __init__(self, providers: Sequence[MarketDataProvider], *, name: str = "composite") -> None:
        if not providers:
            raise ValueError("composite provider requires at least one child")
        names = [provider.name for provider in providers]
        if len(set(names)) != len(names):
            raise ValueError("child provider names must be unique")
        self._providers = tuple(providers)
        self._name = name
        self._closed = False
        self._tasks: list[asyncio.Task[None]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def instruments(self) -> Sequence[Instrument]:
        seen: dict[str, Instrument] = {}
        for provider in self._providers:
            for instrument in provider.instruments:
                seen.setdefault(instrument.symbol, instrument)
        return tuple(seen.values())

    def children(self) -> Sequence[MarketDataProvider]:
        return self._providers

    async def start(self) -> None:
        self._closed = False
        for provider in self._providers:
            await provider.start()

    async def fetch_snapshot(self) -> list[MarketObservation]:
        if self._closed:
            raise RuntimeError("provider is closed")
        results = await asyncio.gather(
            *(provider.fetch_snapshot() for provider in self._providers),
            return_exceptions=True,
        )
        observations: list[MarketObservation] = []
        for provider, result in zip(self._providers, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning("provider %r snapshot failed: %s", provider.name, result)
                continue
            observations.extend(result)
        return observations

    async def stream_updates(self) -> AsyncIterator[list[MarketObservation]]:
        queue: asyncio.Queue[list[MarketObservation] | None] = asyncio.Queue(maxsize=32)

        async def pump(provider: MarketDataProvider) -> None:
            try:
                async for batch in provider.stream_updates():
                    if batch:
                        await queue.put(list(batch))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("composite child %r stream stopped", provider.name)

        self._tasks = [
            asyncio.create_task(pump(provider), name=f"omarkets-feed-{provider.name}")
            for provider in self._providers
        ]
        try:
            while not self._closed:
                item = await queue.get()
                if item is None:
                    continue
                yield item
        except asyncio.CancelledError:
            raise
        finally:
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks = []

    async def health(self) -> ProviderHealth:
        checked = utc_now()
        details = []
        healthy = True
        for provider in self._providers:
            report = await provider.health()
            healthy = healthy and report.healthy
            details.append(f"{report.name}:{'ok' if report.healthy else 'degraded'}")
        return ProviderHealth(
            name=self.name,
            healthy=healthy and not self._closed,
            checked_at=checked,
            detail=", ".join(details) or None,
        )

    async def aclose(self) -> None:
        self._closed = True
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks = []
        for provider in self._providers:
            try:
                await provider.aclose()
            except Exception:  # pragma: no cover - defensive
                logger.exception("error closing child provider %r", provider.name)


__all__ = ["CompositeMarketDataProvider"]
