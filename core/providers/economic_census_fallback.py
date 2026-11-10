"""FRED/ALFRED fallback for the Census feeds, which Cloudflare has blocked since Nov 2, 2026.

Census stays first; FRED is asked only when the Census request or its parse fails,
or no Census key is configured. Values come from the first-release (advance)
vintage on ALFRED, not FRED's current vintage, so a fallback actual matches the
headline Census published and does not drift after later revisions (durable
goods are revised by the full M3 report a week later). Month-over-month series
are converted to the percent change Census publishes (one decimal), new home
sales to units.

Release grace: the next month is expected at the first scheduled FRED release
date (``fred/release/dates``) at least two weeks after the latest month's first
release and after that next month has ended, at the Census release time. It is
pending until that slot plus ``RELEASE_GRACE``, then late (degraded). Grace is
tied to that specific missing month, so a month that is already overdue stays
degraded across a calendar-month rollover.

Nothing here logs or returns a URL with a key, or a key.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from typing import Literal, NamedTuple
from zoneinfo import ZoneInfo

from core.providers.economic_base import EconomicProviderError
from core.providers.economic_fred import FRED_URL

NY = ZoneInfo("America/New_York")
FRED_OBSERVATIONS_URL = FRED_URL
FRED_RELEASE_DATES_URL = "https://api.stlouisfed.org/fred/release/dates"
ALFRED_SERIES_URL = "https://alfred.stlouisfed.org/series?seid={series_id}"
CENSUS_KEY_MISSING = "Census API key not configured (OMARKETS_CENSUS_API_KEY)"
FRED_FALLBACK_NOTE = "using FRED fallback"
FRED_NOT_CONFIGURED = "FRED fallback not configured (OMARKETS_FRED_API_KEY)"
#: Room past the scheduled release for FRED to post its copy: the one-hour grace
#: ADP releases get, plus an hour for the mirror.
RELEASE_GRACE = timedelta(hours=2)
#: A new reference month never follows the previous one's first release this soon;
#: shorter gaps are revisions (M3 full report, annual retail revision).
MIN_RELEASE_GAP = timedelta(days=14)


class FredSeries(NamedTuple):
    series_id: str
    percent_change: bool  # FRED level -> m/m percent, as Census publishes
    scale: float
    release_id: int  # FRED release whose schedule carries this series
    release_time: time  # Census release time, ET


FRED_SERIES: dict[str, FredSeries] = {
    "RETAIL_SALES": FredSeries("RSAFS", True, 1.0, 9, time(8, 30)),
    "DURABLE_GOODS": FredSeries("DGORDER", True, 1.0, 95, time(8, 30)),
    "CORE_DURABLE_GOODS": FredSeries("ADXTNO", True, 1.0, 95, time(8, 30)),
    "NEW_HOME_SALES": FredSeries("HSN1F", False, 1000.0, 97, time(10, 0)),
}

Outcome = Literal["current", "pending", "late", "unscheduled", "failed", "not_configured"]


@dataclass(frozen=True)
class FallbackResult:
    """What the fallback produced for one series. ``degraded`` drives provider status."""

    outcome: Outcome
    reason: str  # why Census was not used
    period: str | None = None
    actual: float | None = None
    previous: float | None = None
    vintage: date | None = None
    source_url: str | None = None
    missing: str | None = None  # the next reference month, when not yet on FRED
    due: datetime | None = None  # its scheduled release

    @property
    def used(self) -> bool:
        return self.actual is not None

    @property
    def degraded(self) -> bool:
        return self.outcome in {"late", "unscheduled", "failed", "not_configured"}

    def detail(self) -> str:
        """Operator-facing status text after the Census reason; no URL, no key."""
        due = f" (due {self.due.astimezone(NY):%Y-%m-%d %H:%M} ET)" if self.due else ""
        return {
            "current": FRED_FALLBACK_NOTE,
            "pending": f"{FRED_FALLBACK_NOTE}; {self.missing} release pending{due}",
            "late": f"{FRED_FALLBACK_NOTE}; FRED has not published {self.missing}{due}",
            "unscheduled": f"{FRED_FALLBACK_NOTE}; {self.missing} release schedule unavailable",
            "failed": "FRED fallback failed",
            "not_configured": FRED_NOT_CONFIGURED,
        }[self.outcome]


def blocked_detail(exc: BaseException) -> str:
    """Operator-facing reason for a failed Census request; no URL, no key."""
    if isinstance(exc, EconomicProviderError) and exc.status_code == 403:
        return "Census blocked (HTTP 403)"
    if isinstance(exc, json.JSONDecodeError | UnicodeDecodeError):
        return "Census blocked (non-JSON response)"
    return "Census request or validation failed"


def _shift(period: str, months: int) -> str:
    index = int(period[:4]) * 12 + int(period[5:]) - 1 + months
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _observations(body: bytes) -> list[tuple[str, date, float]]:
    """``(period, realtime_start, value)`` rows; missing values skipped."""
    rows = []
    for row in json.loads(body)["observations"]:
        if row["value"] == ".":
            continue
        value = float(row["value"])
        if not math.isfinite(value):
            raise ValueError("FRED value not finite")
        rows.append((str(row["date"])[:7], date.fromisoformat(row["realtime_start"]), value))
    return rows


def parse_first_release(body: bytes, now: datetime) -> tuple[str, date]:
    """Latest complete month on ALFRED and the date it was first released."""
    local = now.astimezone(NY)
    current = f"{local.year:04d}-{local.month:02d}"
    rows = [(p, start) for p, start, _ in _observations(body) if p < current]
    if not rows:
        raise ValueError("FRED observations unavailable")
    return max(rows)


def parse_vintage(code: str, body: bytes, period: str) -> tuple[float, float | None]:
    """``(actual, previous)`` for ``period`` from one vintage, in Census terms."""
    spec = FRED_SERIES[code]
    levels: dict[str, float] = {}
    for p, _, value in _observations(body):
        levels.setdefault(p, value)
    if period not in levels:
        raise ValueError("FRED vintage lacks the reference month")

    def measure(p: str) -> float | None:
        if p not in levels:
            return None
        if not spec.percent_change:
            return round(levels[p] * spec.scale, 6)
        prior = levels.get(_shift(p, -1))
        if not prior:
            return None
        return round((levels[p] / prior - 1) * 100, 1)

    actual, previous = measure(period), measure(_shift(period, -1))
    if actual is None or (code != "RETAIL_SALES" and previous is None):
        raise ValueError("FRED history too short")
    return actual, previous


def expected_slot(code: str, period: str, first_release: date, body: bytes) -> datetime | None:
    """Scheduled release of the month after ``period``, from a FRED release-dates body."""
    spec = FRED_SERIES[code]
    month_after = _shift(period, 2)
    earliest = max(
        first_release + MIN_RELEASE_GAP,
        date(int(month_after[:4]), int(month_after[5:]), 1),  # after the month has ended
    )
    dates = sorted(
        day
        for row in json.loads(body)["release_dates"]
        if (day := date.fromisoformat(row["date"])) >= earliest
    )
    if not dates:
        return None
    return datetime.combine(dates[0], spec.release_time, tzinfo=NY)


class FredFallback:
    """Fetches one Census headline from ALFRED.

    Caches the vintage per first release, and the next month's scheduled slot until
    that slot arrives (from then on the schedule is re-read every poll)."""

    def __init__(self, key: str | None):
        self.key = key
        self._vintages: dict[str, tuple[date, float, float | None]] = {}
        self._slots: dict[tuple[str, str], datetime] = {}  # (series, missing month) -> slot

    async def fetch(self, request, code: str, now: datetime, reason: str) -> FallbackResult:
        if not self.key:
            return FallbackResult("not_configured", reason)
        spec = FRED_SERIES[code]
        base = {"api_key": self.key, "series_id": spec.series_id, "file_type": "json"}
        try:
            body = await request(
                "GET",
                FRED_OBSERVATIONS_URL,
                params={
                    **base,
                    "output_type": "4",  # initial release only
                    "realtime_start": "1776-07-04",
                    "realtime_end": "9999-12-31",
                    "sort_order": "desc",
                    "limit": "3",
                },
            )
            period, first = parse_first_release(body, now)
            cached = self._vintages.get(spec.series_id)
            if cached and cached[0] == first:
                actual, previous = cached[1], cached[2]
            else:
                day = first.isoformat()
                body = await request(
                    "GET",
                    FRED_OBSERVATIONS_URL,
                    params={
                        **base,
                        "realtime_start": day,
                        "realtime_end": day,
                        "sort_order": "desc",
                        "limit": "4",
                    },
                )
                actual, previous = parse_vintage(code, body, period)
                self._vintages[spec.series_id] = (first, actual, previous)
        except Exception:
            return FallbackResult("failed", reason)
        found = FallbackResult(
            "current",
            reason,
            period=period,
            actual=actual,
            previous=previous,
            vintage=first,
            source_url=ALFRED_SERIES_URL.format(series_id=spec.series_id),
        )
        local = now.astimezone(NY)
        if period >= _shift(f"{local.year:04d}-{local.month:02d}", -1):
            return found
        missing = _shift(period, 1)
        due = self._slots.get((spec.series_id, missing))
        if due is None or now >= due:  # re-read the schedule once the slot arrives
            try:
                body = await request(
                    "GET",
                    FRED_RELEASE_DATES_URL,
                    params={
                        "api_key": self.key,
                        "release_id": str(spec.release_id),
                        "file_type": "json",
                        "realtime_start": (first + timedelta(days=1)).isoformat(),
                        "realtime_end": "9999-12-31",
                        "include_release_dates_with_no_data": "true",
                        "sort_order": "asc",
                        "limit": "50",
                    },
                )
                due = expected_slot(code, period, first, body)
            except Exception:
                due = None
            if due is None:
                return replace(found, outcome="unscheduled", missing=missing)
            self._slots = {(spec.series_id, missing): due} | {
                k: v for k, v in self._slots.items() if k[0] != spec.series_id
            }
        if now < due:
            return found  # the next month is not due yet
        outcome = "pending" if now < due + RELEASE_GRACE else "late"
        return replace(found, outcome=outcome, missing=missing, due=due)
