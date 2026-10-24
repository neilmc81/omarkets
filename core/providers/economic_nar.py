"""Bounded official NAR existing-home sales releases; no inferred actuals."""
from __future__ import annotations

import calendar
import hashlib
import re
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit

from core.domain.economics import EconomicEvent
from core.providers.economic_adp import _ReleaseHTML
from core.providers.economic_base import EconomicBatch
from core.providers.economic_calendars import stable_id
from core.providers.economic_http import HttpEconomicProvider

NAR_URL = "https://www.nar.realtor/research-and-statistics/housing-statistics/existing-home-sales"
ORIGIN = "https://www.nar.realtor"


def release_url(body: bytes) -> str:
    page = _ReleaseHTML()
    page.feed(body.decode("utf-8"))
    links = {urljoin(ORIGIN, href) for href, title in page.links
             if "read the full news release" in title.casefold()}
    if len(links) != 1:
        raise ValueError("unique NAR release link unavailable")
    url = links.pop()
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.netloc != "www.nar.realtor"
            or not parts.path.startswith("/newsroom/existing-home-sales-")
            or parts.query or parts.fragment):
        raise ValueError("invalid NAR release URL")
    return url


def parse_release(body: bytes, url: str, now: datetime) -> EconomicEvent:
    page = _ReleaseHTML()
    page.feed(body.decode("utf-8"))
    if page.meta.get("og:url") != url:
        raise ValueError("NAR canonical identity mismatch")
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.netloc != "www.nar.realtor"
            or not parts.path.startswith("/newsroom/existing-home-sales-")
            or parts.query or parts.fragment):
        raise ValueError("invalid NAR issuer URL")
    published = datetime.fromisoformat(page.meta.get("article:published_time", ""))
    if published.tzinfo is None or published > now:
        raise ValueError("NAR publication time unavailable or future")
    text = " ".join(" ".join(page.text).split())
    # Limit extraction to the explicitly national total-sales section. Inventory,
    # regional and property-subtype figures outside it cannot supply an actual.
    date_match = re.search(r"WASHINGTON \(([A-Za-z]+ \d{1,2}, \d{4})\)", text)
    section = re.search(
        r"National Snapshot Total Existing-Home Sales for "
        r"(" + "|".join(calendar.month_name[1:]) + r")(?:\s+(\d{4}))? "
        r"(.{1,800}?) (?=Inventory in |Median Sales Price|Single-Family|Regional Snapshot)",
        text, re.I)
    description = page.meta.get("og:description", "")
    if date_match is None or section is None:
        raise ValueError("explicit national NAR section and date unavailable")
    report_date = datetime.strptime(date_match[1], "%B %d, %Y").date()
    if report_date != published.date():
        raise ValueError("NAR release dates disagree")
    month = list(calendar.month_name).index(section[1].capitalize())
    year = int(section[2]) if section[2] else report_date.year
    if ((not section[2] and month >= report_date.month)
            or (year, month) >= (report_date.year, report_date.month)):
        raise ValueError("NAR reference period ambiguous or not completed")
    national = section[3]
    excluded = r"\b(?:Northeast|Midwest|South|West|regional|single-family|condo|co-op)\b"
    # A decimal dot is allowed in the sentence's percentage/change values.
    rate_pattern = (r"\bExisting-home sales (?:(?!\.\s|[!?]).){0,220}?"
                    r"seasonally adjusted annual rate of (\d+(?:\.\d+)?) million")
    rates = re.findall(rate_pattern, national, re.I)
    metadata_head = description.split(". ", 1)[0]
    metadata_rates = re.findall(rate_pattern, metadata_head, re.I)
    metadata_periods = re.findall(
        r"\bin (" + "|".join(calendar.month_name[1:]) + r")(?:\s+(\d{4}))?\b",
        metadata_head, re.I)
    if (re.search(excluded, national, re.I) or re.search(excluded, metadata_head, re.I)
            or len(rates) != 1 or len(metadata_rates) != 1
            or float(rates[0]) != float(metadata_rates[0])
            or len(metadata_periods) != 1
            or metadata_periods[0][0].casefold() != section[1].casefold()
            or (metadata_periods[0][1] and int(metadata_periods[0][1]) != year)):
        raise ValueError("national NAR rate and period do not agree with metadata")
    period = f"{year}-{month:02d}"
    actual = float(rates[0]) * 1_000_000
    if actual <= 0:
        raise ValueError("invalid NAR national annual rate")
    return EconomicEvent(
        id=stable_id("nar_releases", url), event_code="EXISTING_HOME_SALES",
        name="Existing Home Sales", category="housing", importance="medium",
        unit="homes/year (seasonally adjusted annual rate)", frequency="monthly",
        period=period, status="released", actual=actual, actual_basis="source_release",
        provider="nar_releases", source_event_id=url, source_url=url,
        released_at=published.astimezone(UTC), received_at=now,
        source_revision=hashlib.sha256(
            f"{url}|{published.isoformat()}|{period}|{actual}".encode()).hexdigest(), mode="live",
        notes=["Official NAR national seasonally adjusted annual sales rate.",
               "Publication timestamp is issuer metadata; not a scheduled release time.",
               "Original consensus and prior vintages are unavailable from this release."],
    )


class NARReleasesProvider(HttpEconomicProvider):
    provider_name = "nar_releases"

    async def collect(self, now: datetime) -> EconomicBatch:
        url = release_url(await self.request("GET", NAR_URL))
        event = parse_release(await self.request("GET", url), url, now)
        return EconomicBatch(provider=self.name, checked_at=now, events=[event], status="healthy")
