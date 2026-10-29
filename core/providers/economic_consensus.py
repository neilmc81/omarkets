from __future__ import annotations

from datetime import datetime, timedelta

from core.domain.economics import EconomicEvent
from core.providers.economic_base import EconomicBatch
from core.providers.economic_calendars import stable_id
from core.providers.economic_http import HttpEconomicProvider

FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

EVENT_CODES: tuple[tuple[str, str, str, str], ...] = (
    ("core cpi", "CORE_CPI", "inflation", "critical"),
    ("cpi", "CPI", "inflation", "critical"),
    # ADP titles may also say "Non-Farm"; classify the issuer first so the
    # private estimate is never presented as the official payroll release.
    ("adp", "ADP", "employment", "medium"),
    ("non-farm", "PAYROLLS", "employment", "critical"),
    ("nonfarm", "PAYROLLS", "employment", "critical"),
    ("average hourly", "AHE", "employment", "high"),
    ("unemployment rate", "UNEMPLOYMENT", "employment", "high"),
    ("unemployment claims", "INITIAL_CLAIMS", "employment", "high"),
    ("jolts", "JOLTS", "employment", "high"),
    ("core pce", "CORE_PCE", "inflation", "critical"),
    ("pce price", "PCE", "inflation", "critical"),
    ("core ppi", "CORE_PPI", "inflation", "high"),
    ("ppi", "PPI", "inflation", "high"),
    ("federal funds rate", "FOMC", "policy", "critical"),
    ("fomc statement", "FOMC_STATEMENT", "policy", "critical"),
    ("fomc economic projections", "SEP", "policy", "high"),
    ("fomc press conference", "FOMC_PRESS_CONFERENCE", "policy", "high"),
    # Price inflation is not the real output-growth release at the same slot.
    ("gdp price", "GDP_PRICE_INDEX", "inflation", "high"),
    ("gdp", "GDP", "growth", "high"),
    ("core retail sales", "CORE_RETAIL_SALES", "growth", "high"),
    ("retail sales", "RETAIL_SALES", "growth", "high"),
    ("industrial production", "INDUSTRIAL_PRODUCTION", "growth", "high"),
    ("durable goods", "DURABLE_GOODS", "growth", "medium"),
    ("housing starts", "HOUSING_STARTS", "housing", "medium"),
    ("building permits", "BUILDING_PERMITS", "housing", "medium"),
    ("existing home sales", "EXISTING_HOME_SALES", "housing", "medium"),
    ("new home sales", "NEW_HOME_SALES", "housing", "medium"),
    ("cb consumer confidence", "CONSUMER_CONFIDENCE", "growth", "medium"),
    ("ism manufacturing prices", "ISM_MFG_PRICES", "inflation", "high"),
    ("ism manufacturing", "ISM_MFG", "growth", "high"),
    ("ism services", "ISM_SERVICES", "growth", "high"),
    ("ism non-manufacturing", "ISM_SERVICES", "growth", "high"),
)

IMPACT_MAP = {"high": "high", "medium": "medium", "low": "low", "holiday": "low"}
SCALES = {"K": 1e3, "M": 1e6, "B": 1e9}


def classify(title: str) -> tuple[str, str, str] | None:
    lowered = title.lower()
    for needle, code, category, importance in EVENT_CODES:
        if needle in lowered:
            return code, category, importance
    return None


def parse_number(value: object) -> tuple[float | None, str | None]:
    if value is None:
        return None, None
    text = str(value).strip().replace(",", "")
    if not text or text in ("-", "\u2014"):
        return None, None
    scale = 1.0
    unit = "value"
    if text.endswith("%"):
        text = text[:-1]
        unit = "percent"
    elif text[-1].upper() in SCALES:
        scale = SCALES[text[-1].upper()]
        text = text[:-1]
    try:
        number = float(text) * scale
    except ValueError:
        return None, None
    if number != number or number in (float("inf"), float("-inf")):
        return None, None
    return number, unit


class ForexFactoryProvider(HttpEconomicProvider):
    provider_name = "consensus"

    async def collect(self, now: datetime) -> EconomicBatch:
        payload = await self.json_request("GET", FF_URL)
        if not isinstance(payload, list) or len(payload) > 1000:
            raise ValueError("invalid calendar array")
        events, issues, seen = [], [], set()
        for row in payload:
            try:
                if not isinstance(row, dict):
                    raise ValueError("invalid entry")
                if str(row.get("country", "")).upper() != "USD":
                    continue
                title = str(row.get("title", "")).strip()
                if not title:
                    continue
                classified = classify(title)
                if classified is None:
                    continue
                code, category, importance = classified
                impact = IMPACT_MAP.get(str(row.get("impact", "")).strip().lower())
                if impact is None:
                    continue
                moment = datetime.fromisoformat(str(row["date"]))
                if moment.tzinfo is None:
                    raise ValueError("naive schedule")
                actual, actual_unit = parse_number(row.get("actual"))
                consensus, consensus_unit = parse_number(row.get("forecast"))
                previous, previous_unit = parse_number(row.get("previous"))
                if moment > now + timedelta(days=31) or moment < now - timedelta(days=31):
                    continue
                unit = actual_unit or consensus_unit or previous_unit or "value"
                identity = f"{code}:{moment.isoformat()}:{title[:40]}"
                if identity in seen:
                    continue
                seen.add(identity)
                scheduled = moment > now
                released = not scheduled and actual is not None
                notes = [
                    "Consensus calendar source; forecast/previous as published.",
                    "Forecast is a market consensus, not an official statistic.",
                ]
                events.append(
                    EconomicEvent(
                        id=stable_id(self.name, identity),
                        event_code=code,
                        name=title,
                        country="US",
                        category=category,
                        importance=importance,  # type: ignore[arg-type]
                        scheduled_at=moment,
                        timezone="America/New_York",
                        period=None,
                        unit=unit,
                        frequency="release",
                        status="scheduled" if scheduled else ("released" if released else "due"),
                        actual=None if scheduled else actual,
                        consensus=consensus,
                        previous=previous,
                        provider=self.name,
                        source_event_id=identity,
                        source_url=FF_URL,
                        released_at=moment if released else None,
                        received_at=now,
                        notes=notes,
                        mode="live",
                    )
                )
            except (ValueError, KeyError, TypeError, AttributeError):
                issues.append("Consensus calendar entry rejected: invalid payload")
        if not events:
            issues.append("Consensus calendar returned no supported US events")
        return EconomicBatch(
            provider=self.name,
            checked_at=now,
            events=events,
            issues=issues,
            checked_series_ids=[],
            status="degraded" if issues else "healthy",
        )
