"""ISM-issued Manufacturing and Services values from public PR Newswire releases."""

from __future__ import annotations

import asyncio
import calendar
import hashlib
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Literal
from zoneinfo import ZoneInfo

from core.domain.economics import EconomicEvent, MacroObservation, MacroProvenance
from core.providers.economic_base import EconomicBatch, EconomicProviderError
from core.providers.economic_calendars import stable_id
from core.providers.economic_http import HttpEconomicProvider

LIST_URL = "https://www.prnewswire.com/news/institute-for-supply-management/"
ORIGIN = "https://www.prnewswire.com"
HEADERS = {"User-Agent": "Omarkets/0.1 (local economic calendar)", "Accept": "text/html"}
_NUM = re.compile(r"\d{1,3}(?:\.\d+)?")
_DATE = re.compile(r"\b([A-Z][a-z]{2} \d{2}, \d{4}, \d{1,2}:\d{2} ET)\b")


class _HTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.author = ""
        self.links: list[tuple[str, str]] = []
        self.rows: list[list[str]] = []
        self.tables: list[list[list[str]]] = []
        self._table_stack: list[list[list[str]]] = []
        self._anchor: tuple[str, list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._title: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "meta" and attributes.get("name") == "author":
            self.author = attributes.get("content") or ""
        if tag == "title":
            self._title = []
        if tag == "a":
            self._anchor = (attributes.get("href") or "", [])
        if tag == "table":
            self._table_stack.append([])
        if tag == "tr":
            self._row = []
        if tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        for target in (self._title, self._cell):
            if target is not None:
                target.append(data)
        if self._anchor is not None:
            self._anchor[1].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self._title is not None:
            self.title = " ".join(" ".join(self._title).split())
            self._title = None
        if tag == "a" and self._anchor is not None:
            href, content = self._anchor
            self.links.append((href, " ".join(" ".join(content).split())))
            self._anchor = None
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(" ".join(" ".join(self._cell).split()))
            self._cell = None
        if tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            if self._table_stack:
                self._table_stack[-1].append(self._row)
            self._row = None
        if tag == "table" and self._table_stack:
            self.tables.append(self._table_stack.pop())


def _clean(text: str) -> str:
    return " ".join(text.replace("®", "").split())


def discover_release(
    body: bytes,
    month: int,
    year: int,
    family: Literal["manufacturing", "services"] = "manufacturing",
) -> str | None:
    if b"News from Institute for Supply Management" not in body:
        raise ValueError("issuer listing identity missing")
    parsed = _HTML()
    parsed.feed(body.decode("utf-8"))
    expected = f"{calendar.month_name[month]} {year} ISM {family.title()} PMI Report"
    eligible = [
        (href, title)
        for href, title in parsed.links
        if href.startswith(f"/news-releases/{family}-pmi-at-")
    ][:12]
    candidates = {
        href
        for href, title in eligible
        if expected in _clean(title) and re.fullmatch(r"/news-releases/[a-z0-9-]+-\d+\.html", href)
    }
    if len(candidates) > 1:
        raise ValueError("ambiguous ISM release listing")
    return ORIGIN + next(iter(candidates)) if candidates else None


def parse_release(
    body: bytes,
    month: int,
    year: int,
    family: Literal["manufacturing", "services"] = "manufacturing",
) -> tuple[datetime, float, float]:
    parsed = _HTML()
    parsed.feed(body.decode("utf-8"))
    if parsed.author != "Institute for Supply Management":
        raise ValueError("release issuer is not ISM")
    title = _clean(parsed.title)
    expected = f"{calendar.month_name[month]} {year} ISM {family.title()} PMI Report"
    if expected not in title:
        raise ValueError("release reference period mismatch")
    headline = re.fullmatch(
        re.escape(family.title()) + r" PMI at (\d{1,3}(?:\.\d+)?)%; " + re.escape(expected), title
    )
    if headline is None:
        raise ValueError("release headline unavailable")
    publisher = body.find(b"News provided by")
    if publisher < 0:
        raise ValueError("release publisher block unavailable")
    block = body[publisher : publisher + 1200].decode("utf-8")
    if "Institute for Supply Management" not in block:
        raise ValueError("release publisher block mismatch")
    dates = _DATE.findall(block)
    if len(dates) != 1:
        raise ValueError("publication time missing or ambiguous")
    local = datetime.strptime(dates[0], "%b %d, %Y, %H:%M ET").replace(
        tzinfo=ZoneInfo("America/New_York")
    )
    published = local.astimezone(UTC)
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    if (local.year, local.month, local.hour, local.minute) != (next_year, next_month, 10, 0):
        raise ValueError("publication month inconsistent with report")
    values: dict[str, float] = {}
    pmi_label = f"{family.title()} PMI"
    if family == "services":
        tables = [
            table
            for table in parsed.tables
            if any(
                len(row) >= 2 and _clean(row[0]) == pmi_label and _NUM.fullmatch(row[1])
                for row in table
            )
        ]
        if len(tables) != 1:
            raise ValueError("Services comparison table missing or ambiguous")
        headers = [row for row in tables[0] if any("Series Index" in _clean(cell) for cell in row)]
        expected_column = f"Series Index {calendar.month_abbr[month]}"
        if len(headers) != 1 or _clean(headers[0][0]) != expected_column:
            raise ValueError("Services table current-month column mismatch")
        comparison = [
            row
            for row in tables[0]
            if any(_clean(cell) == "Services PMI" for cell in row)
            and any(_clean(cell) == "Manufacturing PMI" for cell in row)
        ]
        if len(comparison) != 1:
            raise ValueError("Services table sector order mismatch")
        sector_headers = [_clean(cell) for cell in comparison[0]]
        if sector_headers.index("Services PMI") > sector_headers.index("Manufacturing PMI"):
            raise ValueError("Services table sector order mismatch")
    for label in (pmi_label, "Prices"):
        matches = [
            row
            for row in parsed.rows
            if len(row) >= 2 and _clean(row[0]) == label and _NUM.fullmatch(row[1])
        ]
        if len(matches) != 1:
            raise ValueError(f"{label} value missing or ambiguous")
        value = float(matches[0][1])
        if not 0 <= value <= 100:
            raise ValueError(f"{label} index value outside range")
        values[label] = value
    if values[pmi_label] != float(headline[1]):
        raise ValueError("PMI headline and table disagree")
    return published, values[pmi_label], values["Prices"]


class ISMNewswireProvider(HttpEconomicProvider):
    provider_name = "ism_newswire"

    async def collect(self, now: datetime) -> EconomicBatch:
        local = now.astimezone(ZoneInfo("America/New_York"))
        year, month = (local.year - 1, 12) if local.month == 1 else (local.year, local.month - 1)
        period = f"{year:04d}-{month:02d}"
        listing_body = await self.request("GET", LIST_URL, headers=HEADERS)
        events, observations, issues = [], [], []
        verified_bodies: dict[str, bytes] = {}
        for family, definitions in (
            (
                "manufacturing",
                (
                    ("ISM_MFG", "ISM Manufacturing PMI", "growth"),
                    ("ISM_MFG_PRICES", "ISM Manufacturing Prices", "inflation"),
                ),
            ),
            (
                "services",
                (("ISM_SERVICES", "ISM Services PMI", "growth"),),
            ),
        ):
            try:
                listed = await asyncio.to_thread(
                    discover_release, listing_body, month, year, family
                )
                if listed is None:
                    issues.append(f"ISM {family.title()} {period} report not yet listed")
                    continue
                release_body = await self.request("GET", listed, headers=HEADERS)
                published, pmi, prices = await asyncio.to_thread(
                    parse_release, release_body, month, year, family
                )
                if published > now:
                    raise ValueError("release publication time is in the future")
            except (EconomicProviderError, ValueError, UnicodeError):
                issues.append(f"ISM {family.title()} {period} release unavailable or rejected")
                continue
            family_values = (pmi, prices) if family == "manufacturing" else (pmi,)
            for (code, name, category), value in zip(definitions, family_values, strict=True):
                identity = f"{period}:{code}"
                verified_bodies[code] = release_body
                events.append(
                    EconomicEvent(
                        id=stable_id(self.name, identity),
                        event_code=code,
                        name=name,
                        category=category,
                        importance="high",
                        period=period,
                        unit="index points",
                        frequency="monthly",
                        status="released",
                        actual=value,
                        actual_basis="source_release",
                        provider=self.name,
                        source_event_id=identity,
                        source_url=listed,
                        released_at=published,
                        received_at=now,
                        notes=[
                            "ISM-issued public newswire release; table value and issuer verified."
                        ],
                        mode="live",
                    )
                )
        # Only parser-verified monthly PMI values enter the macro registry.
        for event in events:
            if event.event_code not in {"ISM_MFG", "ISM_SERVICES"}:
                continue
            observations.append(
                MacroObservation(
                    id=event.event_code,
                    period=event.period,
                    value=event.actual,
                    unit=event.unit,
                    source_series_id=event.event_code,
                    provider=self.name,
                    released_at=event.released_at,
                    received_at=event.received_at,
                    notes=event.notes,
                    metadata=MacroProvenance(
                        source_url=event.source_url,
                        source_revision=hashlib.sha256(
                            verified_bodies[event.event_code]
                        ).hexdigest(),
                    ),
                )
            )
        return EconomicBatch(
            provider=self.name,
            checked_at=now,
            status="degraded" if issues else "healthy",
            events=events,
            observations=observations,
            checked_series_ids=[item.id for item in observations],
            issues=issues,
        )
