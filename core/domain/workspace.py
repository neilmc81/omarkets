"""Typed payloads for the Chart focus and the watchlist.

The focus is the one instrument the Chart page shows; the watchlist is one
named, ordered list of instruments. Both are validated against the instrument
registry (:mod:`core.domain.instruments`), and neither affects what the desk
writes: Today, the shell bar and the desk stay on NQ in Phase 1.
"""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from core.domain.instruments import get_instrument, normalize_symbol

#: The most symbols one watchlist holds (the registry has 18 instruments today).
MAX_WATCHLIST_SYMBOLS = 50


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def registry_symbol(symbol: str) -> str:
    """The canonical symbol, or ``ValueError`` when it is not in the registry."""
    canonical = normalize_symbol(symbol)
    if get_instrument(canonical) is None:
        raise ValueError(f"unknown instrument: {symbol}")
    return canonical


class Watchlist(_Frozen):
    """One named, ordered list of registry instruments (no repeats)."""

    name: str = Field(min_length=1, max_length=60)
    symbols: list[str] = Field(min_length=1, max_length=MAX_WATCHLIST_SYMBOLS)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value

    @field_validator("symbols")
    @classmethod
    def _symbols(cls, value: list[str]) -> list[str]:
        canonical = [registry_symbol(s) for s in value]
        repeats = sorted({s for s in canonical if canonical.count(s) > 1})
        if repeats:
            raise ValueError(f"listed more than once: {', '.join(repeats)}")
        return canonical


class SavedWatchlist(Watchlist):
    #: When the list was last saved; ``None`` for the default list.
    updated_at: AwareDatetime | None = None


class FocusUpdate(_Frozen):
    symbol: str = Field(min_length=1, max_length=20)

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, value: str) -> str:
        return registry_symbol(value)


class ChartInstrument(_Frozen):
    """What the Chart page needs to draw one instrument; decided by the core."""

    symbol: str
    label: str
    unit: str
    #: Decimals the venue quotes; the chart's price scale uses it.
    price_precision: int = Field(ge=0, le=8)
    #: True when ``/api/v1/levels/{symbol}`` has key levels (NQ, ES).
    levels: bool
    #: True when the release pins (measured on NQ) belong on this chart.
    release_pins: bool


class ChartInstruments(_Frozen):
    instruments: list[ChartInstrument]


class ChartFocus(_Frozen):
    symbol: str
    #: When the focus was last saved; ``None`` while it is the default (NQ).
    updated_at: AwareDatetime | None
    instrument: ChartInstrument


__all__ = [
    "MAX_WATCHLIST_SYMBOLS",
    "ChartFocus",
    "ChartInstrument",
    "ChartInstruments",
    "FocusUpdate",
    "SavedWatchlist",
    "Watchlist",
    "registry_symbol",
]
