"""Current market state.

The state is derived from a cache of the latest observation per instrument.
That cache is seeded from durable storage at startup and then updated by the
provider stream, so a restart does not report "no data" for instruments that
were observed before the process stopped.

State building is pure and deterministic: given observations, a staleness
policy, and a clock, :func:`build_market_state` returns a
:class:`~core.domain.models.MarketState`. It performs no I/O and no async work,
which makes staleness and change logic testable without a running event loop.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import math
import time
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel

from core.domain.calendars import calendar_for
from core.domain.instruments import (
    DEFAULT_STALE_SECONDS,
    INSTRUMENTS,
    AssetClass,
    get_instrument,
)
from core.domain.models import (
    DataStatus,
    FeedStatus,
    InstrumentState,
    MarketObservation,
    MarketState,
    ProviderStatus,
    utc_now,
)
from core.providers.base import MarketDataProvider
from core.storage.base import ObservationRepository, OhlcBucket, StorageError

logger = logging.getLogger(__name__)

#: A price batch waiting this long (ms) on Omarkets itself is a warning.
SLOW_WRITE_WAIT_MS = 250.0
#: A storage commit (mostly the disk sync) this slow (ms) is a warning. A bare
#: 4 KB write + fsync on this disk alone stalls up to ~2.2 s and DuckDB commits
#: reached 3.6 s (Oct 22, 2026); compaction holds the lock 1.5-3.5 s, 4x a day.
SLOW_STORE_MS = 4000.0


class ServiceHealth(BaseModel):
    """Typed health snapshot of the market state service.

    Returned across the service→API boundary instead of a loose dict.
    """

    provider: str
    provider_status: ProviderStatus
    provider_error: str | None = None
    stream_running: bool
    cached_instruments: int
    updates_published: int
    last_update_at: datetime | None = None
    storage_healthy: bool


class StalePolicy:
    """Resolves the freshness threshold (seconds) for an instrument.

    Thresholds default per asset class, and may be overridden per asset class
    name via configuration.
    """

    def __init__(
        self,
        *,
        default_seconds: float = 30.0,
        overrides: Mapping[str, float] | None = None,
    ) -> None:
        if default_seconds <= 0:
            raise ValueError("default_seconds must be positive")
        self._default = float(default_seconds)
        self._overrides: dict[str, float] = {}
        valid_classes = {asset_class.value for asset_class in AssetClass}
        for key, value in (overrides or {}).items():
            if key not in valid_classes:
                raise ValueError(
                    f"unknown asset class in stale override: {key!r}; "
                    f"expected one of {sorted(valid_classes)}"
                )
            if value <= 0:
                raise ValueError(f"stale override for {key!r} must be positive")
            self._overrides[key] = float(value)

    def threshold_for(self, symbol: str) -> float:
        instrument = get_instrument(symbol)
        if instrument is None:
            return self._default
        override = self._overrides.get(instrument.asset_class.value)
        if override is not None:
            return override
        return DEFAULT_STALE_SECONDS.get(instrument.asset_class, self._default)


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def build_instrument_state(
    symbol: str,
    observation: MarketObservation | None,
    *,
    policy: StalePolicy,
    now: datetime,
) -> InstrumentState:
    """Build the current state of one instrument.

    A missing observation yields ``unavailable``. An observation older than the
    instrument's threshold yields ``stale``. Change fields are ``None`` unless a
    provider-supplied ``previous_close`` baseline exists.
    """
    symbol = symbol.strip().upper()
    instrument = get_instrument(symbol)
    if instrument is None:
        raise KeyError(f"unknown instrument symbol: {symbol!r}")

    stale_after = policy.threshold_for(symbol)

    # A cached/durable row can be ahead of a replay or corrected host clock.
    # Neither event time nor receipt may reveal facts beyond this evidence cutoff.
    if observation is None or observation.timestamp > now or observation.received_at > now:
        return InstrumentState(
            symbol=symbol,
            display_name=instrument.display_name,
            asset_class=instrument.asset_class,
            quote_type=instrument.quote_type,
            unit=instrument.unit,
            value=None,
            previous_close=None,
            change=None,
            change_pct=None,
            timestamp=None,
            received_at=None,
            age_seconds=None,
            stale_after_seconds=stale_after,
            status=DataStatus.UNAVAILABLE,
            source=None,
            feed_status=FeedStatus.UNAVAILABLE,
        )

    age = max(0.0, (now - observation.timestamp).total_seconds())
    previous_close = _as_float(observation.metadata.get("previous_close"))
    change: float | None = None
    change_pct: float | None = None
    if previous_close is not None and previous_close != 0.0:
        change = observation.value - previous_close
        change_pct = (change / previous_close) * 100.0

    expected_delay = _as_float(observation.metadata.get("expected_delay_seconds")) or 0.0
    if not math.isfinite(expected_delay) or expected_delay < 0:
        expected_delay = 0.0
    excess_age = max(0.0, age - expected_delay)
    freshness_margin = max(stale_after, 60.0) if expected_delay else stale_after
    declared_stale = observation.metadata.get("feed_status") in {"stale", "unavailable"}
    status = (
        DataStatus.STALE if declared_stale or excess_age > freshness_margin else DataStatus.LIVE
    )
    feed_status = _feed_status(observation, status)
    # The instrument's own calendar decides whether an old quote is just the
    # last print before a closure; a 24x7 market never closes.
    market_closed = status is DataStatus.STALE and calendar_for(
        instrument).closed_since_last_session(observation.timestamp, now)
    latency = None
    if observation.received_at is not None:
        latency = max(0.0, (observation.received_at - observation.timestamp).total_seconds())

    return InstrumentState(
        symbol=symbol,
        display_name=instrument.display_name,
        asset_class=instrument.asset_class,
        quote_type=instrument.quote_type,
        unit=instrument.unit,
        value=observation.value,
        previous_close=previous_close,
        change=change,
        change_pct=change_pct,
        timestamp=observation.timestamp,
        received_at=observation.received_at,
        age_seconds=round(age, 3),
        stale_after_seconds=freshness_margin,
        status=status,
        source=observation.source,
        feed_status=feed_status,
        provider_symbol=_optional_str(observation.metadata.get("provider_symbol")),
        contract_code=_optional_str(observation.metadata.get("contract_code")),
        contract_expiry=_optional_str(observation.metadata.get("contract_expiry")),
        latency_seconds=None if latency is None else round(latency, 3),
        expected_delay_seconds=expected_delay,
        excess_age_seconds=round(excess_age, 3),
        baseline_period=_optional_str(observation.metadata.get("baseline_period")),
        market_closed=market_closed,
    )


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _feed_status(observation: MarketObservation, status: DataStatus) -> FeedStatus:
    """Classify feed quality from the observation, never from guesswork.

    A declared delayed feed stays ``DELAYED`` even when it is older than the
    staleness threshold: the delay is a property of the source, not a fault.
    A live-declared feed that has aged out is reported ``STALE``.
    """
    declared = observation.metadata.get("feed_status")
    if declared == FeedStatus.DEMO.value or observation.source == "fake":
        return FeedStatus.DEMO
    if declared == FeedStatus.DELAYED.value or observation.metadata.get("delayed") is True:
        return FeedStatus.DELAYED
    if status is DataStatus.STALE or declared == FeedStatus.STALE.value:
        return FeedStatus.STALE
    return FeedStatus.LIVE


def build_market_state(
    observations: Mapping[str, MarketObservation],
    *,
    policy: StalePolicy,
    now: datetime,
    provider: str,
    provider_status: ProviderStatus = ProviderStatus.OK,
) -> MarketState:
    """Build a complete market state containing every known instrument."""
    # Accept mappings keyed by any casing; canonical symbols are authoritative.
    normalized = {symbol.strip().upper(): obs for symbol, obs in observations.items()}
    instruments: dict[str, InstrumentState] = {}
    live = stale = unavailable = 0
    for symbol in INSTRUMENTS:
        state = build_instrument_state(
            symbol,
            normalized.get(symbol),
            policy=policy,
            now=now,
        )
        instruments[symbol] = state
        if state.status is DataStatus.LIVE:
            live += 1
        elif state.status is DataStatus.STALE:
            stale += 1
        else:
            unavailable += 1

    return MarketState(
        generated_at=now,
        provider=provider,
        provider_status=provider_status,
        instruments=instruments,
        live_count=live,
        stale_count=stale,
        unavailable_count=unavailable,
    )


class SubscriptionClosed(Exception):
    """Raised to a subscriber when the service is shutting down."""


class SubscriptionLike(Protocol):
    """The read side of a subscriber mailbox, as consumed by the WS layer."""

    async def get(self) -> MarketState: ...


class _Subscription:
    """A latest-wins mailbox for one WebSocket client."""

    __slots__ = ("queue", "_closed")

    def __init__(self) -> None:
        self.queue: asyncio.Queue[MarketState | None] = asyncio.Queue(maxsize=1)
        self._closed = False

    def offer(self, state: MarketState) -> None:
        if self._closed:
            return
        try:
            self.queue.put_nowait(state)
        except asyncio.QueueFull:
            # Snapshots supersede each other; keep the newest.
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - single publisher
                pass
            self.queue.put_nowait(state)

    def close(self) -> None:
        """Wake any waiter by delivering a sentinel, then mark closed."""
        self._closed = True
        try:
            self.queue.put_nowait(None)
        except asyncio.QueueFull:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover
                pass
            try:
                self.queue.put_nowait(None)
            except asyncio.QueueFull:  # pragma: no cover
                pass

    async def get(self) -> MarketState:
        state = await self.queue.get()
        if state is None:
            raise SubscriptionClosed
        return state


class _ObservationSubscription:
    """A bounded mailbox of accepted observation batches for one consumer.

    Unlike the latest-wins state mailbox, a technical consumer must see every
    accepted batch: bars are built from the *sequence* of observations. The
    queue is bounded so a stalled consumer cannot grow memory without limit; on
    overflow the oldest batch is dropped and the subscriber is told it fell
    behind, rather than silently losing data it did not know about.
    """

    __slots__ = ("queue", "_closed", "dropped")

    def __init__(self, maxsize: int = 256) -> None:
        self.queue: asyncio.Queue[list[MarketObservation] | None] = asyncio.Queue(maxsize=maxsize)
        self._closed = False
        self.dropped = 0

    def offer(self, batch: list[MarketObservation]) -> None:
        if self._closed:
            return
        try:
            self.queue.put_nowait(batch)
        except asyncio.QueueFull:
            self.dropped += 1
            logger.warning(
                "observation subscriber overflowed; dropped %d batch(es); "
                "derived bars for the dropped observations will be missing",
                self.dropped,
            )
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - single publisher
                pass
            self.queue.put_nowait(batch)

    def close(self) -> None:
        self._closed = True
        try:
            self.queue.put_nowait(None)
        except asyncio.QueueFull:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover
                pass
            try:
                self.queue.put_nowait(None)
            except asyncio.QueueFull:  # pragma: no cover
                pass

    async def get(self) -> list[MarketObservation]:
        batch = await self.queue.get()
        if batch is None:
            raise SubscriptionClosed
        return batch


class MarketStateService:
    """Maintains current market state and fans it out to subscribers.

    Lifecycle::

        service = MarketStateService(provider=..., repository=..., ...)
        await service.start()   # seeds from storage, starts the stream task
        ...
        await service.stop()    # cancels the stream task, closes resources

    The service is constructed once per application process and attached to
    FastAPI ``app.state``. It is deliberately not a module-level singleton.
    """

    def __init__(
        self,
        *,
        provider: MarketDataProvider,
        repository: ObservationRepository,
        policy: StalePolicy | None = None,
        clock=None,
        maintenance_seconds: float = 6 * 3600,
    ) -> None:
        self._provider = provider
        self._repository = repository
        self._policy = policy or StalePolicy()
        self._clock = clock or utc_now
        #: How often old observations are thinned (see ``ObservationRepository.compact``).
        self._maintenance_seconds = maintenance_seconds
        self._maintenance_task: asyncio.Task[None] | None = None

        self._observations: dict[str, MarketObservation] = {}
        self._subscriptions: dict[int, _Subscription] = {}
        self._observation_subscriptions: dict[int, _ObservationSubscription] = {}
        self._ids = itertools.count()
        self._task: asyncio.Task[None] | None = None
        self._provider_status = ProviderStatus.STOPPED
        self._provider_error: str | None = None
        self._updates_published = 0
        self._last_update_at: datetime | None = None

    # --- properties -----------------------------------------------------

    @property
    def provider_name(self) -> str:
        return self._provider.name

    @property
    def provider_status(self) -> ProviderStatus:
        return self._provider_status

    @property
    def last_update_at(self) -> datetime | None:
        return self._last_update_at

    @property
    def updates_published(self) -> int:
        return self._updates_published

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    # --- lifecycle ------------------------------------------------------

    async def start(self) -> None:
        """Seed state from storage, then start consuming the provider stream."""
        if self.is_running:
            return

        try:
            self._repository.initialize()
            logger.info("storage initialized")
        except StorageError:
            logger.exception("storage initialization failed")
            raise

        try:
            seeded = self._repository.latest_per_instrument()
        except StorageError:
            logger.exception("could not seed state from storage; starting empty")
            seeded = {}

        # Storage is durable truth; do not let an older cached row win.
        for observation in seeded.values():
            self._accept_observation(observation)
        if seeded:
            logger.info("seeded %d instruments from storage", len(seeded))

        await self._provider.start()
        self._provider_status = ProviderStatus.OK
        self._provider_error = None
        self._task = asyncio.create_task(self._run(), name="omarkets-state-stream")
        self._maintenance_task = asyncio.create_task(
            self._maintain(), name="omarkets-storage-maintenance"
        )
        logger.info("provider %r started", self._provider.name)

    async def stop(self) -> None:
        """Stop the stream task and release resources cleanly.

        Safe to call more than once, and the service can be started again
        afterwards: ``stop`` releases resources rather than destroying state.
        """
        maintenance, self._maintenance_task = self._maintenance_task, None
        if maintenance is not None and not maintenance.done():
            maintenance.cancel()
            try:
                await maintenance
            except asyncio.CancelledError:
                pass
            except Exception:  # pragma: no cover - defensive
                logger.exception("error while stopping storage maintenance")
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # pragma: no cover - defensive
                logger.exception("error while stopping state stream task")

        try:
            await self._provider.aclose()
        except Exception:  # pragma: no cover - defensive
            logger.exception("error closing provider")

        try:
            self._repository.close()
        except Exception:  # pragma: no cover - defensive
            logger.exception("error closing storage")

        # Wake and drop subscribers so no waiter is left hanging.
        for subscription in self._subscriptions.values():
            subscription.close()
        self._subscriptions.clear()
        for observation_subscription in self._observation_subscriptions.values():
            observation_subscription.close()
        self._observation_subscriptions.clear()

        self._provider_status = ProviderStatus.STOPPED
        logger.info("market state service stopped")

    # --- storage maintenance ---------------------------------------------

    async def compact_storage(self) -> int:
        """Thin observations past the retention window; returns rows removed."""
        try:
            removed = await asyncio.to_thread(self._repository.compact, now=self._clock())
        except StorageError:
            logger.exception("storage compaction failed")
            return 0
        if removed:
            logger.info("storage compaction thinned %d old observations", removed)
        return removed

    async def _maintain(self) -> None:
        # First pass shortly after start, then on a fixed cadence. Each pass is
        # incremental, so a missed or repeated pass is harmless.
        await asyncio.sleep(min(120.0, self._maintenance_seconds))
        while True:
            await self.compact_storage()
            await asyncio.sleep(self._maintenance_seconds)

    # --- stream loop ----------------------------------------------------

    async def refresh_snapshot(self) -> None:
        """Refresh through the normal deterministic ingestion path after return."""
        from core.domain.sessions import closed_market_delay

        if closed_market_delay(self._clock()) > 0:
            # Closed-market manual refresh reads stored state, not routine quotes.
            return
        batch = await self._provider.fetch_snapshot()
        await self._handle_batch(batch)

    async def _run(self) -> None:
        try:
            async for batch in self._provider.stream_updates():
                try:
                    await self._handle_batch(batch)
                except Exception:
                    # A single bad tick must not kill the stream.
                    logger.exception("failed to process provider batch")
        except asyncio.CancelledError:
            logger.debug("state stream task cancelled")
            raise
        except Exception as exc:
            logger.exception("provider stream failed")
            self._provider_status = ProviderStatus.ERROR
            self._provider_error = str(exc)

    def _accept_observation(self, observation: MarketObservation) -> bool:
        """Cache an observation only if it supersedes the cached one.

        Out-of-order ticks are common from real feeds. Storage keeps the latest
        by event time with deterministic tie-breaks, so the in-memory cache uses
        the same ordering (event time, then received time, then value) or the
        API and storage would disagree.
        """
        current = self._observations.get(observation.symbol)
        if current is not None:
            if observation.timestamp < current.timestamp:
                return False
            if observation.timestamp == current.timestamp:
                if observation.received_at < current.received_at:
                    return False
                if (
                    observation.received_at == current.received_at
                    and observation.value < current.value
                ):
                    return False
        self._observations[observation.symbol] = observation
        return True

    async def _handle_batch(self, batch: Iterable[MarketObservation]) -> None:
        batch_started = time.perf_counter()
        observations = [obs for obs in batch if obs.symbol in INSTRUMENTS]
        if not observations:
            return

        write_ms = 0.0
        try:
            write_started = time.perf_counter()
            # DuckDB's per-row executemany can take seconds under sustained
            # polling. Its repository lock permits this write on a worker while
            # the event loop continues serving cached shell projections.
            def store() -> tuple[int, float, float]:
                started = time.perf_counter()
                count = self._repository.store(observations)
                return count, started, time.perf_counter()

            write_task = asyncio.create_task(asyncio.to_thread(store))
            try:
                written, store_started, store_ended = await asyncio.shield(write_task)
            except asyncio.CancelledError as cancellation:
                # Cancelling the await cannot stop a DuckDB worker. Let it
                # finish before stop() closes the shared connection.
                try:
                    await write_task
                except Exception:
                    logger.exception("inflight storage write failed during shutdown")
                raise cancellation
            write_ended = time.perf_counter()
            write_ms = (write_ended - write_started) * 1000
            # queue = waiting for a worker thread; store = lock + insert + the
            # disk sync DuckDB does on commit; resume = waiting for the event loop.
            queue_ms = (store_started - write_started) * 1000
            store_ms = (store_ended - store_started) * 1000
            resume_ms = (write_ended - store_ended) * 1000
            # The insert takes a few ms. The disk sync on this machine often takes
            # 0.2-1 s and stalls 2-3.6 s a few times an hour (btrfs on LUKS,
            # measured Oct 21-22, 2026), off the event loop, so only an unusual
            # sync is a warning. Waiting on Omarkets itself
            # (queue/resume) still warns at 250 ms.
            slow = queue_ms + resume_ms >= SLOW_WRITE_WAIT_MS or store_ms >= SLOW_STORE_MS
            if write_ms >= SLOW_WRITE_WAIT_MS:
                logger.log(
                    logging.WARNING if slow else logging.DEBUG,
                    "market_batch_write duration_ms=%.2f count=%d written=%d queue_ms=%.2f "
                    "store_ms=%.2f resume_ms=%.2f",
                    write_ms, len(observations), written, queue_ms, store_ms, resume_ms,
                )
            logger.debug("persisted %d observations", written)
        except StorageError:
            # Serve from memory rather than failing the whole stream.
            logger.exception("failed to persist observations; continuing in memory")

        accepted_batch = [obs for obs in observations if self._accept_observation(obs)]
        accepted = len(accepted_batch)

        self._provider_status = ProviderStatus.OK
        self._provider_error = None
        self._updates_published += 1
        self._last_update_at = self._clock()

        self._publish(self.current_state())
        # State uses the newest event-time observation, while derived analytics
        # need late arrivals too so they can revise the historical bar they
        # belong to. The technical consumer deduplicates and orders by event time.
        if observations:
            for subscription in self._observation_subscriptions.values():
                subscription.offer(observations)
        logger.debug(
            "state updated with %d observations (%d accepted)",
            len(observations),
            accepted,
        )
        duration_ms = (time.perf_counter() - batch_started) * 1000
        # The write has its own warning above; this one covers everything else.
        if duration_ms - write_ms >= SLOW_WRITE_WAIT_MS:
            logger.warning("market_batch duration_ms=%.2f count=%d", duration_ms, len(observations))

    # --- state access ---------------------------------------------------

    def current_state(self, *, now: datetime | None = None) -> MarketState:
        """Build the current market state from the in-memory cache.

        The cache is copied before iteration: a single ``dict()`` copy is
        atomic with respect to the event loop, so readers never observe a
        partially-mutated mapping.
        """
        return build_market_state(
            dict(self._observations),
            policy=self._policy,
            now=now or self._clock(),
            provider=self._provider.name,
            provider_status=self._provider_status,
        )

    def observation_for(self, symbol: str) -> MarketObservation | None:
        return self._observations.get(symbol.strip().upper())

    def history(
        self,
        symbol: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
    ) -> list[MarketObservation]:
        return list(self._repository.history(symbol, start=start, end=end, limit=limit))

    def ohlc(
        self,
        symbol: str,
        *,
        start: datetime,
        end: datetime,
        bucket_seconds: int,
        within: Sequence[tuple[datetime, datetime]] = (),
    ) -> Sequence[OhlcBucket]:
        return self._repository.ohlc(symbol, start=start, end=end,
                                     bucket_seconds=bucket_seconds, within=within)

    def cached_instrument_count(self) -> int:
        return len(self._observations)

    def health_detail(self) -> ServiceHealth:
        return ServiceHealth(
            provider=self._provider.name,
            provider_status=self._provider_status,
            provider_error=self._provider_error,
            stream_running=self.is_running,
            cached_instruments=len(self._observations),
            updates_published=self._updates_published,
            last_update_at=self._last_update_at,
            storage_healthy=self._repository.health(),
        )

    # --- subscriptions --------------------------------------------------

    def subscribe(self) -> tuple[int, _Subscription]:
        """Register a subscriber. Returns an id used to unsubscribe."""
        client_id = next(self._ids)
        subscription = _Subscription()
        # Send the current snapshot immediately so a client never waits a tick.
        subscription.offer(self.current_state())
        self._subscriptions[client_id] = subscription
        logger.debug("websocket client %d subscribed", client_id)
        return client_id, subscription

    def unsubscribe(self, client_id: int) -> None:
        if self._subscriptions.pop(client_id, None) is not None:
            logger.debug("websocket client %d unsubscribed", client_id)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscriptions)

    def _publish(self, state: MarketState) -> None:
        for subscription in self._subscriptions.values():
            subscription.offer(state)

    async def updates(self) -> AsyncIterator[MarketState]:
        """Convenience async iterator over state updates for one consumer."""
        client_id, subscription = self.subscribe()
        try:
            while True:
                yield await subscription.get()
        finally:
            self.unsubscribe(client_id)

    def subscribe_observations(self) -> tuple[int, _ObservationSubscription]:
        """Register a consumer of accepted observation batches.

        Used by derived analytics (e.g. the technical service) that need the
        observation sequence rather than the state snapshot. The consumer is
        responsible for seeding its history from
        :meth:`history` if it needs prior data.
        """
        client_id = next(self._ids)
        subscription = _ObservationSubscription()
        self._observation_subscriptions[client_id] = subscription
        return client_id, subscription

    def unsubscribe_observations(self, client_id: int) -> None:
        subscription = self._observation_subscriptions.pop(client_id, None)
        if subscription is not None:
            subscription.close()

    @property
    def observation_subscriber_count(self) -> int:
        return len(self._observation_subscriptions)

    async def observation_updates(self) -> AsyncIterator[list[MarketObservation]]:
        """Async iterator over accepted observation batches for one consumer."""
        client_id, subscription = self.subscribe_observations()
        try:
            while True:
                yield await subscription.get()
        finally:
            self.unsubscribe_observations(client_id)


__all__ = [
    "MarketStateService",
    "ServiceHealth",
    "StalePolicy",
    "SubscriptionClosed",
    "SubscriptionLike",
    "build_instrument_state",
    "build_market_state",
]
