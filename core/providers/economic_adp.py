"""Bounded public ADP issuer releases; no payroll estimates, only an expected-release check."""

from __future__ import annotations

import calendar
import hashlib
import re
from datetime import UTC, date, datetime, time, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from core.domain.economics import EconomicEvent
from core.domain.market_calendar import is_trading_day
from core.providers.economic_base import (
    EconomicBatch,
    EconomicProviderError,
    ReleasePending,
    require_published,
)
from core.providers.economic_calendars import stable_id
from core.providers.economic_http import HttpEconomicProvider
from core.providers.economic_ism_newswire import _HTML

ORIGIN = "https://mediacenter.adp.com"
LISTINGS = (ORIGIN + "/workforce-data-releases?l=9", ORIGIN + "/press-releases?l=25")
NY = ZoneInfo("America/New_York")
HEADERS = {"User-Agent": "Omarkets/0.1 (local economic calendar)", "Accept": "text/html"}
NUMBER = r"-?\d{1,3}(?:,\d{3})*"
# Listing links older than this are ignored. It must reach back past the
# longest gap between monthly releases (five weeks) with room to spare.
DISCOVERY_DAYS = 42
# ADP publishes both reports at 08:15 ET. A report missing before its expected
# time plus this grace is simply not out yet, not a fault.
RELEASE_TIME = time(8, 15)
RELEASE_GRACE = timedelta(hours=1)


class _ReleaseHTML(_HTML):
    def __init__(self):
        super().__init__()
        self.meta = {}
        self.text = []
        self._hidden = 0

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        attributes = dict(attrs)
        if tag == "meta":
            self.meta[attributes.get("property")] = attributes.get("content")
        if tag in {"script", "style"}:
            self._hidden += 1

    def handle_data(self, data):
        super().handle_data(data)
        if not self._hidden:
            self.text.append(data)

    def handle_endtag(self, tag):
        super().handle_endtag(tag)
        if tag in {"script", "style"}:
            self._hidden -= 1


def discover_releases(body: bytes, now: datetime) -> list[str]:
    parsed = _ReleaseHTML()
    parsed.feed(body.decode("utf-8"))
    if parsed.meta.get("og:site_name") != "ADP Media Center":
        raise ValueError("ADP listing identity missing")
    candidates = set()
    for href, title in parsed.links:
        parts = urlsplit(href)
        if (
            parts.scheme != "https"
            or parts.netloc != "mediacenter.adp.com"
            or parts.query
            or parts.fragment
        ):
            continue
        match = re.fullmatch(
            r"/(\d{4}-\d{2}-\d{2})-ADP-National-Employment-Report-[A-Za-z0-9,%-]+", parts.path
        )
        if not match or not title.startswith("ADP National Employment Report"):
            continue
        published_day = datetime.fromisoformat(match[1]).date()
        if (
            now.astimezone(NY).date() - timedelta(days=DISCOVERY_DAYS)
            <= published_day
            <= now.astimezone(NY).date()
        ):
            candidates.add(href)
    return sorted(candidates, reverse=True)[:6]


