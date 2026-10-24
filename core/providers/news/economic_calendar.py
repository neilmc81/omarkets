"""Economic-calendar source bridging the Phase 2 calendar into events.

Phase 2 already owns scheduled macro releases and observed releases. Phase 5
does not recompute them; it adapts them into first-class
:class:`~core.domain.news.MarketEvent` objects so the unified event stream and
the News/Events view include the economic calendar without duplicating logic.

The adapter is given a callable returning :class:`~core.domain.economics.EconomicEvent`
objects. This keeps the news pipeline independent of the economics service while
remaining typed at the boundary.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from core.domain.economics import EconomicEvent
from core.domain.news import (
    EconomicCalendarDetail,
    SourceItem,
    SourceMetadata,
    SourceType,
)
from core.providers.news.base import NewsSource


class EconomicCalendarSource(NewsSource):
    """Adapts externally supplied economic-calendar events into source items.

    ``events`` is a callable so the source always reads the current calendar at
    fetch time rather than a stale snapshot captured at construction.
    """

    source_type = SourceType.ECONOMIC_CALENDAR

    def __init__(
        self,
        *,
        events: Callable[[], Sequence[EconomicEvent]],
        metadata: SourceMetadata,
        clock: Callable[[], datetime],
        enabled: bool = True,
    ) -> None:
        super().__init__(clock=clock, enabled=enabled)
        self._events = events
        self._metadata = metadata

    @property
    def name(self) -> str:
        return self._metadata.name

    @property
    def metadata(self) -> SourceMetadata:
        return self._metadata

    async def fetch(self) -> list[SourceItem]:
        now = self.clock()
        items: list[SourceItem] = []
        seen: set[str] = set()
        for event in self._events():
            # Calendar slots and undated current-vintage observations are not
            # fresh release news. Keep them readable in Calendar/Macro without
            # paying to assess a publication that has not been confirmed.
            if (
                event.released_at is None or event.released_at > now
                or event.actual_basis == "current_vintage" or event.id.startswith("observed:")
            ):
                continue
            source_id = f"{event.provider}:{event.source_event_id}:{event.revision}"
            if source_id in seen:
                continue
            seen.add(source_id)
            moment = event.released_at
            items.append(
                SourceItem(
                    source=self.name,
                    source_type=self.source_type,
                    source_id=source_id,
                    source_url=event.source_url,
                    headline=event.name,
                    summary=(
                        f"{event.country} {event.event_code} "
                        f"({event.period or 'period unavailable'})"
                    ),
                    published_at=moment,
                    received_at=now,
                    language="en",
                    event_type=event.category.value,
                    metadata={
                        "event_code": event.event_code,
                        "economic_event_id": event.id,
                        "economic_revision": event.revision,
                        "country": event.country,
                        "period": event.period,
                        "unit": event.unit,
                        "importance": event.importance.value,
                        "status": event.status.value,
                        "provider": event.provider,
                        "mode": event.mode.value,
                        "observed_release": event.id.startswith("observed:"),
                    },
                    raw={
                        "provider": event.provider,
                        "source_event_id": event.source_event_id,
                        "event_code": event.event_code,
                        "scheduled_at": event.scheduled_at.isoformat()
                        if event.scheduled_at
                        else None,
                        "released_at": event.released_at.isoformat() if event.released_at else None,
                    },
                    economic=EconomicCalendarDetail(
                        event_code=event.event_code,
                        name=event.name,
                        country=event.country,
                        period=event.period,
                        unit=event.unit,
                        importance=event.importance.value,
                        status=event.status.value,
                        actual=event.actual,
                        consensus=event.consensus,
                        previous=event.previous,
                        revised_previous=event.revised_previous,
                    ),
                )
            )
        return items


def _utc_now() -> datetime:  # pragma: no cover - default clock
    return datetime.now(UTC)


__all__ = ["EconomicCalendarSource"]
