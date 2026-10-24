from __future__ import annotations

from datetime import datetime, timedelta

from core.domain.economics import EconomicEvent, MacroObservation, MacroProvenance
from core.providers.economic_base import EconomicBatch, EconomicProvider
from core.providers.economic_http import Clock, utc_now


class DemoEconomicProvider(EconomicProvider):
    def __init__(self, clock: Clock = utc_now):
        self.clock = clock
        self._start: datetime | None = None

    @property
    def name(self) -> str:
        return "demo"

    async def fetch(self) -> EconomicBatch:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must be timezone aware")
        if self._start is None:
            self._start = now
        scheduled = self._start + timedelta(seconds=30)
        released = now >= scheduled
        revised = now >= scheduled + timedelta(seconds=30)
        value = 0.3 if revised else 0.2
        event = EconomicEvent(
            id="demo:cpi:2026-08",
            event_code="CPI_MOM",
            name="DEMO CPI release",
            category="inflation",
            importance="high",
            scheduled_at=scheduled,
            period="2026-08",
            unit="percent",
            frequency="monthly",
            status="released" if released else "scheduled",
            actual=value if released else None,
            previous=0.1,
            revised_previous=0.15 if revised else None,
            provider=self.name,
            source_event_id="demo:cpi:2026-08",
            received_at=now,
            released_at=scheduled if released else None,
            revision=2 if revised else 1,
            source_revision="2" if revised else "1",
            mode="demo",
            notes=["Synthetic demonstration only; never production data."],
        )
        observations = []
        if released:
            observations.append(
                MacroObservation(
                    id="CPI_MOM",
                    period="2026-08",
                    value=value,
                    unit="percent",
                    source_series_id="demo:CPI_MOM",
                    provider=self.name,
                    received_at=now,
                    released_at=scheduled,
                    revision=2 if revised else 1,
                    notes=["Synthetic demonstration only."],
                    metadata=MacroProvenance(source_revision="2" if revised else "1"),
                )
            )
        return EconomicBatch(
            provider=self.name,
            checked_at=now,
            status="healthy",
            events=[event],
            observations=observations,
            checked_series_ids=["CPI_MOM"] if observations else [],
        )
