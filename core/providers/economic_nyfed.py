from __future__ import annotations

from datetime import date, datetime

from core.analysis.economics import numeric, transform_observation
from core.domain.economics import MacroInput
from core.domain.macro_registry import MACRO_DEFINITIONS
from core.providers.economic_base import EconomicBatch
from core.providers.economic_http import HttpEconomicProvider

NYFED_URL = "https://markets.newyorkfed.org/api/rates/all/latest.json"


class NYFedEconomicProvider(HttpEconomicProvider):
    provider_name = "nyfed"

    async def collect(self, now: datetime) -> EconomicBatch:
        payload = await self.json_request("GET", NYFED_URL)
        records = payload["refRates"]
        if not isinstance(records, list) or len(records) > 100:
            raise ValueError("invalid rates array")
        observations, issues, checked = [], [], []
        for definition in MACRO_DEFINITIONS:
            if definition.provider != self.name:
                continue
            rate_type, _, field = definition.source_series_id.partition(":")
            field = field or "percentRate"
            try:
                inputs = []
                for row in records:
                    if row["type"] != rate_type:
                        continue
                    period = date.fromisoformat(row["effectiveDate"]).isoformat()
                    if period > now.date().isoformat():
                        raise ValueError("future effective date")
                    revision = row.get("revisionIndicator", "")
                    if not isinstance(revision, str):
                        raise ValueError("invalid revision")
                    inputs.append(
                        MacroInput(
                            source_series_id=definition.source_series_id,
                            provider=self.name,
                            period=period,
                            value=numeric(row.get(field)),
                            unit="percent",
                            received_at=now,
                            source_revision=revision or None,
                        )
                    )
                observation = transform_observation(
                    definition,
                    inputs,
                    now,
                    NYFED_URL,
                    [
                        "Effective date only; publication timestamp unavailable.",
                        definition.info or "Daily reference rate, not a live quote.",
                    ],
                )
                if observation is not None and observation.value is not None:
                    observations.append(observation)
                    checked.append(definition.id)
                if observation is None or observation.value is None:
                    issues.append(f"{definition.id}: rate unavailable")
            except (ValueError, KeyError, TypeError):
                issues.append(f"{definition.id}: invalid rate payload")
        return EconomicBatch(
            provider=self.name,
            checked_at=now,
            observations=observations,
            issues=issues,
            checked_series_ids=checked,
            status="degraded" if issues else "healthy",
        )
