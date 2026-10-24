"""Official BLS release slots; schedule only, never observed release values."""

from __future__ import annotations

from datetime import UTC, datetime

from icalendar import Calendar

from core.domain.economics import EconomicEvent
from core.providers.economic_base import EconomicBatch
from core.providers.economic_calendars import stable_id, summary_period
from core.providers.economic_http import HttpEconomicProvider

BLS_CALENDAR_URL = "https://www.bls.gov/schedule/news_release/bls.ics"


def _release_kind(summary: str) -> tuple[str, str, str] | None:
    title = summary.casefold().strip()
    if title.startswith("consumer price index"):
        return "CPI", "inflation", "critical"
    if title.startswith("employment situation"):
        return "PAYROLLS", "employment", "critical"
    return None


class BLSCalendarProvider(HttpEconomicProvider):
    provider_name = "bls_calendar"

    async def collect(self, now: datetime) -> EconomicBatch:
        calendar = Calendar.from_ical(await self.request("GET", BLS_CALENDAR_URL))
        events: list[EconomicEvent] = []
        issues: list[str] = []
        seen: set[str] = set()
        for entry in calendar.walk("VEVENT"):
            try:
                summary = str(entry["SUMMARY"]).strip()
                kind = _release_kind(summary)
                if kind is None:
                    continue
                uid = str(entry["UID"]).strip()
                if not uid or uid in seen:
                    raise ValueError("missing or duplicate UID")
                seen.add(uid)
                start = entry.decoded("DTSTART")
                if not isinstance(start, datetime) or start.tzinfo is None:
                    raise ValueError("release requires unambiguous timestamp")
                start = start.astimezone(UTC)
                sequence = int(entry.get("SEQUENCE", 0))
                if sequence < 0:
                    raise ValueError("invalid revision")
                code, category, importance = kind
                period = summary_period(summary)
                notes = [
                    "Official BLS publication schedule only; actual and consensus unavailable.",
                    "No publication is inferred from a past schedule slot.",
                ]
                if period is None:
                    notes.append("Reference period not explicit; no period inferred.")
                events.append(
                    EconomicEvent(
                        id=stable_id(self.name, uid),
                        event_code=code,
                        name=summary,
                        category=category,
                        importance=importance,
                        scheduled_at=start,
                        period=period,
                        unit="release",
                        frequency="release",
                        status=(
                            "cancelled"
                            if str(entry.get("STATUS", "")).upper() == "CANCELLED"
                            else ("scheduled" if start > now else "due")
                        ),
                        provider=self.name,
                        source_event_id=uid,
                        source_url=BLS_CALENDAR_URL,
                        received_at=now,
                        source_revision=str(sequence),
                        notes=notes,
                        mode="live",
                    )
                )
            except (ValueError, KeyError, TypeError):
                issues.append("BLS calendar entry rejected: invalid identity or timestamp")
        if not events:
            issues.append("BLS calendar contains no supported concrete releases")
        return EconomicBatch(
            provider=self.name,
            checked_at=now,
            events=events,
            issues=issues,
            checked_series_ids=[],
            status="degraded" if issues else "healthy",
        )
