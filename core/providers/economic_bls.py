from __future__ import annotations

import json
import re
from datetime import datetime

from core.analysis.economics import numeric, transform_history
from core.domain.economics import MacroInput
from core.domain.macro_registry import MACRO_DEFINITIONS
from core.providers.economic_base import EconomicBatch
from core.providers.economic_http import HttpEconomicProvider

BLS_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
BLS_INPUT_UNITS = {
    "CUSR0000SA0": "index points",
    "CUUR0000SA0": "index points",
    "CUSR0000SA0L1E": "index points",
    "CUUR0000SA0L1E": "index points",
    "CES0000000001": "thousand jobs",
    "LNS14000000": "percent",
    "CES0500000003": "USD per hour",
    "JTS000000000000000JOL": "thousand jobs",
    "WPSFD4": "index points",
    "WPUFD4": "index points",
    "WPSFD49104": "index points",
    "WPUFD49104": "index points",
}


class BLSEconomicProvider(HttpEconomicProvider):
    provider_name = "bls"

    def __init__(self, *args, api_key: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._api_key = api_key

    async def _post(self, now: datetime, with_key: bool):
        body = {
            "seriesid": list(BLS_INPUT_UNITS),
            "startyear": str(now.year - 2),
            "endyear": str(now.year),
        }
        if with_key and self._api_key:
            body["registrationkey"] = self._api_key
        return await self.json_request("POST", BLS_URL, json=body)

    async def collect(self, now: datetime) -> EconomicBatch:
        payload = await self._post(now, with_key=True)
        if payload.get("status") != "REQUEST_SUCCEEDED":
            messages = " ".join(str(item) for item in payload.get("message", []))
            if self._api_key and "key" in messages.lower():
                payload = await self._post(now, with_key=False)
        if payload.get("status") != "REQUEST_SUCCEEDED":
            raise ValueError("BLS rejected query")
        observations, issues, checked = [], [], []
        series = payload["Results"]["series"]
        if not isinstance(series, list) or len(series) > len(BLS_INPUT_UNITS):
            raise ValueError("invalid series array")
        seen = set()
        for entry in series:
            source = entry.get("seriesID")
            if source not in BLS_INPUT_UNITS or source in seen:
                raise ValueError("unexpected or duplicate source series")
            seen.add(source)
            try:
                inputs = []
                for row in entry["data"]:
                    month = row["period"]
                    if month == "M13":
                        continue
                    if not re.fullmatch(r"M(0[1-9]|1[0-2])", month):
                        raise ValueError("invalid monthly period")
                    period = f"{row['year']}-{month[1:]}"
                    if period > now.strftime("%Y-%m"):
                        raise ValueError("future reference month")
                    footnotes = row["footnotes"]
                    if not isinstance(footnotes, list) or any(
                        not isinstance(f, dict) or any(not isinstance(v, str) for v in f.values())
                        for f in footnotes
                    ):
                        raise ValueError("invalid footnotes")
                    revision = json.dumps(footnotes, sort_keys=True, separators=(",", ":"))
                    inputs.append(
                        MacroInput(
                            source_series_id=source,
                            provider=self.name,
                            period=period,
                            value=numeric(row["value"]),
                            unit=BLS_INPUT_UNITS[source],
                            received_at=now,
                            source_revision=revision,
                        )
                    )
                for definition in MACRO_DEFINITIONS:
                    if definition.provider == self.name and definition.source_series_id == source:
                        history = transform_history(
                            definition,
                            inputs,
                            now,
                            BLS_URL,
                            [definition.info or "", "Publication time not supplied by BLS API."],
                        )
                        if history:
                            observations.extend(history)
                            checked.append(definition.id)
                            if history[-1].value is None:
                                issues.append(f"{definition.id}: required period value unavailable")
                        else:
                            issues.append(f"{definition.id}: empty series")
            except (ValueError, KeyError, TypeError):
                issues.append(f"{source}: invalid series payload")
        issues.extend(f"{s}: series missing" for s in sorted(set(BLS_INPUT_UNITS) - seen))
        return EconomicBatch(
            provider=self.name,
            checked_at=now,
            status="degraded" if issues else "healthy",
            observations=observations,
            issues=issues,
            checked_series_ids=checked,
        )
