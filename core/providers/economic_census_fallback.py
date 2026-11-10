"""FRED fallback for the Census feeds, which Cloudflare has blocked since Nov 2, 2026.

Census stays first; FRED is asked only when the Census request or its parse fails.
FRED serves levels, so month-over-month series are converted to the percent change
Census publishes (one decimal), and new home sales to units, so actuals and
surprises are unchanged. Nothing here logs or returns a URL or key.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from typing import NamedTuple
from zoneinfo import ZoneInfo

from core.providers.economic_base import EconomicProviderError

NY = ZoneInfo("America/New_York")
BLOCKED_HTTP = "upstream blocked (HTTP 403)"
BLOCKED_BODY = "upstream blocked (non-JSON response)"
FRED_FALLBACK_NOTE = "using FRED fallback (Census unavailable)"
FRED_NOT_CONFIGURED = "FRED fallback not configured (OMARKETS_FRED_API_KEY)"
#: A FRED copy a day after the usual release is late, not pending.
RELEASE_GRACE = timedelta(days=1)


class FredSeries(NamedTuple):
    series_id: str
    percent_change: bool  # FRED level -> m/m percent, as Census publishes
    scale: float
    due_day: int  # latest usual day of the month the release lands


FRED_SERIES: dict[str, FredSeries] = {
    "RETAIL_SALES": FredSeries("RSAFS", True, 1.0, 17),
    "DURABLE_GOODS": FredSeries("DGORDER", True, 1.0, 28),
    "CORE_DURABLE_GOODS": FredSeries("ADXTNO", True, 1.0, 28),
    "NEW_HOME_SALES": FredSeries("HSN1F", False, 1000.0, 26),
}


class FredLag(Exception):
    """FRED has not caught up with the expected release; ``pending`` is inside grace."""

    def __init__(self, pending: bool):
        super().__init__("FRED not yet updated")
        self.pending = pending


def blocked_detail(exc: BaseException) -> str:
    """Operator-facing reason for a failed Census request; no URL, no key."""
    if isinstance(exc, EconomicProviderError) and exc.status_code == 403:
        return BLOCKED_HTTP
    if isinstance(exc, json.JSONDecodeError | UnicodeDecodeError):
        return BLOCKED_BODY
    return "request or validation failed"


def fred_params(code: str, key: str) -> dict[str, str]:
    return {
        "api_key": key,
        "series_id": FRED_SERIES[code].series_id,
        "file_type": "json",
        "sort_order": "desc",
        "limit": "14",
    }


def _previous_month(period: str) -> str:
    year, month = int(period[:4]), int(period[5:])
    return f"{year - 1:04d}-12" if month == 1 else f"{year:04d}-{month - 1:02d}"


def parse_fred(code: str, body: bytes, now: datetime) -> tuple[str, float, float | None]:
    """``(period, actual, previous)`` for the latest complete month, in Census terms."""
    spec = FRED_SERIES[code]
    local = now.astimezone(NY)
    current = f"{local.year:04d}-{local.month:02d}"
    levels: dict[str, float] = {}
    for row in json.loads(body)["observations"]:
        if row["value"] == ".":
            continue
        period = str(row["date"])[:7]
        value = float(row["value"])
        if period >= current or not math.isfinite(value) or period in levels:
            continue
        levels[period] = value
    if not levels:
        raise ValueError("FRED observations unavailable")

    def measure(period: str) -> float | None:
        if not spec.percent_change:
            return round(levels[period] * spec.scale, 6)
        prior = levels.get(_previous_month(period))
        if prior is None or prior == 0:
            return None
        return round((levels[period] / prior - 1) * 100, 1)

    latest = max(levels)
    actual = measure(latest)
    if actual is None:
        raise ValueError("FRED history too short")
    previous_period = _previous_month(latest)
    previous = measure(previous_period) if previous_period in levels else None
    if code != "RETAIL_SALES" and previous is None:
        raise ValueError("FRED history too short")
    expected = _previous_month(current)
    if latest < expected:
        due = datetime(local.year, local.month, spec.due_day, 10, 0, tzinfo=NY)
        raise FredLag(pending=local <= due + RELEASE_GRACE)
    return latest, actual, previous
