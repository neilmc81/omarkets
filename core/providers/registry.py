"""Provider routing for market data.

Omarkets supports more than one market-data provider. This module records which
provider is authoritative for each canonical instrument so routing is
configuration, not scattered conditionals, and so the active source is always
explicit in the market state.

The registry does not perform I/O. It resolves a provider name for a symbol and
exposes the routing table for diagnostics.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from core.domain.instruments import INSTRUMENTS


class ProviderRoute(BaseModel):
    """The selected provider for one canonical instrument."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str = Field(min_length=1)
    provider: str = Field(min_length=1)


class ProviderRegistry:
    """Maps canonical symbols to the provider expected to supply them."""

    def __init__(self, routes: Mapping[str, str]) -> None:
        unknown = sorted(set(routes) - set(INSTRUMENTS))
        if unknown:
            raise ValueError(f"routes reference unknown instruments: {unknown}")
        self._routes: dict[str, ProviderRoute] = {
            symbol: ProviderRoute(symbol=symbol, provider=provider)
            for symbol, provider in routes.items()
        }

    def provider_for(self, symbol: str) -> str | None:
        route = self._routes.get(symbol.strip().upper())
        return route.provider if route else None

    def routes(self) -> list[ProviderRoute]:
        return [self._routes[symbol] for symbol in INSTRUMENTS if symbol in self._routes]

    def symbols_for(self, provider: str) -> tuple[str, ...]:
        return tuple(
            symbol
            for symbol in INSTRUMENTS
            if symbol in self._routes and self._routes[symbol].provider == provider
        )

    def with_routes(self, routes: Mapping[str, str]) -> ProviderRegistry:
        merged = {symbol: route.provider for symbol, route in self._routes.items()}
        merged.update(routes)
        return ProviderRegistry(merged)


#: Canonical symbol -> provider used when no override is configured.
DEFAULT_ROUTES: dict[str, str] = {
    "NQ": "cnbc",
    "ES": "cnbc",
    "US2Y": "cnbc",
    "US10Y": "cnbc",
    "DXY": "cnbc",
    "VIX": "cnbc",
    "VXN": "cnbc",
    "WTI": "cnbc",
    "GOLD": "cnbc",
    "NVDA": "cnbc",
    "MSFT": "cnbc",
    "AAPL": "cnbc",
    "AMZN": "cnbc",
    "META": "cnbc",
    "GOOGL": "cnbc",
    "AVGO": "cnbc",
    "TSLA": "cnbc",
    "BTC": "binance",
}
