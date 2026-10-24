"""FRED's public schedule for CPI and Employment Situation release slots.

FRED republishes source-provided dates in US Central time. This adapter never
asserts that BLS published, or that FRED updated, at the scheduled instant.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from core.domain.economics import EconomicEvent
from core.providers.economic_base import EconomicBatch, EconomicProviderError
from core.providers.economic_calendars import stable_id
from core.providers.economic_fred import FRED_MAINTENANCE, is_fred_maintenance
from core.providers.economic_http import HttpEconomicProvider

FRED_CALENDAR_URL = "https://fred.stlouisfed.org/releases/calendar"
RELEASES = {
    10: ("CPI", "Consumer Price Index", "inflation"),
    50: ("PAYROLLS", "Employment Situation", "employment"),
    192: ("JOLTS", "Job Openings and Labor Turnover Survey", "employment"),
}
_DATE = re.compile(
    r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday) "
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December) "
    r"\d{1,2}, \d{4}\b"
)
_TIME = re.compile(r"\b(\d{1,2}):(\d{2})\s*([ap])m\b", re.IGNORECASE)


class _Rows(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[tuple[str, set[str]]] = []
        self._in_row = False
        self._text: list[str] = []
        self._links: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._in_row = True
            self._text = []
            self._links = set()
        elif tag == "a" and self._in_row:
            self._links.update(value for key, value in attrs if key == "href" and value)

    def handle_data(self, data: str) -> None:
        if self._in_row:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "tr" and self._in_row:
            self.rows.append((" ".join(" ".join(self._text).split()), self._links))
            self._in_row = False


def parse_calendar(body: bytes, release_id: int, year: int) -> list[datetime]:
    """Fail closed on changed/ambiguous row structure or local DST time."""
    if release_id not in RELEASES:
        raise ValueError("unsupported release")
    if b"All times are US Central Time." not in body:
        raise ValueError("calendar timezone declaration missing")
    parser = _Rows()
    parser.feed(body.decode("utf-8"))
    target = f"/release?rid={release_id}"
    pending_date: datetime | None = None
    found: list[datetime] = []
    for text, links in parser.rows:
        date_match = _DATE.search(text)
        if date_match:
            pending_date = datetime.strptime(date_match.group(), "%A %B %d, %Y")
            continue
        if target not in links:
            continue
        if pending_date is None:
            raise ValueError("release row has no date")
        time_match = _TIME.search(text)
        if time_match is None:
            raise ValueError("release row has no concrete time")
        hour = int(time_match[1]) % 12 + (12 if time_match[3].lower() == "p" else 0)
        minute = int(time_match[2])
        naive = pending_date.replace(hour=hour, minute=minute)
        if naive.year != year:
            raise ValueError("calendar year mismatch")
        zone = ZoneInfo("America/Chicago")
        first = naive.replace(tzinfo=zone, fold=0)
        second = naive.replace(tzinfo=zone, fold=1)
        if first.utcoffset() != second.utcoffset():
            raise ValueError("ambiguous or nonexistent local time")
        found.append(first.astimezone(UTC))
        pending_date = None
    if len(found) != len(set(found)):
        raise ValueError("duplicate release slot")
    return found


class FREDReleaseCalendarProvider(HttpEconomicProvider):
    provider_name = "fred_release_calendar"

    async def collect(self, now: datetime) -> EconomicBatch:
        events: list[EconomicEvent] = []
        issues: list[str] = []
        for year in (now.year - 1, now.year, now.year + 1):
            if FRED_MAINTENANCE in issues:
                break
            for release_id, (code, name, category) in RELEASES.items():
                url = f"{FRED_CALENDAR_URL}?rid={release_id}&y={year}"
                try:
                    body = await self.request("GET", url)
                    if is_fred_maintenance(body):
                        issues.append(FRED_MAINTENANCE)
                        break
                    slots = parse_calendar(body, release_id, year)
                except (EconomicProviderError, ValueError, UnicodeError, OSError):
                    issues.append(f"FRED calendar {release_id}/{year} unavailable or invalid")
                    continue
                for slot in slots:
                    # Retain bounded official historical schedules from the same
                    # annual requests; this restores slots, never past forecasts.
                    if slot < now - timedelta(days=90):
                        continue
                    identity = f"{release_id}:{slot.date().isoformat()}"
                    events.append(
                        EconomicEvent(
                            id=stable_id(self.name, identity),
                            event_code=code,
                            name=name,
                            category=category,
                            importance="high" if code == "JOLTS" else "critical",
                            scheduled_at=slot,
                            period=None,
                            unit="release",
                            frequency="release",
                            status="due" if slot <= now else "scheduled",
                            provider=self.name,
                            source_event_id=identity,
                            source_url=url,
                            received_at=now,
                            notes=[
                                "FRED republishes source-provided release date in US Central time.",
                                "Schedule only; not proof of BLS publication "
                                "or FRED data availability.",
                                "Reference period unavailable; no period inferred.",
                                "Official schedule lookback is bounded to 90 days; "
                                "historical consensus and original actuals are not reconstructed.",
                                "Date-keyed identity; cross-date reschedules require review.",
                            ],
                            mode="live",
                        )
                    )
        if not events:
            issues.append("FRED calendar contains no supported release slots")
        return EconomicBatch(
            provider=self.name,
            checked_at=now,
            events=events,
            issues=issues,
            checked_series_ids=[],
            status="degraded" if issues else "healthy",
        )
