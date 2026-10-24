"""Provider abstraction.

The Market Core depends on this interface, never on a specific vendor. Vendor
code lives behind an adapter that translates vendor payloads into the canonical
domain schemas. No provider SDK type may cross this boundary.

Phase 1 ships a single implementation, :class:`~core.providers.fake.FakeMarketDataProvider`,
which produces deterministic synthetic data and requires no network access.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence

from pydantic import AwareDatetime, BaseModel

from core.domain.instruments import Instrument
from core.domain.models import MarketObservation


class ProviderHealth(BaseModel):
    """Health of a provider adapter at a point in time."""

    name: str
    healthy: bool
    checked_at: AwareDatetime
    detail: str | None = None


class MarketDataProvider(ABC):
    """Source of market observations.

    Implementations must be safe to start and stop repeatedly and must not
    perform I/O at construction time. ``stream_updates`` is the primary path
    used by the state service; ``fetch_snapshot`` is a one-shot pull used for
    diagnostics and simple callers.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable identifier for the provider, e.g. ``"fake"``."""

    @property
    @abstractmethod
    def instruments(self) -> Sequence[Instrument]:
        """Instruments this provider can supply."""

    async def start(self) -> None:
        """Prepare the provider for streaming.

        The default is a no-op. Providers that release resources in
        :meth:`aclose` must override this so the provider can be started again
        after being stopped.
        """
        return None

    @abstractmethod
    async def fetch_snapshot(self) -> list[MarketObservation]:
        """Fetch one observation per configured instrument."""

    @abstractmethod
    def stream_updates(self) -> AsyncIterator[list[MarketObservation]]:
        """Yield batches of observations as they become available.

        The iterator runs until cancelled. It must await between emissions and
        must not busy-loop.
        """

    @abstractmethod
    async def health(self) -> ProviderHealth:
        """Report current provider health."""

    @abstractmethod
    async def aclose(self) -> None:
        """Release any resources held by the provider."""


class ProviderError(RuntimeError):
    """Raised by a provider when it cannot supply data."""

    def __init__(self, provider: str, detail: str) -> None:
        super().__init__(f"{provider}: {detail}")
        self.provider = provider
        self.detail = detail


class UnavailableInstrumentError(ProviderError, KeyError):
    """Raised when a provider cannot supply a requested instrument."""

    def __init__(self, provider: str, symbol: str) -> None:
        super().__init__(provider, f"instrument not available: {symbol}")
        self.symbol = symbol


__all__ = [
    "MarketDataProvider",
    "ProviderError",
    "ProviderHealth",
    "UnavailableInstrumentError",
]
