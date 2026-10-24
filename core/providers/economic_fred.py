from __future__ import annotations

import json
from datetime import date, datetime

from core.analysis.economics import numeric, transform_history
from core.domain.economics import MacroInput
from core.domain.macro_registry import MACRO_DEFINITIONS
from core.providers.economic_base import EconomicBatch
from core.providers.economic_http import HttpEconomicProvider, sensitive_http_logs

FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
FRED_MAINTENANCE = "FRED is down for maintenance (St. Louis Fed holding page); retried next poll."
FRED_INPUT_UNITS = {
    "PCEPI": "index points",
    "PCEPILFE": "index points",
    "A191RL1Q225SBEA": "percent",
    "RSAFS": "million USD",
    "RSFSXMV": "million USD",
    "INDPRO": "index points",
    "ICSA": "claims",
    "CCSA": "claims",
    "PPIFIS": "index points",
    "PPIFES": "index points",
    "HOUST": "thousand units SAAR",
    "PERMIT": "thousand units SAAR",
    "HSN1F": "thousand units SAAR",
    "DGORDER": "million USD",
    "GACDISA066MSFRBNY": "index points",
    "GACDFSA066MSFRBPHI": "index points",
}


def is_fred_maintenance(body: bytes) -> bool:
    """During maintenance the St. Louis Fed answers every URL, API included, with
    HTTP 200 and an HTML holding page (seen Oct 21-22, 2026)."""
    return b"<title>Site Maintenance | St. Louis Fed" in body[:4000]


class FREDEconomicProvider(HttpEconomicProvider):
    provider_name = "fred"

    def __init__(self, *args, api_key: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._api_key = api_key

    async def request(self, method: str, url: str, **kwargs) -> bytes:
        with sensitive_http_logs():
            return await super().request(method, url, **kwargs)

    async def aclose(self) -> None:
        with sensitive_http_logs():
            await super().aclose()

    async def collect(self, now: datetime) -> EconomicBatch:
        if not self._api_key:
            return EconomicBatch(
                provider=self.name,
                checked_at=now,
                status="not_configured",
                issues=["FRED API key not configured; no request sent."],
                checked_series_ids=[],
            )
        observations, issues, checked = [], [], []
        for definition in MACRO_DEFINITIONS:
            if definition.provider != self.name:
                continue
            source = definition.source_series_id
            try:
                body = await self.request(
                    "GET",
                    FRED_URL,
                    params={
                        "api_key": self._api_key,
                        "series_id": source,
                        "file_type": "json",
                        "sort_order": "desc",
                        "limit": "30",
                    },
                )
                if is_fred_maintenance(body):
                    issues.append(FRED_MAINTENANCE)
                    break
                rows = json.loads(body)["observations"]
                if not isinstance(rows, list) or len(rows) > 30:
                    raise ValueError("invalid observations array")
                inputs = []
                for row in rows:
                    day = date.fromisoformat(row["date"])
                    if day > now.date():
                        raise ValueError("future reference period")
                    if definition.frequency == "monthly":
                        period = day.strftime("%Y-%m")
                    elif definition.frequency == "quarterly":
                        period = f"{day.year}-Q{(day.month - 1) // 3 + 1}"
                    else:
                        period = day.isoformat()
                    start = date.fromisoformat(row["realtime_start"])
                    end = date.fromisoformat(row["realtime_end"])
                    if start > end:
                        raise ValueError("invalid query window")
                    inputs.append(
                        MacroInput(
                            source_series_id=source,
                            provider=self.name,
                            period=period,
                            value=numeric(row["value"]),
                            unit=FRED_INPUT_UNITS[source],
                            received_at=now,
                        )
                    )
                history = transform_history(
                    definition,
                    inputs,
                    now,
                    f"https://fred.stlouisfed.org/series/{source}",
                    [
                        definition.info or "",
                        "FRED current vintage; realtime dates are not publication instants.",
                        "True vintage unavailable; realtime_start/end is the query "
                        "window, not a revision.",
                    ],
                )
                if history:
                    observations.extend(history)
                    checked.append(definition.id)
                    if history[-1].value is None:
                        issues.append(f"{definition.id}: value unavailable")
                else:
                    issues.append(f"{definition.id}: value unavailable")
            except Exception:
                issues.append(f"{definition.id}: request or validation failed")
        return EconomicBatch(
            provider=self.name,
            checked_at=now,
            observations=observations,
            issues=issues,
            checked_series_ids=checked,
            status="degraded" if issues else "healthy",
        )
