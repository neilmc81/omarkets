"""News / geopolitical event ingestion service.

Owns the deterministic Phase 5 pipeline::

    SOURCE -> FETCH -> NORMALIZE -> DEDUPLICATE -> STORE -> EVENT BUS

Each source adapter is polled independently on its own schedule. One failing
source can never stop the others: failures are recorded as source health and the
service retains all cached state.

The service is the single integration point for Phase 6. It exposes
:meth:`NewsService.subscribe_events` for in-process consumers and
:meth:`NewsService.register_processor` to attach a deterministic or future
classifier that receives every newly stored canonical event. Phase 6 will
implement that processor; Phase 5 attaches nothing and makes no judgement about
an event's importance, relevance, novelty or market impact.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta

from pydantic import BaseModel, Field

from core.domain.news import (
    IngestionStatus,
    MarketEvent,
    RawEvent,
    SourceHealth,
    SourceItem,
    SourceMetadata,
    SourceOccurrence,
    SourceStatus,
    SourceType,
)
from core.domain.sessions import closed_market_delay
from core.ingest.dedupe import DEFAULT_SIMILARITY_THRESHOLD, DEFAULT_WINDOW, find_match
from core.ingest.entities import filter_instruments
from core.ingest.normalize import normalize_item
from core.providers.news.base import Clock, NewsSource, content_hash, utc_now
from core.providers.news.catalog import DEFAULT_ENABLED_SOURCES, SOURCE_DEFINITIONS, build_source
from core.services.market_state import SubscriptionClosed
from core.storage.events import SQLiteEventRepository

logger = logging.getLogger(__name__)

#: A source poll is logged as slow only past these limits (milliseconds).
SLOW_FETCH_MS = 5000.0
SLOW_PROCESS_MS = 2000.0


def _normalize_batch(
    items: Sequence[SourceItem], metadata: SourceMetadata,
) -> list[MarketEvent | None]:
    """Pure worker operation; never access storage or service callbacks."""
    results: list[MarketEvent | None] = []
    for item in items:
        try:
            results.append(normalize_item(item, metadata))
        except Exception:
            results.append(None)
    return results

#: Event-bus message types published to WebSocket/API subscribers.
NEWS_MESSAGE_TYPES: tuple[str, ...] = (
    "market_event_created",
    "market_event_updated",
    "source_status_changed",
)


class NewsMessageData(BaseModel):
    ids: list[str]
    occurred_at: datetime


class NewsMessage(BaseModel):
    type: str
    data: NewsMessageData


class NewsMailbox:
    """Coalescing mailbox for news invalidation messages."""

    def __init__(self, max_ids: int = 100) -> None:
        if max_ids < 1:
            raise ValueError("max_ids must be positive")
        self.max_ids = max_ids
        self.pending: dict[str, NewsMessage] = {}
        self.ready = asyncio.Event()
        self.closed = False

    def offer(self, message: NewsMessage) -> None:
        if self.closed:
            return
        previous = self.pending.get(message.type)
        ids = set(message.data.ids)
        if previous:
            ids.update(previous.data.ids)
        self.pending[message.type] = NewsMessage(
            type=message.type,
            data=NewsMessageData(
                ids=["*"] if "*" in ids or len(ids) > self.max_ids else sorted(ids),
                occurred_at=max(previous.data.occurred_at, message.data.occurred_at)
                if previous
                else message.data.occurred_at,
            ),
        )
        self.ready.set()

    async def get(self) -> NewsMessage:
        while not self.pending:
            await self.ready.wait()
            if self.closed:
                raise SubscriptionClosed
        key = next(iter(self.pending))
        result = self.pending.pop(key)
        if not self.pending:
            self.ready.clear()
        return result

    def close(self) -> None:
        self.closed = True
        self.pending.clear()
        self.ready.set()


class NewsPolling(BaseModel):
    """Per-source polling policy."""

    default_seconds: float = Field(default=300.0, ge=30, allow_inf_nan=False)
    overrides: dict[str, float] = Field(default_factory=dict)
    min_seconds: float = Field(default=30.0, ge=5, allow_inf_nan=False)

    @classmethod
    def from_catalog(
        cls, default_seconds: float, overrides: dict[str, float] | None = None
    ) -> NewsPolling:
        """Policy where a source's catalog ``poll_seconds`` can only slow it down.

        A source is never polled more often than ``default_seconds`` or its
        catalog interval (e.g. Department of Energy every 15 minutes, not 5).
        Explicit ``overrides`` win.
        """
        catalog = {
            definition.name: definition.poll_seconds
            for definition in SOURCE_DEFINITIONS
            if definition.poll_seconds > default_seconds
        }
        return cls(default_seconds=default_seconds, overrides=catalog | (overrides or {}))

    def interval(self, name: str) -> float:
        value = self.overrides.get(name)
        if value is None:
            return self.default_seconds
        if value < self.min_seconds:
            raise ValueError(
                f"poll interval for {name!r} must be at least {self.min_seconds} seconds"
            )
        return value


class IngestionStats(BaseModel):
    """Per-run counters for observability."""

    fetched: int = 0
    normalized: int = 0
    duplicates: int = 0
    stored: int = 0
    rejected: int = 0
    failed: int = 0
    duration_seconds: float = 0.0


class IngestionResult(BaseModel):
    """The outcome of processing a batch for one source in one run."""

    source: str
    checked_at: datetime
    status: SourceStatus
    stats: IngestionStats = Field(default_factory=IngestionStats)
    events: list[MarketEvent] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)


def build_sources(
    *,
    enabled: Iterable[str] | None = None,
    disabled: Iterable[str] | None = None,
    overrides: dict[str, float] | None = None,
    clock: Clock = utc_now,
) -> list[NewsSource]:
    """Construct the configured source set from the built-in catalog."""
    enabled_set = set(enabled) if enabled is not None else set(DEFAULT_ENABLED_SOURCES)
    disabled_set = set(disabled or ())
    overrides = overrides or {}
    sources: list[NewsSource] = []
    for definition in SOURCE_DEFINITIONS:
        is_enabled = definition.name in enabled_set and definition.name not in disabled_set
        source = build_source(definition, clock=clock, enabled=is_enabled)
        sources.append(source)
    return sources


class NewsService:
    """Deterministic news ingestion pipeline and event bus."""

    def __init__(
        self,
        *,
        repository: SQLiteEventRepository | None = None,
        sources: Sequence[NewsSource] = (),
        polling: NewsPolling | None = None,
        enabled: bool = True,
        clock: Clock = utc_now,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        dedupe_window=DEFAULT_WINDOW,
    ) -> None:
        self.repository = repository
        self.sources = tuple(sources)
        names = [source.name for source in self.sources]
        if len(set(names)) != len(names):
            raise ValueError("news source names must be unique")
        self.polling = polling or NewsPolling()
        self.enabled = enabled
        self.clock = clock
        self.similarity_threshold = similarity_threshold
        self.dedupe_window = dedupe_window
        self._tasks: list[asyncio.Task] = []
        self._mailboxes: set[NewsMailbox] = set()
        self._processors: list[Callable[[MarketEvent], None]] = []
        self._events: dict[str, MarketEvent] = {}
        self._recent: list[MarketEvent] = []
        self._health_status: dict[str, SourceStatus] = {}
        self._stats: dict[str, IngestionStats] = {}
        self._started = False
        self._closed = False
        for source in self.sources:
            self.polling.interval(source.name)
        for name in self.polling.overrides:
            self.polling.interval(name)

    # --- subscription / integration points ---------------------------

    @property
    def subscriber_count(self) -> int:
        return len(self._mailboxes)

    def subscribe(self) -> NewsMailbox:
        mailbox = NewsMailbox()
        self._mailboxes.add(mailbox)
        return mailbox

    def unsubscribe(self, mailbox: NewsMailbox) -> None:
        self._mailboxes.discard(mailbox)
        mailbox.close()

    def subscribe_events(self) -> list[MarketEvent]:
        """Return a snapshot of the current canonical event cache.

        Phase 6 in-process consumers can poll this; WebSocket/API consumers use
        :meth:`subscribe` for invalidations and REST for recovery.
        """
        return list(self._recent)

    def register_processor(self, processor: Callable[[MarketEvent], None]) -> None:
        """Attach a consumer invoked once per newly stored canonical event.

        This is the Phase 6 seam. The processor receives a fully normalized,
        deduplicated, persisted event and is responsible for its own
        classification/routing. Phase 5 registers none.
        """
        self._processors.append(processor)

    def _run_processors(self, event: MarketEvent) -> None:
        for processor in tuple(self._processors):
            try:
                processor(event)
            except Exception:  # pragma: no cover - consumer isolation
                logger.exception("news processor failed for event %s", event.id)

    def _publish(self, kind: str, ids: list[str]) -> None:
        if not ids:
            return
        message = NewsMessage(
            type=kind,
            data=NewsMessageData(ids=sorted(set(ids)), occurred_at=self.clock()),
        )
        for mailbox in self._mailboxes:
            mailbox.offer(message)

    # --- lifecycle ----------------------------------------------------

    def _seed(self) -> None:
        if self.repository is None:
            return
        self._recent = self.repository.recent_events(limit=1000)
        self._events = {event.id: event for event in self._recent}

    async def start(self) -> None:
        if self._started:
            return
        self._closed = False
        try:
            if self.repository is not None:
                self.repository.initialize()
                self._seed()
            self._started = True
            if self.enabled:
                if self.repository is None:
                    raise ValueError("enabled news ingestion requires a repository")
                self._tasks = [
                    asyncio.create_task(self._poll(source))
                    for source in self.sources
                    if source.enabled
                ]
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._started = False
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for mailbox in tuple(self._mailboxes):
            self.unsubscribe(mailbox)
        for source in self.sources:
            try:
                await source.aclose()
            except Exception:
                pass
        if self.repository is not None:
            self.repository.close()

    async def _poll(self, source: NewsSource) -> None:
        interval = self.polling.interval(source.name)
        while True:
            await self.ingest_source(source)
            quiet_delay = closed_market_delay(self.clock())
            # A source that was rate limited (HTTP 429/Retry-After) waits longer.
            backoff = getattr(source, "backoff_seconds", 0.0)
            await asyncio.sleep(max(quiet_delay if quiet_delay > 0 else interval, backoff))

    # --- pipeline -----------------------------------------------------

    async def ingest_source(
        self, source: NewsSource, *, dispatch_processors: bool = True,
    ) -> IngestionResult:
        """Fetch, normalize, dedupe and store one source. Failure-isolated."""
        started = self.clock()
        monotonic_started = time.perf_counter()
        if not source.enabled or self.repository is None:
            return IngestionResult(
                source=source.name,
                checked_at=started,
                status=SourceStatus.DISABLED,
                issues=["source disabled or repository unavailable"],
            )
        try:
            items = await source.fetch()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result = IngestionResult(
                source=source.name,
                checked_at=started,
                status=source.health().status,
                stats=IngestionStats(failed=1, duration_seconds=self._elapsed(started)),
                issues=[f"fetch failed: {type(exc).__name__}"],
            )
            source.record_failure(f"{type(exc).__name__}: {exc}")
            self._store_health(source.health())
            logger.warning("news source %s fetch failed: %s", source.name, exc)
            return result

        fetch_ms = (time.perf_counter() - monotonic_started) * 1000
        process_started = time.perf_counter()
        result = await self.process_items_async(
            source, items, started=started, dispatch_processors=dispatch_processors,
        )
        process_ms = (time.perf_counter() - process_started) * 1000
        duration_ms = (time.perf_counter() - monotonic_started) * 1000
        # Fetching is a remote download: 0.5 s typical, 1.5 s at p90 (Oct 21,
        # 2026). The old 250 ms total warned on ~every poll of every source.
        slow = fetch_ms >= SLOW_FETCH_MS or process_ms >= SLOW_PROCESS_MS
        logger.log(
            logging.WARNING if slow else logging.DEBUG,
            "news_ingest_duration source=%s duration_ms=%.2f fetch_ms=%.2f "
            "process_ms=%.2f items=%d",
            source.name,
            duration_ms,
            fetch_ms,
            process_ms,
            len(items),
        )
        detail = (
            f"fetched={result.stats.fetched} stored={result.stats.stored} "
            f"duplicates={result.stats.duplicates} rejected={result.stats.rejected}"
        )
        source.record_success(result.stats.fetched, detail=detail)
        health = source.health()
        self._store_health(health)
        logger.info(
            "news source %s: fetched=%d normalized=%d duplicates=%d stored=%d rejected=%d in %.2fs",
            source.name,
            result.stats.fetched,
            result.stats.normalized,
            result.stats.duplicates,
            result.stats.stored,
            result.stats.rejected,
            result.stats.duration_seconds,
        )
        return result.model_copy(update={"status": health.status})

    async def process_items_async(
        self, source: NewsSource, items: Sequence[SourceItem], *, started: datetime,
        dispatch_processors: bool = True,
    ) -> IngestionResult:
        """Normalize off-loop and commit bounded chunks on the connection owner.

        No SQLite transaction spans an await. Source ordering, deduplication and
        processor callbacks remain owned by the event-loop thread.
        """
        stats = IngestionStats(fetched=len(items))
        events: list[MarketEvent] = []
        issues: list[str] = []
        for offset in range(0, len(items), 5):
            batch = items[offset:offset + 5]
            normalized = await asyncio.to_thread(_normalize_batch, batch, source.metadata)
            result = self.process_items(
                source, batch, started=started, normalized=normalized,
                dispatch_processors=dispatch_processors,
            )
            for field in ("normalized", "duplicates", "stored", "rejected", "failed"):
                setattr(stats, field, getattr(stats, field) + getattr(result.stats, field))
            events.extend(result.events)
            issues.extend(result.issues)
            await asyncio.sleep(0)
        stats.duration_seconds = self._elapsed(started)
        self._stats[source.name] = stats
        return IngestionResult(
            source=source.name, checked_at=self.clock(), status=source.health().status,
            stats=stats, events=events, issues=issues,
        )

    def process_items(
        self,
        source: NewsSource,
        items: Sequence[SourceItem],
        *,
        started: datetime | None = None,
        normalized: Sequence[MarketEvent | None] | None = None,
        dispatch_processors: bool = True,
    ) -> IngestionResult:
        """Run the deterministic normalization/dedupe/store pipeline.

        Sync and pure of I/O beyond the repository, so it is directly testable
        without constructing an event loop or a network adapter.
        """
        if self.repository is None:
            raise RuntimeError("event repository unavailable")
        started = started or self.clock()
        stats = IngestionStats(fetched=len(items))
        issues: list[str] = []
        stored: list[MarketEvent] = []
        changed: dict[str, MarketEvent] = {}
        metadata = source.metadata
        repository = self.repository
        with repository.connection:
            for index, item in enumerate(items):
                try:
                    raw_seq = repository.store_raw(
                        RawEvent(
                            source=source.name,
                            source_id=item.source_id,
                            source_url=item.source_url,
                            fetched_at=started,
                            content_hash=content_hash(item.raw),
                            payload=item.raw,
                        )
                    )
                    event = normalized[index] if normalized is not None else normalize_item(
                        item, metadata,
                    )
                    if event is None:
                        raise ValueError("source normalization failed")
                    stats.normalized += 1
                    existing = self._find_existing(event)
                    occurrence = SourceOccurrence(
                        occurrence_key=(
                            f"{item.source}:{item.source_id}:{event.id}"
                            if event.economic is not None
                            else f"{item.source}:{item.source_id}"
                        ),
                        event_id=existing.id if existing is not None else event.id,
                        source=item.source,
                        source_type=item.source_type,
                        source_id=item.source_id,
                        source_url=item.source_url,
                        headline=event.headline,
                        published_at=event.published_at,
                        received_at=event.received_at,
                        fingerprint=event.deduplication_key,
                        raw_seq=raw_seq or None,
                    )
                    occurrence_new = repository.store_occurrence(occurrence)
                    if existing is None:
                        canonical = self._create(event)
                        repository.upsert_event(canonical)
                        stats.stored += 1
                    elif occurrence_new:
                        canonical = self._merge(existing, event)
                        repository.merge_event(canonical)
                    else:
                        # Re-fetch of an already-recorded report: no event change.
                        stats.duplicates += 1
                        canonical = existing
                    self._events[canonical.id] = canonical
                    stored.append(canonical)
                    if existing is None or occurrence_new:
                        changed[canonical.id] = canonical
                except Exception as exc:
                    stats.rejected += 1
                    issues.append(f"rejected item {item.source_id!r}: {type(exc).__name__}")
        # Keep a bounded, ordered recent cache.
        self._recent = self._merge_recent(stored)
        if changed:
            created = [event.id for event in changed.values() if event.revision == 1]
            updated = [event.id for event in changed.values() if event.revision > 1]
            self._publish("market_event_created", created)
            self._publish("market_event_updated", updated)
        if dispatch_processors:
            for event in changed.values():
                self._run_processors(event)
        stats.duration_seconds = self._elapsed(started)
        self._stats[source.name] = stats
        return IngestionResult(
            source=source.name,
            checked_at=self.clock(),
            status=source.health().status,
            stats=stats,
            events=stored,
            issues=issues,
        )

    def _find_existing(self, event: MarketEvent) -> MarketEvent | None:
        """Resolve durable exact identity before searching the recent cache."""
        existing = self._events.get(event.id)
        if existing is not None:
            return existing
        if self.repository is not None:
            # The startup cache contains only the latest 1000 events. A feed
            # refetch must not recreate and overwrite an older canonical row.
            existing = self.repository.event_by_id(event.id)
            if existing is not None:
                return existing
            candidate = self.repository.event_by_key(event.deduplication_key)
            if candidate is not None:
                existing = find_match(
                    event,
                    [candidate],
                    threshold=self.similarity_threshold,
                    window=self.dedupe_window,
                )
                if existing is not None:
                    return existing
        return find_match(
            event,
            self._events.values(),
            threshold=self.similarity_threshold,
            window=self.dedupe_window,
        )

    def _create(self, event: MarketEvent) -> MarketEvent:
        return event.model_copy(
            update={
                "ingestion_status": IngestionStatus.STORED,
                "occurrence_count": 1,
                "sources": [event.source],
                "first_seen_at": event.published_at or event.received_at,
                "last_seen_at": event.published_at or event.received_at,
            }
        )

    def _merge(self, existing: MarketEvent, event: MarketEvent) -> MarketEvent:
        """Merge a distinct source report into the canonical event."""
        merged_sources = sorted(set(existing.sources) | {event.source})
        first_seen = min(
            [
                value
                for value in (existing.first_seen_at, event.published_at, event.received_at)
                if value
            ],
        )
        last_seen = max(
            [
                value
                for value in (existing.last_seen_at, event.published_at, event.received_at)
                if value
            ],
        )
        return existing.model_copy(
            update={
                "occurrence_count": existing.occurrence_count + 1,
                "sources": merged_sources,
                "revision": existing.revision + 1,
                "last_seen_at": last_seen,
                "first_seen_at": first_seen,
            }
        )

    def _merge_recent(self, incoming: Sequence[MarketEvent]) -> list[MarketEvent]:
        by_id = {event.id: event for event in self._recent}
        for event in incoming:
            by_id[event.id] = event
        ordered = sorted(
            by_id.values(),
            key=lambda item: (item.published_at or item.received_at, item.id),
            reverse=True,
        )
        return ordered[:1000]

    def _elapsed(self, started: datetime) -> float:
        return max(0.0, (self.clock() - started).total_seconds())

    def _store_health(self, health: SourceHealth) -> None:
        if self.repository is None:
            return
        with self.repository.connection:
            self.repository.store_source_health(health)
        if self._health_status.get(health.name) != health.status:
            self._health_status[health.name] = health.status
            self._publish("source_status_changed", [health.name])

    # --- read sides ---------------------------------------------------

    def source_health(self) -> list[SourceHealth]:
        # In-memory health of configured sources is authoritative; a persisted
        # row is only a fallback for a source no longer in the catalog or for a
        # process that has not polled yet.
        by_name = {health.name: health for health in (s.health() for s in self.sources)}
        if self.repository is not None:
            try:
                for health in self.repository.source_health():
                    by_name.setdefault(health.name, health)
            except Exception:  # pragma: no cover - storage failure isolation
                pass
        return [by_name[name] for name in sorted(by_name)]

    def ingestion_stats(self) -> dict[str, IngestionStats]:
        return dict(self._stats)

    @property
    def event_count(self) -> int:
        return len(self._events)

    def latest_events(self, *, limit: int = 100) -> list[MarketEvent]:
        now = self.clock()
        return [
            event for event in self._recent
            if (event.published_at or event.received_at) <= now
        ][:limit]

    def events(
        self,
        *,
        source: str | None = None,
        source_type: SourceType | None = None,
        event_type: str | None = None,
        instrument: str | None = None,
        country: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 200,
    ) -> list[MarketEvent]:
        # Suppress legacy future-calendar rows before ordering/LIMIT without
        # altering their stored evidence. Scheduled events remain in Calendar.
        current_end = self.clock() + timedelta(microseconds=1)
        end = min(end, current_end) if end is not None else current_end
        if instrument is not None:
            canonical = filter_instruments([instrument])
            instrument = canonical[0] if canonical else instrument
        if self.repository is None:
            return [
                event
                for event in self._recent
                if self._matches(
                    event,
                    source=source,
                    source_type=source_type,
                    event_type=event_type,
                    instrument=instrument,
                    country=country,
                    start=start,
                    end=end,
                )
            ][:limit]
        return self.repository.query_events(
            source=source,
            source_type=source_type,
            event_type=event_type,
            instrument=instrument,
            country=country,
            start=start,
            end=end,
            limit=limit,
        )

    def event(self, event_id: str) -> MarketEvent | None:
        cached = self._events.get(event_id)
        if cached is not None:
            return cached
        if self.repository is None:
            return None
        return self.repository.event_by_id(event_id)

    def occurrences(self, event_id: str) -> list[SourceOccurrence]:
        if self.repository is None:
            return []
        return self.repository.occurrences_for(event_id)

    @staticmethod
    def _matches(
        event: MarketEvent,
        *,
        source: str | None,
        source_type: SourceType | None,
        event_type: str | None,
        instrument: str | None,
        country: str | None,
        start: datetime | None,
        end: datetime | None,
    ) -> bool:
        if source is not None and source not in event.sources and event.source != source:
            return False
        if source_type is not None and event.source_type != source_type:
            return False
        if event_type is not None and event.event_type != event_type:
            return False
        if instrument is not None and instrument not in event.instruments:
            return False
        if country is not None and country not in event.countries:
            return False
        moment = event.published_at or event.received_at
        if start is not None and moment < start:
            return False
        if end is not None and moment >= end:
            return False
        return True


__all__ = [
    "IngestionResult",
    "IngestionStats",
    "NEWS_MESSAGE_TYPES",
    "NewsMailbox",
    "NewsMessage",
    "NewsPolling",
    "NewsService",
    "build_sources",
    "utc_now",
]
