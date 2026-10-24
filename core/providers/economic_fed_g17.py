"""Current official Federal Reserve G.17 headline, verified against its total table."""

from __future__ import annotations

import calendar
import hashlib
import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from core.domain.economics import EconomicEvent
from core.providers.economic_base import EconomicBatch
from core.providers.economic_calendars import stable_id
from core.providers.economic_http import HttpEconomicProvider

G17_URL = "https://www.federalreserve.gov/releases/g17/current/g17.txt"
NY = ZoneInfo("America/New_York")
MONTHS = "|".join(calendar.month_name[1:])


def parse_release(body: bytes, now: datetime) -> EconomicEvent:
    text = body.decode("utf-8")
    header = re.search(
        r"\A\s*FEDERAL RESERVE STATISTICAL RELEASE\s+G\.17 \(\d+\)\s+"
        r"For release at 9:15 a\.m\. \((EDT|EST)\)\s+(" + MONTHS + r") (\d{1,2}), (\d{4})",
        text,
    )
    if header is None:
        raise ValueError("G17 official header unavailable")
    date = datetime.strptime(f"{header[2]} {header[3]}, {header[4]}", "%B %d, %Y")
    published = date.replace(hour=9, minute=15, tzinfo=NY)
    if published.tzname() != header[1] or published > now:
        raise ValueError("G17 publication time invalid or future")
    # The official g17.txt carries only the tables (checked Oct 21, 2026); a
    # narrative headline, when present, must agree with the total-index table.
    headline = re.search(
        r"Industrial production \(IP\) (?:was (unchanged)|"
        r"(increased|rose|decreased|declined|fell|edged up|edged down) (\d+(?:\.\d+)?) percent) "
        r"in (" + MONTHS + r")\b",
        " ".join(text.split()),
    )
    month = published.month - 1 or 12
    year = published.year - (1 if month == 12 else 0)
    if headline is not None and list(calendar.month_name).index(headline[4]) != month:
        raise ValueError("G17 reference month mismatch")
    section = text.split("Industrial Production and Capacity Utilization:  Summary", 1)
    if len(section) != 2 or "Seasonally adjusted" not in section[1][:100]:
        raise ValueError("G17 summary table unavailable")
    summary = section[1].split("Capacity utilization", 1)[0]
    headers = re.findall(r"^\s*Industrial production[^\n]+$", summary, re.M)
    month_label = {6: "June", 7: "July", 9: "Sept"}.get(month, calendar.month_abbr[month])
    if len(headers) != 1 or len(re.findall(re.escape(month_label) + r"\.?\[p\]", headers[0])) != 2:
        raise ValueError("G17 current-month summary columns unavailable")
    rows = re.findall(r"^\s*Total index\s*\|([^\n]+)$", summary, re.M)
    if len(rows) != 1:
        raise ValueError("G17 total-index table ambiguous")
    columns = rows[0].split("|")
    if len(columns) != 3:
        raise ValueError("G17 total-index table layout changed")
    monthly = re.findall(r"-?(?:\d+)?\.\d+", columns[1])
    if len(monthly) != 6:
        raise ValueError("G17 total-index monthly changes unavailable")
    actual = float(monthly[-1])
    if headline is not None:
        stated = (
            0.0
            if headline[1]
            else float(headline[3])
            * (-1 if headline[2] in {"decreased", "declined", "fell", "edged down"} else 1)
        )
        if stated != actual:
            raise ValueError("G17 headline and national table disagree")
    period = f"{year:04d}-{month:02d}"
    identity = f"{published.date()}:{period}"
    return EconomicEvent(
        id=stable_id("fed_g17", identity),
        event_code="INDUSTRIAL_PRODUCTION",
        name="Industrial Production m/m",
        category="growth",
        importance="high",
        unit="percent",
        frequency="monthly",
        period=period,
        status="released",
        actual=actual,
        actual_basis="source_release",
        provider="fed_g17",
        source_event_id=identity,
        source_url=G17_URL,
        released_at=published.astimezone(UTC),
        received_at=now,
        source_revision=hashlib.sha256(body).hexdigest(),
        mode="live",
        notes=[
            "Official Federal Reserve G.17 total industrial production monthly change, "
            "seasonally adjusted.",
            "Taken from the national total-index summary table (preliminary month); "
            "a narrative headline, when published, must agree with it.",
            "Current issuer release value; original release vintage is not guaranteed.",
        ],
    )


class FedG17Provider(HttpEconomicProvider):
    provider_name = "fed_g17"

    async def collect(self, now: datetime) -> EconomicBatch:
        event = parse_release(await self.request("GET", G17_URL), now)
        return EconomicBatch(provider=self.name, checked_at=now, events=[event], status="healthy")
