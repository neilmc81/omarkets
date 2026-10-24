"""Mega-cap earnings dates from the public Nasdaq earnings calendar.

Only the companies that move the Nasdaq 100 most are kept. Payloads are
validated here; nothing Nasdaq-specific leaves this module.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

NEW_YORK = ZoneInfo("America/New_York")
URL = "https://api.nasdaq.com/api/calendar/earnings"

#: Largest Nasdaq-100 weights plus the chip names that drive tech sentiment.
MEGA_CAPS: dict[str, str] = {
    "NVDA": "Nvidia", "MSFT": "Microsoft", "AAPL": "Apple", "AMZN": "Amazon",
    "META": "Meta", "GOOGL": "Alphabet (Google)", "AVGO": "Broadcom", "TSLA": "Tesla",
    "NFLX": "Netflix", "COST": "Costco", "AMD": "AMD", "ASML": "ASML", "TSM": "TSMC",
}

Timing = Literal["before_open", "after_close", "unknown"]


class EarningsDate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    company: str
    day: date
    timing: Timing
    eps_forecast: float | None = None
    fiscal_quarter: str | None = None

    @property
    def expected_at(self) -> datetime:
        """Typical report time in UTC (07:00 ET before open, 16:05 ET after close)."""
        clock = time(7, 0) if self.timing == "before_open" else time(16, 5)
        return datetime.combine(self.day, clock, tzinfo=NEW_YORK).astimezone(UTC)

    @property
    def when_text(self) -> str:
        return {"before_open": "before the open", "after_close": "after the close",
                "unknown": "time not announced"}[self.timing]


_MONEY = re.compile(r"^\(?\$?(-?[\d,]*\.?\d+)\)?$")


def _eps(text: object) -> float | None:
    if not isinstance(text, str):
        return None
    match = _MONEY.match(text.strip())
    if not match:
        return None
    value = float(match.group(1).replace(",", ""))
    return -value if text.strip().startswith("(") else value


def parse(payload: object, day: date) -> list[EarningsDate]:
    """Validate one day's payload and keep only mega-cap rows."""
    if not isinstance(payload, dict):
        return []
    rows = ((payload.get("data") or {}).get("rows")) or []
    result: list[EarningsDate] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if symbol not in MEGA_CAPS:
            continue
        raw_time = str(row.get("time") or "")
        timing: Timing = ("before_open" if "pre-market" in raw_time
                          else "after_close" if "after-hours" in raw_time else "unknown")
        result.append(EarningsDate(
            symbol=symbol, company=MEGA_CAPS[symbol], day=day, timing=timing,
            eps_forecast=_eps(row.get("epsForecast")),
            fiscal_quarter=str(row.get("fiscalQuarterEnding") or "") or None,
        ))
    return result


class NasdaqEarningsCalendar:
    """Caches each day's mega-cap earnings; refreshes a day after ``ttl``."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        ttl: timedelta = timedelta(hours=6),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self._ttl = ttl
        self._clock = clock
        self._cache: dict[date, tuple[datetime, list[EarningsDate]]] = {}
        self.last_error: str | None = None

    async def _fetch(self, day: date) -> list[EarningsDate]:
        client = self._client or httpx.AsyncClient(timeout=15.0)
        try:
            response = await client.get(
                URL, params={"date": day.isoformat()},
                headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Omarkets",
                         "Accept": "application/json"},
            )
            response.raise_for_status()
            return parse(response.json(), day)
        finally:
            if self._client is None:
                await client.aclose()

    async def day(self, day: date) -> list[EarningsDate]:
        now = self._clock()
        cached = self._cache.get(day)
        if cached is not None and now - cached[0] < self._ttl:
            return cached[1]
        try:
            rows = await self._fetch(day)
        except Exception as exc:  # noqa: BLE001 - provider isolation
            self.last_error = f"earnings calendar unavailable ({type(exc).__name__})"
            logger.warning("nasdaq earnings fetch failed for %s: %s", day, type(exc).__name__)
            return cached[1] if cached else []
        self.last_error = None
        self._cache[day] = (now, rows)
        return rows

    async def between(self, start: date, end: date) -> list[EarningsDate]:
        """Weekday earnings from ``start`` to ``end`` inclusive."""
        result: list[EarningsDate] = []
        day = start
        while day <= end:
            if day.weekday() < 5:
                result.extend(await self.day(day))
            day += timedelta(days=1)
        return result
