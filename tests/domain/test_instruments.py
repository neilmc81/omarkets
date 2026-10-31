"""Tests for the canonical instrument universe.

The universe is a fixed Phase 1 contract: exactly 18 symbols, with the primary
instrument NQ, correct quote types/units for yields and volatility indices, and
a staleness default for every asset class in use.
"""

from __future__ import annotations

import pytest

from core.domain.instruments import (
    DEFAULT_STALE_SECONDS,
    INSTRUMENTS,
    PRIMARY_SYMBOL,
    AssetClass,
    QuoteType,
    SessionCalendarId,
    all_symbols,
    get_instrument,
    normalize_symbol,
)

REQUIRED_SYMBOLS = (
    "NQ",
    "ES",
    "YM",
    "US2Y",
    "US10Y",
    "DXY",
    "VIX",
    "VXN",
    "WTI",
    "GOLD",
    "BTC",
    "NVDA",
    "MSFT",
    "AAPL",
    "AMZN",
    "META",
    "GOOGL",
    "AVGO",
    "TSLA",
)


def test_exactly_the_required_symbols_exist() -> None:
    assert set(all_symbols()) == set(REQUIRED_SYMBOLS)
    assert len(all_symbols()) == 19
    assert set(INSTRUMENTS) == set(REQUIRED_SYMBOLS)


def test_all_symbols_are_unique() -> None:
    symbols = all_symbols()
    assert len(symbols) == len(set(symbols))


def test_nq_is_primary_and_present() -> None:
    assert PRIMARY_SYMBOL == "NQ"
    assert PRIMARY_SYMBOL in INSTRUMENTS
    nq = get_instrument("NQ")
    assert nq is not None
    assert nq.asset_class is AssetClass.EQUITY_INDEX_FUTURE


@pytest.mark.parametrize("symbol", ["US2Y", "US10Y"])
def test_treasury_yields_have_yield_quote_type_and_percent_unit(symbol: str) -> None:
    instrument = get_instrument(symbol)
    assert instrument is not None
    assert instrument.quote_type is QuoteType.YIELD
    assert instrument.unit == "percent"
    assert instrument.asset_class is AssetClass.TREASURY_YIELD


@pytest.mark.parametrize("symbol", ["VIX", "VXN"])
def test_volatility_indices_are_volatility_index_asset_class(symbol: str) -> None:
    instrument = get_instrument(symbol)
    assert instrument is not None
    assert instrument.asset_class is AssetClass.VOLATILITY_INDEX


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("nq", "NQ"),
        ("  nq  ", "NQ"),
        ("Nq", "NQ"),
        ("us2y", "US2Y"),
        ("  Us10Y ", "US10Y"),
        ("\tVIX\n", "VIX"),
    ],
)
def test_normalize_symbol_is_case_and_whitespace_insensitive(raw: str, expected: str) -> None:
    assert normalize_symbol(raw) == expected


def test_get_instrument_returns_none_for_unknown() -> None:
    assert get_instrument("ZZZ") is None
    assert get_instrument("") is None
    assert get_instrument("   ") is None


def test_get_instrument_normalizes_lookup() -> None:
    assert get_instrument("  nq ") is get_instrument("NQ")


def test_every_instrument_has_a_staleness_default_for_its_asset_class() -> None:
    for symbol, instrument in INSTRUMENTS.items():
        assert instrument.asset_class in DEFAULT_STALE_SECONDS, symbol
        assert DEFAULT_STALE_SECONDS[instrument.asset_class] > 0, symbol


def test_every_instrument_has_non_empty_display_name_and_unit() -> None:
    for symbol, instrument in INSTRUMENTS.items():
        assert instrument.display_name, symbol
        assert instrument.unit, symbol
        assert instrument.symbol == symbol


def test_btc_uses_utc_timezone() -> None:
    btc = get_instrument("BTC")
    assert btc is not None
    assert btc.timezone == "UTC"


def test_asset_classes_include_spot_fx_and_cash_indices() -> None:
    assert AssetClass.FX_SPOT.value == "fx_spot"
    assert AssetClass.EQUITY_INDEX.value == "equity_index"
    # DXY stays a currency index, not a pair.
    assert INSTRUMENTS["DXY"].asset_class is AssetClass.FX_INDEX


def test_every_instrument_declares_venue_currency_calendar_and_precision() -> None:
    venues = {symbol: instrument.venue for symbol, instrument in INSTRUMENTS.items()}
    assert venues["NQ"] == venues["ES"] == "CME"
    assert venues["YM"] == "CBOT"
    assert venues["BTC"] == "BINANCE"
    assert venues["NVDA"] == "NASDAQ"
    for symbol, instrument in INSTRUMENTS.items():
        assert 0 <= instrument.price_precision <= 8, symbol
        expected = (SessionCalendarId.ALWAYS_OPEN if symbol == "BTC"
                    else SessionCalendarId.CME_GLOBEX)
        assert instrument.session_calendar is expected, symbol
    # Rates and indices are not amounts of money; prices are.
    for symbol in ("US2Y", "US10Y", "DXY", "VIX", "VXN"):
        assert INSTRUMENTS[symbol].currency is None, symbol
    for symbol in ("NQ", "ES", "YM", "WTI", "GOLD", "BTC", "AAPL"):
        assert INSTRUMENTS[symbol].currency == "USD", symbol
    assert INSTRUMENTS["US2Y"].price_precision == 3
    # Dow futures trade in whole index points.
    assert INSTRUMENTS["YM"].price_precision == 0


def test_currency_must_be_an_iso_code() -> None:
    from pydantic import ValidationError

    from core.domain.instruments import Instrument

    nq = INSTRUMENTS["NQ"].model_dump()
    with pytest.raises(ValidationError):
        Instrument(**{**nq, "currency": "usd"})
