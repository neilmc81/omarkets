"""Current BEA headline actuals (GDP, GDP prices, PCE prices) from the BEA data API.

The API needs a free key (apps.bea.gov/api/signup). It publishes no release
time, only each table's ``LastRevised`` date, so ``released_at`` stays null and
the revision date travels in ``source_event_id`` (``CODE:period:YYYY-MM-DD``).
The calendar only pairs a value with a scheduled release on that same date.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, datetime
from typing import NamedTuple

from core.domain.economics import EconomicEvent
from core.providers.economic_base import EconomicBatch
from core.providers.economic_calendars import stable_id
from core.providers.economic_http import HttpEconomicProvider, sensitive_http_logs

API_URL = "https://apps.bea.gov/api/data/"
SOURCE_URL = "https://www.bea.gov/data"
PROVIDER = "bea_data"
_REVISED = re.compile(r"LastRevised:\s*([A-Z][a-z]+ \d{1,2}, \d{4})")
_PERIOD = {"Q": re.compile(r"\d{4}Q[1-4]"), "M": re.compile(r"\d{4}M(0[1-9]|1[0-2])")}


class Metric(NamedTuple):
    code: str
    name: str
    table: str
    line: str
    description: str
    frequency: str  # BEA frequency letter


METRICS: tuple[Metric, ...] = (
    Metric("GDP", "Real GDP q/q (annualized)", "T10101", "1", "Gross domestic product", "Q"),
    Metric("GDP_PRICE_INDEX", "GDP Price Index q/q", "T10107", "1", "Gross domestic product", "Q"),
    Metric(
        "PCE", "PCE Price Index m/m", "T20807", "1", "Personal consumption expenditures (PCE)", "M"
    ),
    Metric(
        "CORE_PCE", "Core PCE Price Index m/m", "T20807", "25", "PCE excluding food and energy", "M"
    ),
)


def _period(raw: str, frequency: str) -> str:
    if frequency == "Q":
        return f"{raw[:4]}-Q{raw[5]}"
    return f"{raw[:4]}-{raw[5:]}"


def parse_table(body: bytes, metrics: list[Metric], now: datetime) -> list[EconomicEvent]:
    """Latest period of each metric in one table; anything unexpected fails closed."""
    payload = json.loads(body)["BEAAPI"]
    if "Error" in payload or "Error" in payload.get("Results", {}):
        raise ValueError("BEA API returned an error")
    results = payload["Results"]
    table = metrics[0].table
    notes = [n for n in results.get("Notes", []) if n.get("NoteRef") == table]
    found = [m for n in notes if (m := _REVISED.search(n.get("NoteText", "")))]
    if len(found) != 1:
        raise ValueError("BEA table revision date unavailable")
    revised = datetime.strptime(found[0].group(1), "%B %d, %Y").date()
    if revised > now.date():
        raise ValueError("BEA revision date in the future")
    events = []
    for metric in metrics:
        rows: dict[str, float] = {}
        for row in results["Data"]:
            if row.get("TableName") != table or row.get("LineNumber") != metric.line:
                continue
            if row.get("LineDescription") != metric.description:
                raise ValueError("BEA line description changed")
            raw = row.get("TimePeriod", "")
            if not _PERIOD[metric.frequency].fullmatch(raw) or raw in rows:
                raise ValueError("BEA period malformed or duplicated")
            value = float(str(row["DataValue"]).replace(",", ""))
            if not math.isfinite(value):
                raise ValueError("BEA value not finite")
            rows[raw] = value
        if not rows:
            raise ValueError("BEA metric missing")
        ordered = sorted(rows)
        latest = ordered[-1]
        period = _period(latest, metric.frequency)
        actual = rows[latest]
        prior = rows[ordered[-2]] if len(ordered) > 1 else None
        identity = f"{metric.code}:{period}:{revised.isoformat()}"
        events.append(
            EconomicEvent(
                id=stable_id(PROVIDER, identity),
                event_code=metric.code,
                name=metric.name,
                category="growth" if metric.code == "GDP" else "inflation",
                importance="critical",
                unit="percent",
                frequency="quarterly" if metric.frequency == "Q" else "monthly",
                period=period,
                status="released",
                actual=actual,
                previous=prior,
                actual_basis="source_release",
                provider=PROVIDER,
                source_event_id=identity,
                source_url=SOURCE_URL,
                received_at=now,
                source_revision=hashlib.sha256(f"{identity}:{actual!r}".encode()).hexdigest(),
                mode="live",
                notes=[
                    f"BEA NIPA table {table} line {metric.line} ({metric.description}).",
                    f"BEA table last revised {revised.isoformat()}; "
                    "the API publishes no release time.",
                ],
            )
        )
    return events


def revised_on(event: EconomicEvent) -> date | None:
    """The BEA revision date carried in ``source_event_id``."""
    try:
        return date.fromisoformat((event.source_event_id or "").rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return None


class BEADataProvider(HttpEconomicProvider):
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
                issues=["BEA API key not configured (OMARKETS_BEA_API_KEY); no request sent."],
            )
        tables: dict[str, list[Metric]] = {}
        for metric in METRICS:
            tables.setdefault(metric.table, []).append(metric)
        events, issues = [], []
        for table, metrics in tables.items():
            try:
                body = await self.request(
                    "GET",
                    API_URL,
                    params={
                        "UserID": self._api_key,
                        "method": "GetData",
                        "DataSetName": "NIPA",
                        "TableName": table,
                        "Frequency": metrics[0].frequency,
                        "Year": f"{now.year - 1},{now.year}",
                        "ResultFormat": "JSON",
                    },
                )
                events.extend(parse_table(body, metrics, now))
            except Exception:
                issues.append(f"{table}: request or validation failed")
        return EconomicBatch(
            provider=self.name,
            checked_at=now,
            events=events,
            issues=issues,
            status="degraded" if issues else "healthy",
        )
