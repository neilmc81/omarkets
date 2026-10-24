from __future__ import annotations

import calendar
import hashlib
import html
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from icalendar import Calendar

from core.domain.economics import EconomicEvent
from core.providers.economic_base import EconomicBatch
from core.providers.economic_http import HttpEconomicProvider

BEA_URL = "https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics"
FED_CALENDAR_URL = "https://www.federalreserve.gov/json/calendar.json"


def stable_id(provider: str, identity: str) -> str:
    return provider + ":" + hashlib.sha256(identity.encode()).hexdigest()[:32]


def summary_period(summary: str) -> str | None:
    monthly = re.search(
        r"\b(" + "|".join(calendar.month_name[1:]) + r")\s+(\d{4})\b",
        summary,
        re.IGNORECASE,
    )
    if monthly:
        before = summary[: monthly.start()].rstrip()
        if before.endswith("and"):
            return None
        month = next(
            i for i, name in enumerate(calendar.month_name) if name.lower() == monthly[1].lower()
        )
        return f"{monthly[2]}-{month:02d}"
    quarterly = re.search(r"([1-4])(?:st|nd|rd|th) Quarter(?: and Year)? (\d{4})", summary)
    return f"{quarterly[2]}-Q{quarterly[1]}" if quarterly else None


class BEACalendarProvider(HttpEconomicProvider):
    provider_name = "bea_calendar"

    async def collect(self, now: datetime) -> EconomicBatch:
        calendar_data = Calendar.from_ical(await self.request("GET", BEA_URL))
        events, issues, seen = [], [], set()
        for entry in calendar_data.walk("VEVENT"):
            try:
                uid = str(entry["UID"]).strip()
                summary = str(entry["SUMMARY"]).strip()
                if not uid or not summary or uid in seen:
                    raise ValueError("invalid event identity")
                seen.add(uid)
                start = entry.decoded("DTSTART")
                if not isinstance(start, datetime) or start.tzinfo is None:
                    raise ValueError("event requires aware timestamp")
                stamp = entry.decoded("DTSTAMP")
                if not isinstance(stamp, datetime) or stamp.tzinfo is None:
                    raise ValueError("invalid calendar timestamp")
                sequence = int(entry.get("SEQUENCE", 0))
                if sequence < 0:
                    raise ValueError("invalid sequence")
                if summary.startswith("Personal Income and Outlays"):
                    code, category, importance = "PCE", "inflation", "high"
                elif "international trade" in summary.lower():
                    code, category, importance = "TRADE", "growth", "medium"
                elif summary.startswith(("GDP", "Gross Domestic Product")):
                    code, category, importance = "GDP", "growth", "high"
                else:
                    code, category, importance = "BEA_RELEASE", "growth", "medium"
                notes = [
                    "Calendar announcement only; actual and consensus unavailable.",
                    f"Source summary: {summary}",
                    f"Calendar DTSTAMP: {stamp.astimezone(UTC).isoformat()}",
                ]
                period = summary_period(summary)
                if period is None:
                    notes.append("Single reference period not explicit; no period inferred.")
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
                        status="cancelled"
                        if str(entry.get("STATUS", "")) == "CANCELLED"
                        else ("scheduled" if start > now else "due"),
                        provider=self.name,
                        source_event_id=uid,
                        source_url=BEA_URL,
                        received_at=now,
                        source_revision=str(sequence),
                        notes=notes,
                        mode="live",
                    )
                )
            except (ValueError, KeyError, TypeError):
                issues.append("BEA calendar entry rejected: invalid identity or timestamp")
        if not events:
            issues.append("BEA calendar contains no usable events")
        return EconomicBatch(
            provider=self.name,
            checked_at=now,
            events=events,
            issues=issues,
            checked_series_ids=[],
            status="degraded" if issues else "healthy",
        )


def fed_datetime(month: str, day: str, time: str) -> datetime:
    if not re.fullmatch(r"\d{4}-\d{2}", month) or not re.fullmatch(r"\d{1,2}", day):
        raise ValueError("invalid date")
    match = re.fullmatch(r"(\d{1,2}):(\d{2})\s+([ap])\.m\.", time.strip())
    if not match or not 1 <= int(match[1]) <= 12:
        raise ValueError("concrete time unavailable")
    hour = int(match[1]) % 12 + (12 if match[3] == "p" else 0)
    naive = datetime(int(month[:4]), int(month[5:]), int(day), hour, int(match[2]))
    zone = ZoneInfo("America/New_York")
    first, second = naive.replace(tzinfo=zone, fold=0), naive.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise ValueError("ambiguous or nonexistent local time")
    return first.astimezone(UTC)


#: Speaking engagements in the Fed calendar: schedule only, never a release.
FED_SPEECH_TYPES = {"Speeches": "FED_SPEECH", "Testimony": "FED_TESTIMONY"}
_SPEECH_TITLE = re.compile(r"^(Speech|Discussion|Testimony)\s+-{1,2}\s+(.{3,120})$")
_TAGS = re.compile(r"<[^>]{0,200}>")


def _clean_text(value: object, limit: int = 200) -> str:
    """Untrusted calendar text as plain, bounded text."""
    if not isinstance(value, str):
        return ""
    text = " ".join(html.unescape(_TAGS.sub(" ", value)).split())
    return text[:limit]


