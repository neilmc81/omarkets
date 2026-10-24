"""Canonical Omarkets instrument universe.

These symbols are **internal canonical identifiers**. They are deliberately not
vendor tickers: a real provider adapter is responsible for mapping its own
symbol space onto these canonical symbols (see ``docs/architecture.md`` §3.4).

Phase 1 defines a fixed static universe. Adding an instrument means adding an
entry here; nothing else in the core hard-codes a symbol.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field


class AssetClass(StrEnum):
    """Broad asset class, used for staleness defaults and grouping."""

    EQUITY_INDEX_FUTURE = "equity_index_future"
    EQUITY = "equity"
    TREASURY_YIELD = "treasury_yield"
    FX_INDEX = "fx_index"
    VOLATILITY_INDEX = "volatility_index"
    COMMODITY = "commodity"
    CRYPTO = "crypto"


class QuoteType(StrEnum):
    """How an instrument's value should be interpreted."""

    PRICE = "price"
    YIELD = "yield"
    INDEX = "index"


class Instrument(BaseModel):
    """A canonical, tradeable or observable instrument."""

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    asset_class: AssetClass
    quote_type: QuoteType
    timezone: str = "America/New_York"
    unit: str = Field(min_length=1)
    metadata: dict[str, str] = Field(default_factory=dict)


# Default freshness threshold in seconds per asset class. Data older than its
# threshold is reported as "stale". Overridable via Settings.
DEFAULT_STALE_SECONDS: Mapping[AssetClass, float] = MappingProxyType(
    {
        AssetClass.EQUITY_INDEX_FUTURE: 5.0,
        AssetClass.EQUITY: 10.0,
        AssetClass.TREASURY_YIELD: 60.0,
        AssetClass.FX_INDEX: 30.0,
        AssetClass.VOLATILITY_INDEX: 30.0,
        AssetClass.COMMODITY: 60.0,
        AssetClass.CRYPTO: 10.0,
    }
)


def _instrument(
    symbol: str,
    display_name: str,
    asset_class: AssetClass,
    quote_type: QuoteType,
    unit: str,
    *,
    timezone: str = "America/New_York",
    **metadata: str,
) -> Instrument:
    return Instrument(
        symbol=symbol,
        display_name=display_name,
        asset_class=asset_class,
        quote_type=quote_type,
        unit=unit,
        timezone=timezone,
        metadata=metadata,
    )


_ALL: tuple[Instrument, ...] = (
    # --- Primary ---
    _instrument(
        "NQ",
        "E-mini Nasdaq-100 futures",
        AssetClass.EQUITY_INDEX_FUTURE,
        QuoteType.PRICE,
        "index points",
    ),
    # --- Cross-asset context ---
    _instrument(
        "ES",
        "E-mini S&P 500 futures",
        AssetClass.EQUITY_INDEX_FUTURE,
        QuoteType.PRICE,
        "index points",
    ),
    _instrument(
        "US2Y",
        "US 2-year Treasury yield",
        AssetClass.TREASURY_YIELD,
        QuoteType.YIELD,
        "percent",
    ),
    _instrument(
        "US10Y",
        "US 10-year Treasury yield",
        AssetClass.TREASURY_YIELD,
        QuoteType.YIELD,
        "percent",
    ),
    _instrument(
        "DXY",
        "US Dollar Index",
        AssetClass.FX_INDEX,
        QuoteType.INDEX,
        "index points",
    ),
    _instrument(
        "VIX",
        "CBOE Volatility Index",
        AssetClass.VOLATILITY_INDEX,
        QuoteType.INDEX,
        "index points",
    ),
    _instrument(
        "VXN",
        "CBOE Nasdaq-100 Volatility Index",
        AssetClass.VOLATILITY_INDEX,
        QuoteType.INDEX,
        "index points",
    ),
    _instrument(
        "WTI",
        "West Texas Intermediate crude oil",
        AssetClass.COMMODITY,
        QuoteType.PRICE,
        "USD per barrel",
    ),
    _instrument(
        "GOLD",
        "Gold futures",
        AssetClass.COMMODITY,
        QuoteType.PRICE,
        "USD per troy ounce",
    ),
    _instrument(
        "BTC",
        "Bitcoin",
        AssetClass.CRYPTO,
        QuoteType.PRICE,
        "USD",
        timezone="UTC",
    ),
    # --- Technology context ---
    _instrument("NVDA", "NVIDIA", AssetClass.EQUITY, QuoteType.PRICE, "USD"),
    _instrument("MSFT", "Microsoft", AssetClass.EQUITY, QuoteType.PRICE, "USD"),
    _instrument("AAPL", "Apple", AssetClass.EQUITY, QuoteType.PRICE, "USD"),
    _instrument("AMZN", "Amazon", AssetClass.EQUITY, QuoteType.PRICE, "USD"),
    _instrument("META", "Meta Platforms", AssetClass.EQUITY, QuoteType.PRICE, "USD"),
    _instrument("GOOGL", "Alphabet", AssetClass.EQUITY, QuoteType.PRICE, "USD"),
    _instrument("AVGO", "Broadcom", AssetClass.EQUITY, QuoteType.PRICE, "USD"),
    _instrument("TSLA", "Tesla", AssetClass.EQUITY, QuoteType.PRICE, "USD"),
)

#: The canonical instrument registry. Read-only: the universe is static.
INSTRUMENTS: Mapping[str, Instrument] = MappingProxyType(
    {instrument.symbol: instrument for instrument in _ALL}
)

#: The primary instrument Omarkets focuses on.
PRIMARY_SYMBOL = "NQ"


def normalize_symbol(symbol: str) -> str:
    """Normalize a user-supplied symbol to canonical form."""
    return symbol.strip().upper()


def get_instrument(symbol: str) -> Instrument | None:
    """Look up an instrument by symbol, or return ``None`` if unknown."""
    return INSTRUMENTS.get(normalize_symbol(symbol))


def all_symbols() -> tuple[str, ...]:
    """All canonical symbols, in registry order."""
    return tuple(INSTRUMENTS)
