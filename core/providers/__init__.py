"""Market-data provider adapters.

Vendor-specific code is isolated behind :class:`core.providers.base.MarketDataProvider`.
Phase 1 ships only a deterministic offline fake provider.
"""

from core.providers.base import (
    MarketDataProvider,
    ProviderError,
    ProviderHealth,
    UnavailableInstrumentError,
)
from core.providers.fake import FakeMarketDataProvider

__all__ = [
    "FakeMarketDataProvider",
    "MarketDataProvider",
    "ProviderError",
    "ProviderHealth",
    "UnavailableInstrumentError",
]
