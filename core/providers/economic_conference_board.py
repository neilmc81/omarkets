"""Conference Board Consumer Confidence headline from its public PR Newswire listing.

The Conference Board's own site serves no machine-readable release, but each
monthly release appears on its PR Newswire newsroom with the publication time
(ET) and a first sentence carrying the index level and the prior month's
(revised) level. Only that sentence is read; anything else fails closed.
"""

from __future__ import annotations

import calendar
import hashlib
import html
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from core.domain.economics import EconomicEvent
from core.providers.economic_base import EconomicBatch, ReleasePending, require_published
from core.providers.economic_calendars import stable_id
from core.providers.economic_http import HttpEconomicProvider

LIST_URL = "https://www.prnewswire.com/news/the-conference-board/"
ORIGIN = "https://www.prnewswire.com"
HEADERS = {"User-Agent": "Omarkets/0.1 (local economic calendar)", "Accept": "text/html"}
PROVIDER = "conference_board"
NY = ZoneInfo("America/New_York")
MONTHS = {name: number for number, name in enumerate(calendar.month_name) if name}

_CARD = re.compile(
    r'href="(/news-releases/[a-z0-9-]+\.html)".{0,3000}?'
    r"<small>([A-Z][a-z]{2} \d{1,2}, \d{4}, \d{1,2}:\d{2}) ET</small>\s*([^<]+?)\s*</h3>\s*"
    r"<p[^>]*>([^<]*)</p>",
    re.S,
)
_HEADLINE = re.compile(
    r"Consumer Confidence Index\S* (?:\S+ ){1,6}?(?:to|at) (\d{1,3}\.\d) \(1985=100\) "
    r"in ([A-Z][a-z]+)(?:, (?:up|down) from (\d{1,3}\.\d) in ([A-Z][a-z]+))?"
)


def parse_listing(body: bytes, now: datetime) -> list[EconomicEvent]:
    """Every published consumer-confidence release card on the listing page."""
    return read_listing(body, now)[0]


def read_listing(body: bytes, now: datetime) -> tuple[list[EconomicEvent], int]:
    """Published release cards, plus how many cards are stamped a moment ahead.

    A pending card (listed shortly before its stated time) is skipped before
    its headline is read, and counted so the check is not called missing.
    """
    events, pending = [], 0
    for link, stamp, title, summary in _CARD.findall(body.decode("utf-8")):
        title, summary = html.unescape(title).strip(), html.unescape(summary)
        if not title.lower().startswith("us consumer confidence"):
            continue
        released = datetime.strptime(stamp, "%b %d, %Y, %H:%M").replace(tzinfo=NY)
        try:
            require_published(released, now, "consumer confidence release")
        except ReleasePending:
            pending += 1
            continue
        match = _HEADLINE.search(summary)
        if match is None:
            raise ValueError("consumer confidence headline not recognised")
        actual, month, previous, prior_month = match.groups()
        if month not in MONTHS:
            raise ValueError("invalid consumer confidence release")
        year = released.year - (1 if MONTHS[month] > released.month else 0)
        period = f"{year:04d}-{MONTHS[month]:02d}"
        if prior_month and MONTHS.get(prior_month) != (MONTHS[month] - 2) % 12 + 1:
            raise ValueError("consumer confidence prior month mismatch")
        identity = f"{period}:CONSUMER_CONFIDENCE"
        events.append(
            EconomicEvent(
                id=stable_id(PROVIDER, identity),
                event_code="CONSUMER_CONFIDENCE",
                name="CB Consumer Confidence",
                category="growth",
                importance="medium",
                unit="value",
                frequency="monthly",
                period=period,
                status="released",
                actual=float(actual),
                previous=float(previous) if previous else None,
                actual_basis="source_release",
                provider=PROVIDER,
                source_event_id=identity,
                source_url=ORIGIN + link,
                released_at=released,
                received_at=now,
                source_revision=hashlib.sha256(f"{identity}:{actual}".encode()).hexdigest(),
                mode="live",
                notes=[
                    "Conference Board release on PR Newswire; publication time from the listing.",
                    "Index level (1985=100); previous is the revised prior month.",
                ],
            )
        )
    return events, pending


class ConferenceBoardProvider(HttpEconomicProvider):
    provider_name = PROVIDER

    async def collect(self, now: datetime) -> EconomicBatch:
        events, pending = read_listing(await self.request("GET", LIST_URL, headers=HEADERS), now)
        missing = not events and not pending
        return EconomicBatch(
            provider=self.name,
            checked_at=now,
            events=events,
            issues=["No consumer confidence release on the listing page"] if missing else [],
            status="degraded" if missing else "healthy",
        )
