"""Futures contract selection.

Omarkets treats ``NQ``, ``ES`` and ``WTI`` as canonical instrument identifiers,
not as permanent contracts. A real feed quotes a specific contract month, so
this module resolves the active front month deterministically and exposes the
contract metadata without exposing provider symbols elsewhere.

The roll rule is deliberately simple and transparent:

* CME equity-index futures expire on the third Friday of H, M, U, Z months,
* NYMEX WTI crude (``CL``) lists every month; a contract stops trading three
  business days before the 25th of the month before its contract month (four
  if the 25th is not a business day). Business days use the exchange holiday
  calendar in :mod:`core.domain.market_calendar`,
* the front contract rolls to the next one ``roll_days_before_expiry``
  calendar days before that expiry.

This is a display/selection rule, not a back-adjusted continuous series. It does
not attempt intraday roll timing.
"""

from __future__ import annotations

from datetime import date, timedelta

from pydantic import BaseModel, ConfigDict, Field

from core.domain.market_calendar import is_trading_day

CODE_TO_MONTH = {
    "F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
    "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12,
}
MONTH_TO_CODE = {value: key for key, value in CODE_TO_MONTH.items()}
QUARTER_MONTHS = (3, 6, 9, 12)
ALL_MONTHS = tuple(range(1, 13))
#: Roots that list every month and follow the NYMEX crude-oil expiry rule.
CRUDE_ROOTS = frozenset({"CL"})


def _third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    offset = (4 - first.weekday()) % 7
    return first + timedelta(days=offset + 14)


def _business_days_before(day: date, count: int) -> date:
    while count:
        day -= timedelta(days=1)
        if is_trading_day(day):
            count -= 1
    return day


def _crude_last_trade(year: int, month: int) -> date:
    """Last trading day of the CL contract for ``year``/``month``."""
    prior = date(year, month, 1) - timedelta(days=1)
    the_25th = prior.replace(day=25)
    return _business_days_before(the_25th, 3 if is_trading_day(the_25th) else 4)


class FuturesContract(BaseModel):
    """One concrete futures contract for a canonical root symbol."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    root: str = Field(min_length=1)
    code: str = Field(min_length=1)
    expiry: date
    roll_days_before_expiry: int = Field(default=8, ge=0)

    @property
    def contract_month(self) -> str:
        """The contract (delivery) month; a CL contract expires the month before."""
        month = self.provider_month
        return f"20{month[1:]}-{CODE_TO_MONTH[month[0]]:02d}"

    @property
    def provider_month(self) -> str:
        """Month portion used by quote providers, e.g. ``Z26``."""
        return self.code[len(self.root):]

    def provider_symbol(self, prefix: str = "") -> str:
        return f"{prefix}{self.root}{self.provider_month}"


def _contracts_for_year(root: str, year: int, roll_days: int) -> list[FuturesContract]:
    crude = root in CRUDE_ROOTS
    return [
        FuturesContract(
            root=root,
            code=f"{root}{MONTH_TO_CODE[month]}{str(year)[2:]}",
            expiry=_crude_last_trade(year, month) if crude else _third_friday(year, month),
            roll_days_before_expiry=roll_days,
        )
        for month in (ALL_MONTHS if crude else QUARTER_MONTHS)
    ]


def active_contract(root: str, when: date, *, roll_days_before_expiry: int = 8) -> FuturesContract:
    """Return the front contract for ``root`` on ``when``.

    The active contract is the earliest contract whose roll date has not yet
    passed; if all contracts for a year have rolled, the first contract of the
    following year is returned.
    """
    candidates: list[FuturesContract] = []
    for year in (when.year, when.year + 1):
        candidates.extend(_contracts_for_year(root, year, roll_days_before_expiry))
    for contract in sorted(candidates, key=lambda item: item.expiry):
        if when < contract.expiry - timedelta(days=roll_days_before_expiry):
            return contract
    return candidates[-1]  # pragma: no cover - next year always provides a future contract


def contract_for_symbol(
    symbol: str, when: date, *, roots: dict[str, str] | None = None, roll_days: int = 8
) -> FuturesContract | None:
    """Resolve a contract for a canonical symbol, or ``None`` if not a future."""
    mapping = roots if roots is not None else DEFAULT_FUTURES_ROOTS
    if symbol not in mapping:
        return None
    return active_contract(mapping[symbol], when, roll_days_before_expiry=roll_days)


#: Canonical symbol -> futures root. ``WTI`` is quoted from the ``CL`` contract.
DEFAULT_FUTURES_ROOTS: dict[str, str] = {"NQ": "NQ", "ES": "ES", "WTI": "CL"}
