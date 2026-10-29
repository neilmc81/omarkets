"""Durable goods and new home sales headlines from the official Census data API.

Like ``census_retail`` (and with the same free key), the API publishes no
release time, so ``released_at`` stays null. The calendar only pairs a value
with a release scheduled in the month after its reference month.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from typing import NamedTuple
from zoneinfo import ZoneInfo

from core.domain.economics import EconomicEvent
from core.providers.economic_base import EconomicBatch
from core.providers.economic_calendars import stable_id
from core.providers.economic_http import HttpEconomicProvider, sensitive_http_logs

API_URL = "https://api.census.gov/data/timeseries/eits/{dataset}"
PROVIDER = "census_releases"
NY = ZoneInfo("America/New_York")
_PERIOD = re.compile(r"\d{4}-(0[1-9]|1[0-2])")


class Series(NamedTuple):
    code: str
    name: str
    dataset: str
    filters: dict[str, str]
    unit: str
    scale: float  # Census value to calendar value
    source_url: str


SERIES: tuple[Series, ...] = (
    Series(
        "DURABLE_GOODS",
        "Durable Goods Orders m/m",
        "advm3",
        {"data_type_code": "MPCNO", "category_code": "MDM"},
        "percent",
        1.0,
        "https://www.census.gov/manufacturing/m3/adv/current/index.html",
    ),
    Series(
        "CORE_DURABLE_GOODS",
        "Core Durable Goods Orders m/m",
        "advm3",
        {"data_type_code": "MPCNO", "category_code": "DXT"},
        "percent",
        1.0,
        "https://www.census.gov/manufacturing/m3/adv/current/index.html",
    ),
    Series(
        "NEW_HOME_SALES",
        "New Home Sales",
        "ressales",
        {"data_type_code": "TOTAL", "category_code": "ASOLD", "geo_level_code": "US"},
        "value",
        1000.0,  # thousands of units, seasonally adjusted annual rate
        "https://www.census.gov/construction/nrs/current/index.html",
    ),
)
_COLUMNS = ("cell_value", "seasonally_adj", "error_data", "time_slot_id", "time")


def query(series: Series, now: datetime) -> dict[str, str]:
    local = now.astimezone(NY)
    params = {
        "get": ",".join((*_COLUMNS[:4], *series.filters)),
        "time": f"from {local.year - 1:04d}-{local.month:02d}",
        "seasonally_adj": "yes",
        **{k: v for k, v in series.filters.items() if k != "geo_level_code"},
    }
    if series.dataset == "advm3":
        params["for"] = "us:*"
    return params


def parse_series(series: Series, body: bytes, now: datetime) -> EconomicEvent:
    """Latest complete month of one national headline; anything unexpected fails closed."""
    table = json.loads(body)
    if not isinstance(table, list) or len(table) < 3 or not isinstance(table[0], list):
        raise ValueError("Census API table unavailable")
    header = table[0]
    if any(column not in header for column in (*_COLUMNS, *series.filters)):
        raise ValueError("Census API columns unavailable")
    index = {column: header.index(column) for column in (*_COLUMNS, *series.filters)}
    local = now.astimezone(NY)
    current = f"{local.year:04d}-{local.month:02d}"
    rows: dict[str, float] = {}
    for row in table[1:]:
        if not isinstance(row, list) or len(row) != len(header):
            raise ValueError("Census API row malformed")
        if any(row[index[k]] != v for k, v in series.filters.items()):
            if "geo_level_code" in series.filters:
                continue  # regional rows share the query
            raise ValueError("Census API returned a different series")
        period = row[index["time"]]
        if not isinstance(period, str) or not _PERIOD.fullmatch(period) or period >= current:
            raise ValueError("Census API period malformed or incomplete")
        if row[index["seasonally_adj"]] != "yes" or row[index["error_data"]] != "no":
            raise ValueError("Census API row is not the seasonally adjusted headline")
        if period in rows:
            raise ValueError("Census API headline row ambiguous")
        value = float(row[index["cell_value"]]) * series.scale
        if not math.isfinite(value):
            raise ValueError("Census API value not finite")
        rows[period] = value
    ordered = sorted(rows)
    if len(ordered) < 2:
        raise ValueError("Census API history too short")
    period, actual = ordered[-1], rows[ordered[-1]]
    identity = f"{period}:{series.code}"
    return EconomicEvent(
        id=stable_id(PROVIDER, identity),
        event_code=series.code,
        name=series.name,
        category="housing" if series.code == "NEW_HOME_SALES" else "growth",
        importance="medium",
        unit=series.unit,
        frequency="monthly",
        period=period,
        status="released",
        actual=actual,
        previous=rows[ordered[-2]],
        actual_basis="source_release",
        provider=PROVIDER,
        source_event_id=identity,
        source_url=series.source_url,
        received_at=now,
        source_revision=hashlib.sha256(f"{identity}:{actual!r}".encode()).hexdigest(),
        mode="live",
        notes=[
            f"Census API time series eits/{series.dataset} "
            f"({', '.join(f'{k}={v}' for k, v in series.filters.items())}, SA).",
            "Census API publishes no release timestamp; publication time unavailable.",
        ],
    )


class CensusReleasesProvider(HttpEconomicProvider):
    provider_name = PROVIDER

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
        events, issues = [], []
        for series in SERIES:
            try:
                body = await self.request(
                    "GET",
                    API_URL.format(dataset=series.dataset),
                    params={**query(series, now), "key": self._api_key},
                )
                events.append(parse_series(series, body, now))
            except Exception:
                issues.append(f"{series.code}: request or validation failed")
        return EconomicBatch(
            provider=self.name,
            checked_at=now,
            events=events,
            issues=issues,
            status="degraded" if issues else "healthy",
        )
