"""US Treasury daily par yield curves (nominal and real) from home.treasury.gov.

Endpoints (public XML, no key), one month per request:

* ``?data=daily_treasury_real_yield_curve&field_tdr_date_value_month=YYYYMM`` —
  real (TIPS) par yields, ``TC_5YEAR``/``TC_10YEAR``.
* ``?data=daily_treasury_yield_curve&field_tdr_date_value_month=YYYYMM`` —
  nominal par yields, ``BC_5YEAR``/``BC_10YEAR``.

Treasury publishes each business day's curve after the close (usually the same
evening ET). Payloads are untrusted: every value is validated here and anything
malformed is dropped. Nothing Treasury-specific leaves this module.
"""

from __future__ import annotations

import asyncio
import logging
import math
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

import httpx
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

URL = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"
REAL = "daily_treasury_real_yield_curve"
NOMINAL = "daily_treasury_yield_curve"
_D = "{http://schemas.microsoft.com/ado/2007/08/dataservices}"
#: Field names per curve, by maturity in years.
FIELDS = {REAL: {5: "TC_5YEAR", 10: "TC_10YEAR"}, NOMINAL: {5: "BC_5YEAR", 10: "BC_10YEAR"}}


class DailyYields(BaseModel):
    """One business day's 5- and 10-year yields (percent); ``None`` when not published."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    day: date
    real_5y: float | None = Field(default=None, gt=-10, lt=20)
    real_10y: float | None = Field(default=None, gt=-10, lt=20)
    nominal_5y: float | None = Field(default=None, gt=-10, lt=30)
    nominal_10y: float | None = Field(default=None, gt=-10, lt=30)


def _number(text: str | None) -> float | None:
    if text is None or not text.strip():
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def parse(xml: str | bytes, curve: str) -> dict[date, dict[int, float]]:
    """Day → {years: yield} for one curve's month; malformed rows are dropped."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return {}
    out: dict[date, dict[int, float]] = {}
    for props in root.iter("{http://schemas.microsoft.com/ado/2007/08/dataservices/metadata}"
                           "properties"):
        raw = props.findtext(f"{_D}NEW_DATE")
        try:
            day = datetime.strptime((raw or "")[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        values = {years: v for years, name in FIELDS[curve].items()
                  if (v := _number(props.findtext(f"{_D}{name}"))) is not None
                  and -10 < v < 30}
        if values:
            out[day] = values
    return out


def combine(real: dict[date, dict[int, float]],
            nominal: dict[date, dict[int, float]]) -> list[DailyYields]:
    """Days with a real yield, oldest first, with that day's nominal yields if any."""
    rows = []
    for day in sorted(real):
        r, n = real[day], nominal.get(day, {})
        try:
            rows.append(DailyYields(day=day, real_5y=r.get(5), real_10y=r.get(10),
                                    nominal_5y=n.get(5), nominal_10y=n.get(10)))
        except ValueError:
            continue
    return rows


def _months(today: date) -> list[str]:
    """This month and last month (enough for a one-week change on the 1st)."""
    last = today.replace(day=1) - timedelta(days=1)
    return [f"{last:%Y%m}", f"{today:%Y%m}"]


class TreasuryYields:
    """Caches the last two months of daily yields; refreshes every ``ttl``."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        ttl: timedelta = timedelta(hours=1),
        retry_after: timedelta = timedelta(minutes=30),
        deadline: float = 45.0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self._ttl = ttl
        self._retry_after = retry_after
        #: Whole-refresh limit in seconds (the client timeout is per operation).
        self._deadline = deadline
        self._clock = clock
        self._rows: list[DailyYields] = []
        self._next: datetime | None = None
        self._lock = asyncio.Lock()
        self.last_error: str | None = None

    @property
    def cached(self) -> list[DailyYields]:
        """The last good rows, oldest first, without fetching."""
        return self._rows

    async def _get(self, client: httpx.AsyncClient, curve: str, month: str) -> str:
        response = await client.get(
            URL, params={"data": curve, "field_tdr_date_value_month": month},
            headers={"User-Agent": "Omarkets (local market desk)",
                     "Accept": "application/xml"})
        response.raise_for_status()
        return response.text

    async def rows(self) -> list[DailyYields]:
        """Daily yields over about the last two months, oldest first. On a failed
        fetch the last good rows are kept and retried after ``retry_after``."""
        async with self._lock:
            now = self._clock()
            if self._next is not None and now < self._next:
                return self._rows
            client = self._client or httpx.AsyncClient(timeout=15.0)
            try:
                months = _months(now.date())
                async with asyncio.timeout(self._deadline):
                    texts = await asyncio.gather(*(self._get(client, curve, month)
                                                   for curve in (REAL, NOMINAL)
                                                   for month in months))
            except Exception as exc:  # noqa: BLE001 - provider isolation
                self.last_error = f"Treasury yields unavailable ({type(exc).__name__})"
                logger.warning("treasury yields fetch failed: %s", type(exc).__name__)
                self._next = now + self._retry_after
                return self._rows
            finally:
                if self._client is None:
                    await client.aclose()
            real: dict[date, dict[int, float]] = {}
            nominal: dict[date, dict[int, float]] = {}
            for text in texts[:2]:
                real.update(parse(text, REAL))
            for text in texts[2:]:
                nominal.update(parse(text, NOMINAL))
            rows = combine(real, nominal)
            if not rows:
                # An empty or changed page is not "no yields": keep the last good rows.
                self.last_error = "Treasury yields page had no readable rows"
                logger.warning("treasury yields: no readable rows")
                self._next = now + self._retry_after
                return self._rows
            self.last_error = None
            self._rows = rows
            self._next = now + self._ttl
            return rows


__all__ = ["DailyYields", "TreasuryYields", "combine", "parse"]
