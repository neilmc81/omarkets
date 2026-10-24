from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit

from defusedxml.ElementTree import fromstring

from core.domain.economics import EconomicEvent, EconomicStatement
from core.providers.economic_base import EconomicBatch, EconomicProviderError
from core.providers.economic_calendars import stable_id
from core.providers.economic_http import HttpEconomicProvider

FED_RSS_URL = "https://www.federalreserve.gov/feeds/press_monetary.xml"


class _StatementHTML(HTMLParser):
    """Extract unstyled article paragraphs, excluding metadata and navigation."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.paragraph = None
        self.paragraphs = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.stack.append((tag, attrs))
        in_article = any(a.get("id") == "article" for _, a in self.stack)
        if tag == "p" and in_article and not attrs.get("class"):
            self.paragraph = []

    def handle_endtag(self, tag):
        if tag == "p" and self.paragraph is not None:
            text = " ".join("".join(self.paragraph).split())
            if text and not text.startswith(("For media inquiries", "Implementation Note")):
                self.paragraphs.append(text)
            self.paragraph = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.paragraph is not None and not any(
            tag in {"script", "style", "nav"} for tag, _ in self.stack
        ):
            self.paragraph.append(data)


def _extract_statement(body: bytes) -> str:
    parser = _StatementHTML()
    parser.feed(body.decode("utf-8"))
    text = "\n\n".join(parser.paragraphs)
    if (
        not (100 <= len(text) <= 40_000)
        or len(parser.paragraphs) < 2
        or "Committee" not in text
        or "federal funds rate" not in text
    ):
        raise ValueError("missing or invalid policy statement")
    return text


def _statement_url(link: str, released: datetime) -> bool:
    parts = urlsplit(link)
    return (
        parts.scheme == "https"
        and parts.netloc == "www.federalreserve.gov"
        and not parts.query
        and not parts.fragment
        and re.fullmatch(rf"/newsevents/pressreleases/monetary{released:%Y%m%d}a.htm", parts.path)
        is not None
    )


class FedReleasesProvider(HttpEconomicProvider):
    provider_name = "fed_releases"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._statements: dict[str, tuple[datetime, EconomicStatement]] = {}

    async def _statement(self, link: str, released: datetime) -> EconomicStatement:
        cached_entry = self._statements.get(link)
        cached = cached_entry[1] if cached_entry else None
        if cached_entry is not None:
            age = self.clock() - cached_entry[0]
            if timedelta(0) <= age < timedelta(minutes=5):
                return cached_entry[1]
        text = await asyncio.to_thread(_extract_statement, await self.request("GET", link))
        statement = EconomicStatement(
            text=text,
            source_url=link,
            published_at=released,
            received_at=self.clock(),
            content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
        checked_at = statement.received_at
        if (
            cached is not None
            and cached.content_sha256 == statement.content_sha256
            and cached.published_at == statement.published_at
        ):
            # Revalidation must not manufacture a new evidence version.
            statement = cached
        self._statements[link] = (checked_at, statement)
        if len(self._statements) > 8:
            del self._statements[next(iter(self._statements))]
        return statement

    async def collect(self, now: datetime) -> EconomicBatch:
        root = fromstring(await self.request("GET", FED_RSS_URL))
        if root.tag != "rss":
            raise ValueError("invalid RSS root")
        events, issues, seen = [], [], set()
        for item in root.findall("./channel/item"):
            try:
                title = item.findtext("title", "").strip()
                if "economic projections" in title.lower():
                    code = "SEP"
                elif "issues FOMC statement" in title:
                    code = "FOMC_STATEMENT"
                elif "Minutes of the Federal Open Market Committee" in title:
                    code = "FOMC_MINUTES"
                else:
                    continue
                link = item.findtext("link", "").strip()
                parts = urlsplit(link)
                if parts.scheme != "https" or parts.netloc != "www.federalreserve.gov":
                    raise ValueError("untrusted release link")
                uid = item.findtext("guid", link).strip()
                if not uid or uid in seen:
                    raise ValueError("invalid release identity")
                released = parsedate_to_datetime(item.findtext("pubDate", ""))
                if released.tzinfo is None or released > now:
                    raise ValueError("invalid publication timestamp")
                seen.add(uid)
                statement = None
                if code == "FOMC_STATEMENT" and now - released <= timedelta(hours=24):
                    if _statement_url(link, released):
                        try:
                            statement = await self._statement(link, released)
                        except (EconomicProviderError, ValueError, UnicodeError):
                            issues.append("FOMC statement unavailable; published notice retained")
                    else:
                        issues.append("FOMC statement URL rejected; published notice retained")
                events.append(
                    EconomicEvent(
                        id=stable_id(self.name, uid),
                        event_code=code,
                        name=title,
                        category="policy",
                        importance="critical" if code != "FOMC_MINUTES" else "high",
                        unit="release",
                        frequency="release",
                        status="released",
                        provider=self.name,
                        source_event_id=uid,
                        source_url=link,
                        statement=statement,
                        released_at=released,
                        received_at=now,
                        mode="live",
                        notes=[
                            "Official published notice; numeric actual and consensus unavailable.",
                            "Not automatically merged with calendar events.",
                        ],
                    )
                )
            except (ValueError, KeyError, TypeError, AttributeError):
                issues.append("Fed RSS item rejected: invalid identity or publication timestamp")
        if not events:
            issues.append("Fed RSS has no supported published notices")
        return EconomicBatch(
            provider=self.name,
            checked_at=now,
            events=events,
            issues=issues,
            checked_series_ids=[],
            status="degraded" if issues else "healthy",
        )