def fed_speech(row: dict, now: datetime, source: str) -> EconomicEvent | None:
    """A Board member's speech, discussion or testimony, or None if malformed or past."""
    code = FED_SPEECH_TYPES.get(str(row.get("type") or ""))
    match = _SPEECH_TITLE.match(_clean_text(row.get("title"), 160))
    if code is None or not match:
        return None
    days = str(row.get("days") or "").split(",")
    if len(days) != 1:
        return None
    start = fed_datetime(str(row.get("month") or ""), days[0].strip(), str(row.get("time") or ""))
    if start < now - timedelta(days=7):
        return None
    form, speaker = match.group(1), match.group(2).strip()
    chair = speaker.startswith(("Chair ", "Chairman ", "Chairwoman "))
    label = {"Speech": "speech", "Discussion": "discussion", "Testimony": "testimony"}[form]
    venue = _clean_text(row.get("location"))
    identity = f"{code}:{start.isoformat()}:{speaker}"
    return EconomicEvent(
        id=stable_id(source, identity),
        event_code=code,
        name=f"Fed {label}: {speaker}",
        category="policy",
        importance="high" if chair else "medium",
        scheduled_at=start,
        period=None,
        unit="release",
        frequency="release",
        status="scheduled" if start > now else "due",
        provider=source,
        source_event_id=identity,
        source_url=FED_CALENDAR_URL,
        received_at=now,
        notes=["Schedule only; a speaking engagement, not a data release.",
               *( [f"Venue: {venue}"] if venue else [] )],
        mode="live",
    )


class FedCalendarProvider(HttpEconomicProvider):
    provider_name = "fed_calendar"

    async def collect(self, now: datetime) -> EconomicBatch:
        payload = await self.json_request("GET", FED_CALENDAR_URL)
        rows = payload["events"]
        if not isinstance(rows, list) or len(rows) > 10000:
            raise ValueError("invalid events array")
        events, issues, seen = [], [], set()
        titles = {
            "FOMC Meeting": ("FOMC", "FOMC decision slot (schedule only)", "policy", "critical"),
            "FOMC Minutes": ("FOMC_MINUTES", "FOMC minutes", "policy", "high"),
            "FOMC Press Conference": (
                "FOMC_PRESS_CONFERENCE",
                "FOMC press conference",
                "policy",
                "high",
            ),
            "Beige Book": ("BEIGE_BOOK", "Beige Book", "policy", "medium"),
            "G.17 - Industrial Production and Capacity Utilization": (
                "INDUSTRIAL_PRODUCTION",
                "Industrial production release",
                "growth",
                "high",
            ),
        }
        for row in rows:
            try:
                if isinstance(row, dict) and row.get("type") in FED_SPEECH_TYPES:
                    try:
                        speech = fed_speech(row, now, self.name)
                    except ValueError:
                        speech = None  # a speech without a time is not a source fault
                    if speech is not None and speech.id not in seen:
                        seen.add(speech.id)
                        events.append(speech)
                    continue
                title = row.get("title", "").strip()
                if title not in titles:
                    continue
                month = row["month"]
                if not isinstance(month, str) or month < f"{now.year - 1}-01":
                    continue
                days = row["days"].split(",")
                if len(days) != 1:
                    raise ValueError("ambiguous monthly event identity")
                code, name, category, importance = titles[title]
                start = fed_datetime(month, days[0].strip(), row.get("time", ""))
                identity = f"{code}:{month}"
                if identity in seen:
                    raise ValueError("duplicate event identity")
                seen.add(identity)
                description = row.get("description", "")
                if not isinstance(description, str):
                    raise ValueError("invalid description")
                events.append(
                    EconomicEvent(
                        id=stable_id(self.name, identity),
                        event_code=code,
                        name=name,
                        category=category,
                        importance=importance,
                        scheduled_at=start,
                        period=None,
                        unit="release",
                        frequency="release",
                        status="scheduled" if start > now else "due",
                        provider=self.name,
                        source_event_id=identity,
                        source_url=FED_CALENDAR_URL,
                        received_at=now,
                        notes=[
                            "Schedule only; source has no stable UID or publication timestamp.",
                            "Identity uses release family and calendar month; "
                            "cross-month reschedules require review.",
                            "Time interpreted as America/New_York; no reference period inferred.",
                            description,
                        ],
                        mode="live",
                    )
                )
            except (ValueError, KeyError, TypeError, AttributeError):
                issues.append("Fed calendar entry rejected: missing time or ambiguous identity")
        announcements = payload.get("announcement", [])
        if not isinstance(announcements, list):
            raise ValueError("invalid announcements")
        if any(
            isinstance(a, dict) and a.get("month") == now.strftime("%Y-%m") for a in announcements
        ):
            issues.append(
                "Current-month Fed schedule announcement requires review; schedule may change"
            )
        if not events:
            issues.append("Fed calendar contains no supported concrete events")
        return EconomicBatch(
            provider=self.name,
            checked_at=now,
            events=events,
            issues=issues,
            checked_series_ids=[],
            status="degraded" if issues else "healthy",
        )
