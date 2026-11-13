from __future__ import annotations

import json
import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from core.analysis.economics import numeric, transform_history
from core.domain.economics import MacroInput
from core.domain.macro_registry import MACRO_DEFINITIONS
from core.providers.economic_base import EconomicBatch
from core.providers.economic_http import HttpEconomicProvider

BLS_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
_ET = ZoneInfo("America/New_York")
# BLS publishes at 08:30 ET (CPI, jobs, PPI) and 10:00 ET (JOLTS). Its API
# often fails or serves incomplete data for a moment right at the bell, so a
# failed or issue-bearing check from just before a release time until shortly
# after it is retried a few times before it counts as an issue.
_RELEASE_TIMES = (time(8, 30), time(10, 0))
_BELL_BEFORE = timedelta(minutes=2)
_BELL_AFTER = timedelta(minutes=15)
_BELL_RETRY_SECONDS = 60
_BELL_RETRIES = 4
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


def near_release(now: datetime) -> bool:
    """Whether ``now`` is at a BLS release bell on a weekday (ET)."""
    local = now.astimezone(_ET)
    if local.weekday() >= 5:
        return False
    for release in _RELEASE_TIMES:
        bell = datetime.combine(local.date(), release, tzinfo=_ET)
        if bell - _BELL_BEFORE <= local < bell + _BELL_AFTER:
            return True
    return False


def _year_ago_gap(definition, observation) -> str | None:
    """Precise note when only the year-ago month of a y/y series has no value."""
    if definition.transform != "yoy" or len(observation.metadata.inputs) != 2:
        return None
    current, previous = observation.metadata.inputs
    if current.value is None or previous.value is not None:
        return None
    return f"Year-ago month {previous.period} is not published by BLS; year over year unavailable."


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
        # A failed request or a batch with issues (missing, invalid or not yet
        # filled series) is retried at the bell; the last result stands.
        retries = 0
        while True:
            try:
                batch = await self._collect_once(now)
            except Exception:
                if not await self._wait_to_retry(retries):
                    raise
            else:
                if not batch.issues or not await self._wait_to_retry(retries):
                    return batch
            retries += 1
            now = self.clock()

    async def _wait_to_retry(self, retries: int) -> bool:
        """Sleep before another try if still at a release bell (fresh clock)."""
        if retries >= _BELL_RETRIES or not near_release(self.clock()):
            return False
        await self.sleep(_BELL_RETRY_SECONDS)
        return True

    async def _collect_once(self, now: datetime) -> EconomicBatch:
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
                            latest = history[-1]
                            if latest.value is None:
                                gap = _year_ago_gap(definition, latest)
                                if gap is None:
                                    issues.append(
                                        f"{definition.id}: required period value unavailable"
                                    )
                                else:
                                    # A year-over-year gap is a fixed hole in BLS history, not a
                                    # fault: no retry, no degraded provider. Note it on the
                                    # observation only.
                                    observations[-1] = latest.model_copy(
                                        update={"notes": [*latest.notes, gap]}
                                    )
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
