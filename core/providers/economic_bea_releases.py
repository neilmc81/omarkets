from __future__ import annotations

from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from defusedxml.ElementTree import fromstring

from core.domain.economics import EconomicEvent
from core.providers.economic_base import EconomicBatch
from core.providers.economic_calendars import stable_id, summary_period
from core.providers.economic_http import HttpEconomicProvider

BEA_RSS_URL = "https://apps.bea.gov/rss/rss.xml"
BEA_HOSTS = {"www.bea.gov", "apps.bea.gov", "bea.gov"}

BEA_TITLES: tuple[tuple[str, str, str, str], ...] = (
    ("personal income and outlays", "PCE", "inflation", "critical"),
    ("international trade", "TRADE", "growth", "medium"),
    ("corporate profits", "CORPORATE_PROFITS", "growth", "medium"),
)


def _classify(title: str) -> tuple[str, str, str]:
    lowered = title.lower()
    if lowered.startswith("gdp") or "gross domestic product" in lowered:
        return "GDP", "growth", "critical"
    for needle, code, category, importance in BEA_TITLES:
        if needle in lowered:
            return code, category, importance
    return "BEA_RELEASE", "growth", "medium"


def _safe_link(link: str) -> str:
    candidate = link if "://" in link else f"https://{link}"
    parts = urlsplit(candidate)
    if parts.scheme != "https" or parts.netloc not in BEA_HOSTS:
        raise ValueError("untrusted release link")
    return candidate


class BEAReleasesProvider(HttpEconomicProvider):
    provider_name = "bea_releases"

    async def collect(self, now: datetime) -> EconomicBatch:
        root = fromstring(await self.request("GET", BEA_RSS_URL))
        if root.tag != "rss":
            raise ValueError("invalid RSS root")
        channel = root.find("channel")
        if channel is None:
            raise ValueError("invalid RSS channel")
        items = channel.findall("item")
        if len(items) > 500:
            raise ValueError("invalid item count")
        events, issues, seen = [], [], set()
        for item in items:
            try:
                title = (item.findtext("title") or "").strip()
                if not title:
                    raise ValueError("missing title")
                link = _safe_link((item.findtext("link") or "").strip())
                uid = (item.findtext("guid") or link).strip()
                if not uid or uid in seen:
                    raise ValueError("invalid release identity")
                released = parsedate_to_datetime(item.findtext("pubDate") or "")
                if released.tzinfo is None or released > now:
                    raise ValueError("invalid publication timestamp")
                vintage = (item.findtext("vintage") or "").strip()
                seen.add(uid)
                code, category, importance = _classify(title)
                notes = [
                    "Official published notice with real publication time.",
                    "Numeric actual and consensus are not extracted from prose.",
                    "Source notice retained independently; "
                    "lifecycle may match a canonical schedule.",
                ]
                if vintage:
                    notes.append(f"Source vintage: {vintage}")
                events.append(
                    EconomicEvent(
                        id=stable_id(self.name, uid),
                        event_code=code,
                        name=title,
                        period=summary_period(title),
                        category=category,
                        importance=importance,
                        unit="release",
                        frequency="release",
                        status="released",
                        provider=self.name,
                        source_event_id=uid,
                        source_url=link,
                        released_at=released,
                        received_at=now,
                        mode="live",
                        notes=notes,
                    )
                )
            except (ValueError, KeyError, TypeError, AttributeError):
                issues.append("BEA RSS item rejected: invalid identity or publication timestamp")
        if not events:
            issues.append("BEA RSS has no usable items")
        return EconomicBatch(
            provider=self.name,
            checked_at=now,
            events=events,
            issues=issues,
            checked_series_ids=[],
            status="degraded" if issues else "healthy",
        )
