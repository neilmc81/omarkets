"""Current Census retail headline from the official Census data API.

www.census.gov sits behind a Cloudflare browser challenge (HTTP 403 to every
non-browser client since at least Oct 2026), so the headline comes from the
Economic Indicators time series instead: ``timeseries/eits/marts``, which needs
a free Census API key. The API publishes no release timestamp, so none is
invented: ``released_at`` stays null.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from core.domain.economics import EconomicEvent
from core.providers.economic_base import EconomicBatch
from core.providers.economic_calendars import stable_id
from core.providers.economic_http import HttpEconomicProvider, sensitive_http_logs

API_URL = "https://api.census.gov/data/timeseries/eits/marts"
SOURCE_URL = "https://www.census.gov/retail/sales.html"
NY = ZoneInfo("America/New_York")
#: Total retail and food services, seasonally adjusted, monthly percent change.
HEADLINE = {"category_code": "44X72", "data_type_code": "MPCSM", "seasonally_adj": "yes"}
_PERIOD = re.compile(r"(\d{4})-(\d{2})")


def query(now: datetime) -> dict[str, str]:
    """Headline rows for roughly the last year (one small response)."""
    local = now.astimezone(NY)
    return {
        "get": "cell_value,data_type_code,category_code,seasonally_adj,error_data",
        **HEADLINE,
        "time": f"from {local.year - 1:04d}-{local.month:02d}",
    }


def parse_series(body: bytes, now: datetime) -> EconomicEvent:
    """Latest month of the national headline; anything unexpected fails closed."""
    table = json.loads(body)
    if not isinstance(table, list) or len(table) < 2 or not isinstance(table[0], list):
        raise ValueError("Census API table unavailable")
    header = table[0]
    columns = ("cell_value", "error_data", "time", *HEADLINE)
    if any(column not in header for column in columns):
        raise ValueError("Census API columns unavailable")
    index = {column: header.index(column) for column in columns}
    local = now.astimezone(NY)
    current = f"{local.year:04d}-{local.month:02d}"
    rows: dict[str, float] = {}
    for row in table[1:]:
        if not isinstance(row, list) or len(row) != len(header):
            raise ValueError("Census API row malformed")
        if any(row[index[key]] != value for key, value in HEADLINE.items()):
            raise ValueError("Census API returned a different series")
        period = row[index["time"]]
        if not isinstance(period, str) or not _PERIOD.fullmatch(period):
            raise ValueError("Census API period malformed")
        if period >= current:
            raise ValueError("Census API period is not yet complete")
        if row[index["error_data"]] != "no" or period in rows:
            raise ValueError("Census API headline row ambiguous")
        value = float(row[index["cell_value"]])
        if not math.isfinite(value):
            raise ValueError("Census API value not finite")
        rows[period] = value
    period = max(rows)
    actual = rows[period]
    identity = f"{period}:RETAIL_SALES"
    return EconomicEvent(
        id=stable_id("census_retail", identity),
        event_code="RETAIL_SALES",
        name="Retail Sales m/m",
        category="growth",
        importance="high",
        unit="percent",
        frequency="monthly",
        period=period,
        status="released",
        actual=actual,
        actual_basis="source_release",
        provider="census_retail",
        source_event_id=identity,
        source_url=SOURCE_URL,
        received_at=now,
        source_revision=hashlib.sha256(f"{identity}:{actual!r}".encode()).hexdigest(),
        mode="live",
        notes=[
            "Census national seasonally adjusted retail and food services monthly change "
            "(Census API, time series eits/marts, category 44X72).",
            "Census API publishes no release timestamp; publication time unavailable.",
            "Current issuer value; original advance-release vintage is not guaranteed.",
            "Core/ex-auto retail sales is a distinct metric, never supplied by this headline.",
        ],
    )


class CensusRetailProvider(HttpEconomicProvider):
    provider_name = "census_retail"

    def __init__(self, *args, api_key: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._api_key = api_key

    async def request(self, method: str, url: str, **kwargs) -> bytes:
        with sensitive_http_logs():  # the key travels in the query string
            return await super().request(method, url, **kwargs)

    async def aclose(self) -> None:
        with sensitive_http_logs():
            await super().aclose()

    async def collect(self, now: datetime) -> EconomicBatch:
        if not self._api_key:
            return EconomicBatch(
                provider=self.name,
                checked_at=now,
                status="not_configured",
                issues=[
                    "Census API key not configured (OMARKETS_CENSUS_API_KEY); no request sent."
                ],
            )
        body = await self.request("GET", API_URL, params={**query(now), "key": self._api_key})
        event = parse_series(body, now)
        return EconomicBatch(provider=self.name, checked_at=now, events=[event], status="healthy")