def parse_release(body: bytes, url: str, now: datetime) -> EconomicEvent:
    parsed = _ReleaseHTML()
    parsed.feed(body.decode("utf-8"))
    if parsed.meta.get("og:site_name") != "ADP Media Center" or parsed.meta.get("og:url") != url:
        raise ValueError("ADP issuer or URL mismatch")
    title = parsed.meta.get("og:title") or ""
    day = datetime.strptime(urlsplit(url).path[1:11], "%Y-%m-%d")
    dates = re.findall(r'<div class="wd_date">([^<]+)</div>', body.decode("utf-8"))
    if len(dates) != 1 or datetime.strptime(dates[0], "%b %d, %Y").date() != day.date():
        raise ValueError("ADP publication date mismatch")
    text = " ".join(" ".join(parsed.text).split())
    weekly = re.fullmatch(
        r"ADP National Employment Report Preliminary Estimate for (\w+ \d{1,2}, \d{4})", title
    )
    previous = revised = None
    notes = []
    if weekly:
        reference = datetime.strptime(weekly[1], "%B %d, %Y").date()
        period = reference.isoformat()
        if reference.weekday() != 5 or not 7 <= (day.date() - reference).days <= 24:
            raise ValueError("ADP weekly reference period mismatch")
        if "publishes every Tuesday at 8:15 a.m. ET" not in text:
            raise ValueError("ADP weekly publication cadence unavailable")
        published = day.replace(hour=8, minute=15, tzinfo=NY).astimezone(UTC)
        notes.append(
            "Publication time uses issuer-stated 08:15 ET cadence and page publication date."
        )
        tables = [
            table
            for table in parsed.tables
            if table
            and table[0]
            == ["Week ending", "Change (Four-week moving average, seasonally adjusted)"]
        ]
        if len(tables) != 1 or len(tables[0]) < 2:
            raise ValueError("ADP weekly table missing or ambiguous")
        rows = tables[0][1:]
        values = {}
        for row in rows:
            if len(row) != 2 or not re.fullmatch(NUMBER, row[1]):
                raise ValueError("ADP weekly value unavailable")
            date = datetime.strptime(row[0], "%m/%d/%Y").date()
            if date in values or date > reference:
                raise ValueError("ADP weekly periods ambiguous")
            values[date] = float(row[1].replace(",", ""))
        if datetime.strptime(rows[0][0], "%m/%d/%Y").date() != reference:
            raise ValueError("ADP weekly current row mismatch")
        actual = values[reference]
        revised = values.get(reference - timedelta(days=7))
        direction = "added" if actual >= 0 else "lost"
        headline = (
            f"For the four weeks ending {weekly[1]}, U.S. private employers {direction} "
            f"an average of {abs(actual):,.0f} jobs per week"
        )
        if headline not in text:
            raise ValueError("ADP weekly headline and table disagree")
        frequency, unit, name = (
            "weekly",
            "jobs/week (four-week average, seasonally adjusted)",
            "ADP Weekly Employment Change",
        )
        notes.append(
            "Earlier table rows are revised history, never actuals for earlier publication slots."
        )
    else:
        monthly = re.fullmatch(
            r"ADP National Employment Report: Private-Sector Employment (Increased|Decreased) by ("
            + NUMBER
            + r") Jobs in (\w+)",
            title,
        )
        if monthly is None:
            raise ValueError("Unsupported ADP release headline")
        month = list(calendar.month_name).index(monthly[3])
        year = day.year - (month > day.month)
        period = f"{year:04d}-{month:02d}"
        if f"{monthly[3]} {year} Report Highlights" not in text:
            raise ValueError("ADP monthly reference period mismatch")
        stamps = re.findall(
            r"<!-- ITEMDATE: (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (EDT|EST) -->", body.decode("utf-8")
        )
        if len(stamps) != 1:
            raise ValueError("ADP monthly publication time unavailable")
        local = datetime.strptime(stamps[0][0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=NY)
        if local.date() != day.date() or local.tzname() != stamps[0][1]:
            raise ValueError("ADP monthly publication time mismatch")
        published = local.astimezone(UTC)
        actual = float(monthly[2].replace(",", "")) * (1 if monthly[1] == "Increased" else -1)
        totals = re.findall(r"Change in U\.S\. Private Employment\s*:\s*(" + NUMBER + r")", text)
        if len(totals) != 1 or float(totals[0].replace(",", "")) != actual:
            raise ValueError("ADP monthly headline and total disagree")
        preceding = calendar.month_name[12 if month == 1 else month - 1]
        revisions = re.findall(
            re.escape(preceding)
            + r" total number of jobs added was revised from ("
            + NUMBER
            + r") to ("
            + NUMBER
            + r")",
            text,
        )
        if len(revisions) > 1:
            raise ValueError("ADP monthly revisions ambiguous")
        if revisions:
            previous, revised = (float(value.replace(",", "")) for value in revisions[0])
        frequency, unit, name = "monthly", "jobs", "ADP Non-Farm Employment Change"
    require_published(published, now, "ADP release")
    identity = f"{day:%Y-%m-%d}:{frequency}"
    return EconomicEvent(
        id=stable_id("adp_releases", identity),
        event_code="ADP",
        name=name,
        category="employment",
        importance="medium",
        period=period,
        unit=unit,
        frequency=frequency,
        status="released",
        actual=actual,
        actual_basis="source_release",
        previous=previous,
        revised_previous=revised,
        provider="adp_releases",
        source_event_id=identity,
        source_url=url,
        released_at=published,
        received_at=now,
        source_revision=hashlib.sha256(body).hexdigest(),
        notes=notes,
        mode="live",
    )


def jobs_report_friday(year: int, month: int) -> date:
    """Friday of the BLS jobs-report week for reference ``month``/``year``.

    BLS rule: the third Friday after the week (Sunday to Saturday) holding the
    12th. A New Year date (Jan 1-3) moves a week later, as on Jan 9, 2026 and
    Jan 8, 2027. (A holiday Friday such as Jul 3, 2026 moves the report to
    Thursday, but the week is the same.) Matches FRED's Employment Situation
    slots for Aug-Dec 2026.
    """
    twelfth = date(year, month, 12)
    saturday = twelfth + timedelta(days=(5 - twelfth.weekday()) % 7)
    day = saturday + timedelta(days=20)
    if day.month == 1 and day.day <= 3:
        day += timedelta(days=7)
    return day


def monthly_release_day(year: int, month: int) -> date:
    """ADP monthly report day for reference ``month``: Wednesday of jobs-report week."""
    day = jobs_report_friday(year, month) - timedelta(days=2)
    while not is_trading_day(day):
        day += timedelta(days=1)
    return day


def _monthly_slots(around: date) -> list[date]:
    slots = []
    for back in range(3, -1, -1):
        year, month = around.year, around.month - back
        while month < 1:
            year, month = year - 1, month + 12
        slots.append(monthly_release_day(year, month))
    return slots


def weekly_release_day(week_start: date) -> date | None:
    """ADP weekly report day in the week starting Monday ``week_start``.

    Tuesdays; after a Monday or Tuesday market holiday it moves to the next
    trading day (Wed Sep 9, 2026 after Labor Day). No weekly in the week of
    the monthly report (none on Tue Sep 29, 2026 before the Sep 30 monthly).
    """
    if any(week_start <= slot < week_start + timedelta(days=7)
           for slot in _monthly_slots(week_start)):
        return None
    day = week_start + timedelta(days=1)
    if not is_trading_day(week_start) or not is_trading_day(day):
        day += timedelta(days=1)
        while not is_trading_day(day):
            day += timedelta(days=1)
    return day


def _at_release(day: date) -> datetime:
    return datetime.combine(day, RELEASE_TIME, tzinfo=NY).astimezone(UTC)


def latest_due_release(family: str, now: datetime) -> datetime:
    """The most recent expected ADP release for ``family`` that is past its grace."""
    today = now.astimezone(NY).date()
    if family == "monthly":
        candidates = _monthly_slots(today)
    else:
        monday = today - timedelta(days=today.weekday())
        candidates = [
            day
            for weeks in range(4)
            if (day := weekly_release_day(monday - timedelta(weeks=weeks))) is not None
        ]
    return max(
        _at_release(day) for day in candidates if _at_release(day) + RELEASE_GRACE <= now
    )


class ADPReleasesProvider(HttpEconomicProvider):
    provider_name = "adp_releases"

    async def collect(self, now: datetime) -> EconomicBatch:
        urls, events, issues = set(), [], []
        for listing in LISTINGS:
            try:
                urls.update(
                    discover_releases(await self.request("GET", listing, headers=HEADERS), now)
                )
            except (EconomicProviderError, ValueError, UnicodeError):
                issues.append("ADP listing unavailable or rejected")
        for url in sorted(urls, reverse=True)[:8]:
            try:
                events.append(
                    parse_release(await self.request("GET", url, headers=HEADERS), url, now)
                )
            except ReleasePending:
                continue
            except (EconomicProviderError, ValueError, UnicodeError):
                issues.append("ADP release unsupported, unavailable or rejected: " + url)
        for family in ("weekly", "monthly"):
            # Nothing new before the next expected release is normal. Only a
            # report still missing after its expected time plus grace is an
            # issue. A release anywhere in the expected slot's week counts, so
            # a one-day schedule shift is not flagged.
            due = latest_due_release(family, now)
            due_local = due.astimezone(NY)
            week_start = due_local.date() - timedelta(days=due_local.weekday())
            if not any(
                event.frequency == family
                and event.released_at is not None
                and event.released_at.astimezone(NY).date() >= week_start
                for event in events
            ):
                issues.append(
                    f"ADP {family} report not found after its expected release "
                    f"({due_local:%a %b %d}, 08:15 ET)"
                )
        return EconomicBatch(
            provider=self.name,
            checked_at=now,
            events=events,
            issues=issues,
            status="degraded" if issues else "healthy",
        )
