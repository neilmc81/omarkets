"""Date-verified official weekly claims, parsed without paid inference."""
from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from core.domain.economics import EconomicEvent
from core.providers.economic_base import EconomicBatch
from core.providers.economic_calendars import stable_id
from core.providers.economic_http import HttpEconomicProvider

CLAIMS_URL = "https://www.dol.gov/ui/data.pdf"


def parse_claims(text: str, now: datetime) -> EconomicEvent:
    text = " ".join(text.split())
    date = re.search(
        r"8:30\s+A\.M\.\s*\(Eastern\)\s+\w+,\s+(\w+\s+\d{1,2},\s+\d{4})", text
    )
    values = re.search(
        r"In the week ending (\w+ \d{1,2}), the advance figure for seasonally adjusted "
        r"initial claims was ([\d,]+)", text
    )
    if not date or not values or "UNEMPLOYMENT INSURANCE WEEKLY CLAIMS" not in text:
        raise ValueError("claims release identity or seasonally adjusted value unavailable")
    publication = datetime.strptime(date[1], "%B %d, %Y").replace(
        hour=8, minute=30, tzinfo=ZoneInfo("America/New_York")
    )
    if publication > now:
        raise ValueError("claims release is still embargoed")
    period = datetime.strptime(f"{values[1]}, {publication.year}", "%B %d, %Y")
    if period.date() > publication.date():
        period = period.replace(year=period.year - 1)
    if not 0 <= (publication.date() - period.date()).days <= 14:
        raise ValueError("claims reference week does not match publication")
    previous = re.search(r"from ([\d,]+) to ([\d,]+)\.", text)
    identity = f"INITIAL_CLAIMS:{publication.date().isoformat()}"
    return EconomicEvent(
        id=stable_id("dol_claims", identity), event_code="INITIAL_CLAIMS",
        name="Unemployment Claims", category="employment", importance="high",
        scheduled_at=publication, released_at=publication, received_at=now,
        period=period.date().isoformat(), unit="value", frequency="weekly",
        status="released", actual=float(values[2].replace(",", "")),
        actual_basis="source_release",
        previous=float(previous[1].replace(",", "")) if previous else None,
        revised_previous=float(previous[2].replace(",", "")) if previous else None,
        provider="dol_claims", source_event_id=identity, source_url=CLAIMS_URL,
        source_revision=hashlib.sha256(text.encode()).hexdigest()[:16], mode="live",
        notes=["Official DOL seasonally adjusted advance initial claims release.",
               "Publication date and reference week verified from the release text.",
               "Latest PDF may lag; an older publication is never relabelled as today's release."],
    )


async def extract_first_page(body: bytes) -> str:
    if not body.startswith(b"%PDF-") or len(body) > 2_000_000:
        raise ValueError("invalid or oversized claims PDF")
    process = await asyncio.create_subprocess_exec(
        "pdftotext", "-f", "1", "-l", "1", "-", "-",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(body), timeout=5)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode or len(output) > 100_000:
        raise ValueError("claims PDF extraction failed")
    return output.decode("utf-8")


class DOLClaimsProvider(HttpEconomicProvider):
    provider_name = "dol_claims"

    async def collect(self, now: datetime) -> EconomicBatch:
        event = parse_claims(await extract_first_page(await self.request("GET", CLAIMS_URL)), now)
        return EconomicBatch(provider=self.name, checked_at=now, events=[event],
                             checked_series_ids=[], status="healthy")
