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
    #: A cash index (SPX, DAX), as distinct from its futures.
    EQUITY_INDEX = "equity_index"
    EQUITY = "equity"
    TREASURY_YIELD = "treasury_yield"
    #: A currency index such as DXY, as distinct from a currency pair.
    FX_INDEX = "fx_index"
    #: A spot currency pair such as EURUSD.
    FX_SPOT = "fx_spot"
    VOLATILITY_INDEX = "volatility_index"
    COMMODITY = "commodity"
    CRYPTO = "crypto"


class QuoteType(StrEnum):
    """How an instrument's value should be interpreted."""

    PRICE = "price"
    YIELD = "yield"
    INDEX = "index"


class SessionCalendarId(StrEnum):
    """Which trading calendar decides when an instrument's market is open.

    The calendars themselves live in :mod:`core.domain.calendars`.
    """

    #: CME Globex (Sunday-Friday 18:00-17:00 ET, daily break, CME holidays).
    CME_GLOBEX = "cme_globex"
    #: Always open; a "day" is 00:00-24:00 UTC (crypto).
    ALWAYS_OPEN = "24x7"


class Instrument(BaseModel):
    """A canonical, tradeable or observable instrument.

    ``venue`` is where the instrument trades or is computed, which is separate
    from the provider that sends its price (see ``core/providers/registry.py``).
    ``currency`` is the ISO code it is priced or settled in, or ``None`` for a
    rate or index that is not an amount of money (yields, DXY, VIX).
    ``price_precision`` is the number of decimals the venue quotes.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    asset_class: AssetClass
    quote_type: QuoteType
    timezone: str = "America/New_York"
    unit: str = Field(min_length=1)
    venue: str = Field(min_length=1)
    currency: str | None = Field(pattern=r"^[A-Z]{3}$")
    session_calendar: SessionCalendarId
    price_precision: int = Field(ge=0, le=8)
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
    venue: str,
    currency: str | None,
    price_precision: int,
    session_calendar: SessionCalendarId = SessionCalendarId.CME_GLOBEX,
    timezone: str = "America/New_York",
    **metadata: str,
) -> Instrument:
    # Every non-crypto instrument keeps the CME Globex calendar it has always
    # been judged by. Stocks, yields and cash indices get their own calendars
    # in their phase of the multi-asset plan (docs/PLAN.md §6).
    return Instrument(
        symbol=symbol,
        display_name=display_name,
        asset_class=asset_class,
        quote_type=quote_type,
        unit=unit,
        venue=venue,
        currency=currency,
        session_calendar=session_calendar,
        price_precision=price_precision,
        timezone=timezone,
        metadata=metadata,
    )


def _megacap(symbol: str, display_name: str) -> Instrument:
    return _instrument(symbol, display_name, AssetClass.EQUITY, QuoteType.PRICE, "USD",
                       venue="NASDAQ", currency="USD", price_precision=2)


_ALL: tuple[Instrument, ...] = (
    # --- Primary ---
    _instrument(
        "NQ",
        "E-mini Nasdaq-100 futures",
        AssetClass.EQUITY_INDEX_FUTURE,
        QuoteType.PRICE,
        "index points",
        venue="CME",
        currency="USD",
        price_precision=2,
    ),
    # --- Cross-asset context ---
    _instrument(
        "ES",
        "E-mini S&P 500 futures",
        AssetClass.EQUITY_INDEX_FUTURE,
        QuoteType.PRICE,
        "index points",
        venue="CME",
        currency="USD",
        price_precision=2,
    ),
    _instrument(
        "YM",
        "E-mini Dow Jones futures",
        AssetClass.EQUITY_INDEX_FUTURE,
        QuoteType.PRICE,
        "index points",
        venue="CBOT",
        currency="USD",
        price_precision=0,
    ),
    _instrument(
        "US2Y",
        "US 2-year Treasury yield",
        AssetClass.TREASURY_YIELD,
        QuoteType.YIELD,
        "percent",
        venue="OTC",
        currency=None,
        price_precision=3,
    ),
    _instrument(
        "US10Y",
        "US 10-year Treasury yield",
        AssetClass.TREASURY_YIELD,
        QuoteType.YIELD,
        "percent",
        venue="OTC",
        currency=None,
        price_precision=3,
    ),
    _instrument(
        "DXY",
        "US Dollar Index",
        AssetClass.FX_INDEX,
        QuoteType.INDEX,
        "index points",
        venue="ICE",
        currency=None,
        price_precision=3,
    ),
    _instrument(
        "VIX",
        "CBOE Volatility Index",
        AssetClass.VOLATILITY_INDEX,
        QuoteType.INDEX,
        "index points",
        venue="CBOE",
        currency=None,
        price_precision=2,
    ),
    _instrument(
        "VXN",
        "CBOE Nasdaq-100 Volatility Index",
        AssetClass.VOLATILITY_INDEX,
        QuoteType.INDEX,
        "index points",
        venue="CBOE",
        currency=None,
        price_precision=2,
    ),
    _instrument(
        "WTI",
        "West Texas Intermediate crude oil",
        AssetClass.COMMODITY,
        QuoteType.PRICE,
        "USD per barrel",
        venue="NYMEX",
        currency="USD",
        price_precision=2,
    ),
    _instrument(
        "GOLD",
        "Gold futures",
        AssetClass.COMMODITY,
        QuoteType.PRICE,
        "USD per troy ounce",
        venue="COMEX",
        currency="USD",
        price_precision=1,
    ),
    _instrument(
        "BTC",
        "Bitcoin",
        AssetClass.CRYPTO,
        QuoteType.PRICE,
        "USD",
        venue="BINANCE",
        currency="USD",
        price_precision=2,
        session_calendar=SessionCalendarId.ALWAYS_OPEN,
        timezone="UTC",
    ),
    # --- Technology context (all eight list on Nasdaq) ---
    _megacap("NVDA", "NVIDIA"),
    _megacap("MSFT", "Microsoft"),
    _megacap("AAPL", "Apple"),
    _megacap("AMZN", "Amazon"),
    _megacap("META", "Meta Platforms"),
    _megacap("GOOGL", "Alphabet"),
    _megacap("AVGO", "Broadcom"),
    _megacap("TSLA", "Tesla"),
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
