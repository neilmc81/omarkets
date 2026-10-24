from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from math import isfinite
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from core.analysis.releases import (
    observed_release_events,
    parse_release_id,
    release_history,
)
from core.domain.economics import (
    EconomicEvent,
    EconomicProviderStatus,
    EconomicsMode,
    EventStatus,
    Importance,
    MacroCategory,
    MacroObservation,
    MacroProviderHealth,
    MacroRegistry,
    MacroSeriesState,
    MacroState,
    UTCDatetime,
)
from core.domain.models import DataStatus, utc_now
from core.domain.sessions import CLOSED_MARKET_MAX_DELAY, closed_market_delay, market_closed
from core.providers.economic_base import EconomicBatch, EconomicProvider
from core.services.calendar_projection import project_calendar
from core.services.market_state import SubscriptionClosed
from core.storage.economics import EconomicChanges, SeriesCheck, SQLiteEconomicRepository


class EconomicMessageType(StrEnum):
    UPCOMING = "economic_event_upcoming"
    DUE = "economic_event_due"
    RELEASED = "economic_event_released"
    REVISED = "economic_event_revised"
    SERIES = "macro_series_updated"
    PROVIDER = "provider_status_changed"


class EconomicMessageData(BaseModel):
    ids: list[str]
    occurred_at: UTCDatetime


class EconomicMessage(BaseModel):
    type: EconomicMessageType
    data: EconomicMessageData


class EconomicMailbox:
    def __init__(self, max_ids: int = 100) -> None:
        if max_ids < 1:
            raise ValueError("max_ids must be positive")
        self.max_ids = max_ids
        self.pending: dict[EconomicMessageType, EconomicMessage] = {}
        self.ready = asyncio.Event()
        self.closed = False

    def offer(self, message: EconomicMessage) -> None:
        if self.closed:
            return
        previous = self.pending.get(message.type)
        ids = set(message.data.ids)
        if previous:
            ids.update(previous.data.ids)
        self.pending[message.type] = EconomicMessage(
            type=message.type,
            data=EconomicMessageData(
                ids=["*"] if "*" in ids or len(ids) > self.max_ids else sorted(ids),
                occurred_at=max(previous.data.occurred_at, message.data.occurred_at)
                if previous
                else message.data.occurred_at,
            ),
        )
        self.ready.set()

    async def get(self) -> EconomicMessage:
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


class EconomicPolling(BaseModel):
    default_seconds: float = Field(default=21600, ge=30, allow_inf_nan=False)
    overrides: dict[str, float] = Field(default_factory=dict)
    transition_seconds: float = Field(default=30, ge=1, allow_inf_nan=False)
    proximity_before_seconds: float = Field(default=1800, ge=0, allow_inf_nan=False)
    proximity_after_seconds: float = Field(default=1800, ge=0, allow_inf_nan=False)
    proximity_seconds: float = Field(default=300, ge=30, allow_inf_nan=False)

    def interval(self, name: str) -> float:
        defaults = {
            "demo": 30,
            "bls": 86400,
            "nyfed": 3600,
            "bea_calendar": 21600,
            "bea_releases": 21600,
            "fed_calendar": 21600,
            "fred_release_calendar": 21600,
            "fed_releases": 1800,
            "fred": 21600,
            "consensus": 3600,
            "dol_claims": 900,
            "ism_releases": 900,
            "ism_newswire": 900,
            "adp_releases": 900,
            "nar_releases": 900,
            "census_retail": 900,
            "fed_g17": 900,
        }
        value = self.overrides.get(name, defaults.get(name, self.default_seconds))
        if not isfinite(value) or value < 30:
            raise ValueError("economic polling intervals must be finite and at least 30 seconds")
        return value


def calendar_window(now: datetime, scope: str, days: int = 30) -> tuple[datetime, datetime]:
    local = now.astimezone(ZoneInfo("America/New_York"))
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if scope == "today":
        start, end = midnight, midnight + timedelta(days=1)
    elif scope == "week":
        start = midnight - timedelta(days=midnight.weekday())
        end = start + timedelta(days=7)
    else:
        start, end = now, now + timedelta(days=days)
    return start.astimezone(UTC), end.astimezone(UTC)


class EconomicService:
    def __init__(
        self,
        *,
        repository: SQLiteEconomicRepository | None = None,
        providers: Sequence[EconomicProvider] = (),
        registry: MacroRegistry | None = None,
        mode: EconomicsMode = EconomicsMode.DISABLED,
        polling: EconomicPolling | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.mode = EconomicsMode(mode)
        self.repository = repository
        self.registry = registry or MacroRegistry()
        self.providers = tuple(providers)
        self.polling = polling or EconomicPolling()
        self.clock = clock
        names = [provider.name for provider in self.providers]
        if len(set(names)) != len(names):
            raise ValueError("economic provider names must be unique")
        if repository is not None and repository.mode != self.mode:
            raise ValueError("service and database mode must match")
        if self.mode == EconomicsMode.DISABLED and self.providers:
            raise ValueError("disabled economics cannot have providers")
        for name in (*names, *self.polling.overrides):
            self.polling.interval(name)
        self._checks: dict[str, MacroProviderHealth] = {}
        self._series_checks: dict[tuple[str, str], SeriesCheck] = {}
        self._observations: list[MacroObservation] = []
        self._release_versions: list[MacroObservation] = []
        self._releases: list[EconomicEvent] = []
        self._events: list[EconomicEvent] = []
        self._tasks: list[asyncio.Task] = []
        self._mailboxes: set[EconomicMailbox] = set()
        self._transitions: dict[str, tuple[int, EventStatus]] = {}
        self._health_status: dict[str, EconomicProviderStatus] = {}
        self._started = False
        self._closed = False
        self._cache_seeded = False

    @property
    def subscriber_count(self) -> int:
        return len(self._mailboxes)

    def subscribe(self) -> EconomicMailbox:
        mailbox = EconomicMailbox()
        self._mailboxes.add(mailbox)
        return mailbox

    def unsubscribe(self, mailbox: EconomicMailbox) -> None:
        self._mailboxes.discard(mailbox)
        mailbox.close()

    def _publish(self, kind: EconomicMessageType, ids: list[str]) -> None:
        if not ids:
            return
        message = EconomicMessage(
            type=kind,
            data=EconomicMessageData(
                ids=sorted(set(ids)),
                occurred_at=self.clock(),
            ),
        )
        for mailbox in self._mailboxes:
            mailbox.offer(message)

    def _seed(self) -> None:
        if self.repository is not None:
            self._checks = {check.name: check for check in self.repository.checks()}
            self._series_checks = {
                (check.provider, check.id): check for check in self.repository.series_checks()
            }
            self._observations = self.repository.observations()
            self._release_versions = self.repository.observations(all_versions=True)
            self._events = self.repository.events()
        self._refresh_releases()
        self._cache_seeded = True

    def _refresh_releases(self) -> None:
        self._releases = observed_release_events(
            self._release_versions,
            self.registry,
            mode=self.mode,
            now=self.clock(),
        )

    def _project_calendar(self) -> list[EconomicEvent]:
        releases = list(self._releases)
        retained = {item.id for item in releases}
        # The public observed feed selects only the newest period. Older issuer
        # occurrences still need their observed member for durable aliases and
        # revision signatures. Rebuild it solely from retained observations so
        # restarts produce the same projection without writing source history.
        for item in self._events:
            if item.provider != "ism_newswire" or item.period is None:
                continue
            alias = f"observed:{item.event_code}:{item.period}"
            if alias in retained:
                continue
            definition = self.registry.get(item.event_code)
            if definition is None or definition.provider != "ism_newswire":
                continue
            history = release_history(
                self._release_versions, definition, item.period, mode=self.mode,
            )
            if history:
                releases.append(history[-1])
                retained.add(alias)
        return project_calendar(list((*self._events, *releases)), now=self.clock())

    async def start(self) -> None:
        if self._started:
            return
        self._closed = False
        try:
            if self.repository is not None:
                self.repository.initialize()
                self._seed()
            self._started = True
            if self.mode != EconomicsMode.DISABLED:
                if self.repository is None:
                    raise ValueError("enabled economics requires a repository")
                self._tasks = [
                    asyncio.create_task(self._poll(provider)) for provider in self.providers
                ]
                self._tasks.append(asyncio.create_task(self._timer()))
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
        try:
            for provider in self.providers:
                try:
                    await provider.aclose()
                except Exception:
                    pass
        finally:
            if self.repository is not None:
                self.repository.close()

    async def _poll(self, provider: EconomicProvider) -> None:
        interval = self.polling.interval(provider.name)
        last_attempt = None
        while True:
            await self._wait_until_due(provider.name, interval, last_attempt=last_attempt)
            await self.check_provider(provider)
            last_attempt = self.clock()
            quiet_delay = closed_market_delay(self.clock())
            if quiet_delay > 0:
                await asyncio.sleep(quiet_delay)

    async def _wait_until_due(self, name: str, interval: float,
                              *, last_attempt: datetime | None = None) -> None:
        """Reconsider an upcoming release instead of sleeping through it for hours."""
        while True:
            check = self._checks.get(name)
            checked_at = last_attempt or (check.checked_at if check else None)
            if checked_at is None:
                return
            delay = (checked_at + timedelta(
                seconds=self._next_interval(name, interval)
            ) - self.clock()).total_seconds()
            if delay <= 0:
                return
            await asyncio.sleep(min(delay, self.polling.transition_seconds))

    def _next_interval(self, name: str, interval: float) -> float:
        if self.polling.proximity_seconds >= interval:
            return interval
        now = self.clock()
        before = timedelta(seconds=self.polling.proximity_before_seconds)
        after = timedelta(seconds=self.polling.proximity_after_seconds)
        related = {
            "dol_claims": {"INITIAL_CLAIMS"},
            "adp_releases": {"ADP"},
            "census_retail": {"RETAIL_SALES"},
            "fed_g17": {"INDUSTRIAL_PRODUCTION"},
            "fred": {
                "PPI",
                "CORE_PPI",
                "RETAIL_SALES",
                "CORE_RETAIL_SALES",
                "INDUSTRIAL_PRODUCTION",
            },
            "ism_releases": {"ISM_MFG", "ISM_MFG_PRICES", "ISM_SERVICES"},
            "ism_newswire": {"ISM_MFG", "ISM_MFG_PRICES", "ISM_SERVICES"},
            "bls": {
                "CPI",
                "CORE_CPI",
                "PAYROLLS",
                "JOLTS",
                "UNEMPLOYMENT",
                "AHE",
                "PPI",
                "CORE_PPI",
            },
        }.get(name, set())
        for item in self._events:
            if (item.provider != name and item.event_code not in related) or (
                item.status == EventStatus.CANCELLED
            ):
                continue
            moment = item.scheduled_at or item.released_at
            if moment is not None and now - after <= moment <= now + before:
                minimum = 900 if name in {"bls", "fred"} else 60 if name in {
                    "dol_claims", "ism_releases", "ism_newswire", "adp_releases"
                } else self.polling.proximity_seconds
                return min(interval, minimum)
        for item in self._releases:
            if item.provider != name:
                continue
            moment = item.released_at
            if moment is not None and now - after <= moment <= now + before:
                return self.polling.proximity_seconds
        return interval

    async def check_provider(self, provider: EconomicProvider) -> None:
        if self.repository is None or self.mode == EconomicsMode.DISABLED:
            return
        try:
            batch = await provider.fetch()
            if batch.provider != provider.name:
                raise ValueError("provider identity mismatch")
            self.accept_batch(batch)
        except asyncio.CancelledError:
            raise
        except Exception:
            check = MacroProviderHealth(
                name=provider.name,
                status=EconomicProviderStatus.UNAVAILABLE,
                checked_at=self.clock(),
                detail="Provider check or persistence failed; cached data retained",
            )
            try:
                self.repository.store_check(
                    check, expected_series_ids=self._provider_series_ids(provider.name)
                )
            except Exception:
                pass
            for series_id in self._provider_series_ids(provider.name):
                key = (provider.name, series_id)
                previous = self._series_checks.get(key)
                self._series_checks[key] = SeriesCheck(
                    provider=provider.name,
                    id=series_id,
                    attempted_at=check.checked_at,
                    successful=False,
                    last_success_at=previous.last_success_at if previous else None,
                    latest_verified_period=previous.latest_verified_period if previous else None,
                )
            self._checks[provider.name] = check
            self.tick()

    def _provider_series_ids(self, provider: str) -> tuple[str, ...]:
        return tuple(item.id for item in self.registry.definitions if item.provider == provider)

    def accept_batch(self, batch: EconomicBatch) -> EconomicChanges:
        if self.repository is None:
            raise RuntimeError("economic repository unavailable")
        if not self._cache_seeded:
            # Some callers accept a batch before start(), including against an
            # existing database. Load its history once before applying deltas.
            self._seed()
        changes = self.repository.store_batch(
            batch, expected_series_ids=self._provider_series_ids(batch.provider)
        )
        # Persistence returns canonical saved revisions. Keep its latest-row
        # order (ascending insertion sequence), including multiple updates of a
        # source identity in one batch, without decoding all historical payloads.
        if changes.observations:
            latest = {
                (item.provider, item.source_series_id, item.id, item.period): item
                for item in self._observations
            }
            for item in changes.observations:
                key = (item.provider, item.source_series_id, item.id, item.period)
                latest.pop(key, None)
                latest[key] = item
            self._observations = list(latest.values())
            self._release_versions.extend(changes.observations)
            self._refresh_releases()
        if changes.events:
            latest_events = {
                (item.provider, item.source_event_id, item.id): item for item in self._events
            }
            for item in changes.events:
                key = (item.provider, item.source_event_id, item.id)
                latest_events.pop(key, None)
                latest_events[key] = item
            self._events = list(latest_events.values())
        # Provider/series checks can change on identical or empty batches. These
        # bounded latest checks remain sourced from the committed repository.
        self._checks = {check.name: check for check in self.repository.checks()}
        self._series_checks = {
            (check.provider, check.id): check for check in self.repository.series_checks()
        }
        self._publish(EconomicMessageType.SERIES, [item.id for item in changes.observations])
        self._publish(
            EconomicMessageType.REVISED, [item.id for item in changes.events if item.revision > 1]
        )
        self.tick()
        return changes

    async def _timer(self) -> None:
        while True:
            self.tick()
            await asyncio.sleep(self.polling.transition_seconds)

    def event_state(self, item: EconomicEvent, now: datetime | None = None) -> EconomicEvent:
        now = now or self.clock()
        if item.status == EventStatus.SCHEDULED and item.scheduled_at is not None:
            if item.scheduled_at <= now:
                return EconomicEvent.model_validate(
                    {**item.model_dump(), "status": EventStatus.DUE}
                )
        return item

    def tick(self) -> None:
        now = self.clock()
        projected = self._project_calendar()
        for item in projected:
            if item.id.startswith("observed:"):
                continue
            current = self.event_state(item, now)
            signature = (current.revision, current.status)
            if self._transitions.get(item.id) == signature:
                continue
            if current.status == EventStatus.SCHEDULED:
                if current.scheduled_at is None or not (
                    now < current.scheduled_at <= now + timedelta(days=30)
                ):
                    continue
                kind = EconomicMessageType.UPCOMING
            elif current.status == EventStatus.DUE:
                if (current.provider == "fred_release_calendar"
                        and current.scheduled_at is not None
                        and current.scheduled_at < now - timedelta(days=7)):
                    # Historical schedule backfill is readable evidence, not a
                    # newly due release notification or paid analysis trigger.
                    self._transitions[item.id] = signature
                    continue
                kind = EconomicMessageType.DUE
            elif current.status == EventStatus.RELEASED:
                kind = EconomicMessageType.RELEASED
            else:
                self._transitions[item.id] = signature
                continue
            self._transitions[item.id] = signature
            self._publish(kind, [item.id])
        for item in self._releases:
            if any(item.id in occurrence.occurrence_aliases for occurrence in projected):
                continue
            signature = (item.revision, item.status)
            previous = self._transitions.get(item.id)
            if previous == signature:
                continue
            self._transitions[item.id] = signature
            if previous is None:
                self._publish(EconomicMessageType.RELEASED, [item.id])
            elif item.revision > previous[0]:
                self._publish(EconomicMessageType.REVISED, [item.id])
        for health in self.provider_health():
            if self._health_status.get(health.name) != health.status:
                self._health_status[health.name] = health.status
                self._publish(EconomicMessageType.PROVIDER, [health.name])

    def provider_health(self) -> list[MacroProviderHealth]:
        names = sorted(
            {provider.name for provider in self.providers}
            | {item.provider for item in self.registry.definitions}
            | self._checks.keys()
        )
        now = self.clock()
        result = []
        for name in names:
            check = self._checks.get(name)
            if check is None or self.mode == EconomicsMode.DISABLED:
                result.append(
                    MacroProviderHealth(
                        name=name,
                        status="not_configured",
                        detail="Disabled or no completed provider check",
                    )
                )
                continue
            if check.status == EconomicProviderStatus.HEALTHY and (
                check.checked_at is None
                or (now - check.checked_at).total_seconds()
                > self._freshness_limit(name, check.checked_at, now)
            ):
                check = check.model_copy(
                    update={
                        "status": EconomicProviderStatus.DEGRADED,
                        "detail": "Provider check is stale; cached data retained",
                    }
                )
            result.append(check)
        return result

    def _freshness_limit(self, name: str, checked_at: datetime, now: datetime) -> float:
        """Seconds a successful check stays current.

        Two poll intervals, plus the deliberate closed-market pause of ``_poll``
        when the market was shut at the check or is shut now, so slower polling
        on a weekend or the daily break is not reported as an unhealthy source.
        """
        limit = self.polling.interval(name) * 2
        if market_closed(checked_at) or market_closed(now):
            limit += CLOSED_MARKET_MAX_DELAY
        return limit

    def latest_observations(self) -> list[MacroObservation]:
        """Select release evidence without projecting history or the calendar.

        This reads the owner-thread cache, preserving current_state's registry
        matching, latest-period/revision ordering, and unavailable values.
        Freshness remains a responsibility of the evidence qualification layer.
        """
        latest_by_key: dict[tuple[str, str, str, str], MacroObservation] = {}
        for item in self._observations:
            key = (item.id, item.provider, item.source_series_id, item.unit)
            previous = latest_by_key.get(key)
            if previous is None or (item.period, item.revision) > (
                previous.period, previous.revision
            ):
                latest_by_key[key] = item
        result = []
        for definition in self.registry.definitions:
            latest = latest_by_key.get((
                definition.id, definition.provider, definition.source_series_id, definition.unit,
            ))
            if latest is not None:
                note = "Freshness reflects source checks, not reference-period timeliness"
                result.append(latest.model_copy(
                    update={"notes": list(dict.fromkeys([*latest.notes, note]))}
                ))
        return result

    def current_state(self) -> MacroState:
        now = self.clock()
        health = {item.name: item for item in self.provider_health()}
        series = []
        for definition in self.registry.definitions:
            candidates = [
                item
                for item in self._observations
                if item.id == definition.id
                and item.provider == definition.provider
                and item.source_series_id == definition.source_series_id
                and item.unit == definition.unit
            ]
            latest = max(candidates, key=lambda item: (item.period, item.revision), default=None)
            recent = self._recent_history(candidates)
            check = self._series_checks.get((definition.provider, definition.id))
            status = DataStatus.UNAVAILABLE
            if latest is not None and latest.value is not None:
                status = (
                    DataStatus.LIVE
                    if (
                        check
                        and check.successful
                        and check.latest_verified_period is not None
                        and check.latest_verified_period >= latest.period
                        and check.last_success_at is not None
                        and (now - check.last_success_at).total_seconds()
                        <= self._freshness_limit(definition.provider, check.last_success_at, now)
                        and self.mode != EconomicsMode.DISABLED
                    )
                    else DataStatus.STALE
                )
            if latest is not None:
                note = "Freshness reflects source checks, not reference-period timeliness"
                latest = latest.model_copy(
                    update={"notes": list(dict.fromkeys([*latest.notes, note]))}
                )
            series.append(
                MacroSeriesState(
                    definition=definition,
                    latest=latest,
                    status=status,
                    last_checked_at=check.last_success_at if check else None,
                    recent=recent,
                )
            )
        upcoming = self.calendar_events(start=now, end=now + timedelta(days=30))
        return MacroState(
            generated_at=now,
            mode=self.mode,
            series=series,
            providers=list(health.values()),
            upcoming_events=upcoming,
        )

    def _recent_history(self, candidates: list[MacroObservation]) -> list[MacroObservation]:
        if not candidates:
            return []
        by_period: dict[str, MacroObservation] = {}
        for item in candidates:
            current = by_period.get(item.period)
            if current is None or (item.revision, item.received_at) > (
                current.revision,
                current.received_at,
            ):
                by_period[item.period] = item
        ordered = [by_period[key] for key in sorted(by_period)]
        return ordered[-24:]

    def calendar_provider_health(self) -> list[MacroProviderHealth]:
        relevant = {
            "bea_calendar", "fed_calendar", "fed_releases", "bea_releases",
            "consensus", "bls_calendar",
            "fred_release_calendar", "dol_claims", "ism_releases",
            "ism_newswire", "adp_releases", "nar_releases", "census_retail", "fed_g17", "demo",
        }
        relevant.update(item.provider for item in self._events)
        relevant.update(item.provider for item in self._releases)
        return [item for item in self.provider_health() if item.name in relevant]

    def observed_releases(self) -> list[EconomicEvent]:
        return list(self._releases)

    def release_event_history(self, event_id: str) -> list[EconomicEvent]:
        parsed = parse_release_id(event_id)
        if parsed is None:
            return []
        definition = self.registry.get(parsed[0])
        if definition is None:
            return []
        if self.repository is not None:
            versions = self.repository.observations(all_versions=True)
        else:
            versions = self._release_versions
        return release_history(versions, definition, parsed[1], mode=self.mode)

    def calendar_schedule_providers(self) -> set[str]:
        """Retained source records that actually supply independently scheduled slots."""
        return {event.provider for event in self._events if event.scheduled_at is not None}

    def calendar_schedule_history(self) -> list[datetime]:
        """Stored source schedules, without the list endpoint's presentation limit."""
        return [e.scheduled_at for e in self._events if e.scheduled_at is not None]

    def calendar_events(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        importance: Importance | None = None,
        category: MacroCategory | None = None,
        status: EventStatus | None = None,
        limit: int = 500,
    ) -> list[EconomicEvent]:
        result = []
        for source in self._project_calendar():
            item = self.calendar_event_state(source)
            when = item.scheduled_at or item.released_at
            if (
                when is None
                or (start is not None and when < start)
                or (end is not None and when >= end)
            ):
                continue
            if importance is not None and item.importance != importance:
                continue
            if category is not None and item.category != category:
                continue
            if status is not None and item.status != status:
                continue
            result.append(item)
        ordered = sorted(result, key=lambda item: (item.scheduled_at or item.released_at, item.id))
        if len(ordered) > limit:
            if start is None and end is None:
                ordered = ordered[-limit:]
            else:
                ordered = ordered[:limit]
        return ordered

    def calendar_occurrence(self, event_id: str) -> EconomicEvent | None:
        """Resolve current canonical or historical source IDs without a list limit."""
        for item in self._project_calendar():
            if event_id == item.id or event_id in item.occurrence_aliases:
                return self.calendar_event_state(item)
        return None

    def calendar_event_state(self, item: EconomicEvent) -> EconomicEvent:
        current = self.event_state(item)
        if current.provider == "consensus" and current.event_code == "ISM_MFG" and (
            "ism manufacturing prices" in current.name.lower()
        ):
            # Correct the legacy mapping in the core view without rewriting
            # stored provider records or changing their evidence identity.
            current = current.model_copy(update={
                "event_code": "ISM_MFG_PRICES", "category": MacroCategory.INFLATION,
            })
        if current.provider == "consensus" and current.event_code == "RETAIL_SALES" and (
            "core retail sales" in current.name.casefold()
        ):
            current = current.model_copy(update={"event_code": "CORE_RETAIL_SALES"})
        return self._calendar_coverage(self._ism_calendar_actual(self._calendar_actual(current)))

    def _ism_calendar_actual(self, item: EconomicEvent) -> EconomicEvent:
        slot = item.scheduled_at
        now = self.clock()
        if (
            item.provider != "consensus"
            or item.event_code not in {"ISM_MFG", "ISM_MFG_PRICES", "ISM_SERVICES"}
            or slot is None
            or slot > now
            or item.received_at > now
            or item.actual is not None
            or item.status == EventStatus.CANCELLED
            or item.unit not in {"value", "index points"}
            or (item.consensus is not None and not 0 <= item.consensus <= 100)
            or (item.previous is not None and not 0 <= item.previous <= 100)
        ):
            return item
        local = slot.astimezone(ZoneInfo("America/New_York"))
        if (local.hour, local.minute, local.second) != (10, 0, 0):
            return item
        year, month = (local.year - 1, 12) if local.month == 1 else (local.year, local.month - 1)
        period = f"{year:04d}-{month:02d}"
        if item.period is not None and item.period != period:
            return item
        matches = [
            event
            for event in self._events
            if event.provider == "ism_newswire"
            and event.event_code == item.event_code
            and event.country == item.country
            and event.mode == item.mode
            and event.period == period
            and event.unit == "index points"
            and event.actual_basis == "source_release"
            and event.actual is not None
            and event.released_at is not None
            and event.released_at == slot
            and event.released_at <= event.received_at <= now
        ]
        if len(matches) != 1:
            return item
        source = matches[0]
        return EconomicEvent.model_validate(
            {
                **item.model_dump(),
                "status": EventStatus.RELEASED,
                "actual": source.actual,
                "actual_basis": "source_release",
                "period": period,
                "unit": source.unit,
                "source_url": source.source_url,
                "released_at": source.released_at,
                "received_at": source.received_at,
                "notes": [
                    *item.notes,
                    f"Schedule and forecast source: {item.source_url}",
                    f"Actual from ISM-issued newswire release: {source.source_url}",
                    "Issuer, report period, and publication time matched the calendar slot.",
                ],
            }
        )

    @staticmethod
    def _calendar_series(item: EconomicEvent) -> str | None:
        code, title = item.event_code, item.name.casefold().strip()
        if code == "RETAIL_SALES" and "core retail sales" in title:
            code = "CORE_RETAIL_SALES"  # Retained legacy source rows.
        if code in {"CPI", "CORE_CPI"}:
            if item.provider == "consensus":
                prefix = "core cpi" if code == "CORE_CPI" else "cpi"
                if title == prefix + " m/m":
                    return code + "_MOM"
                if title == prefix + " y/y":
                    return code + "_YOY"
                return None
            return code + "_MOM"
        if code in {"PPI", "CORE_PPI"} and item.provider == "consensus":
            prefix = "core ppi" if code == "CORE_PPI" else "ppi"
            if title == prefix + " y/y":
                return "BLS_" + code + "_YOY"
        if code in {
            "PPI",
            "CORE_PPI",
            "RETAIL_SALES",
            "CORE_RETAIL_SALES",
            "INDUSTRIAL_PRODUCTION",
        }:
            expected_title = {
                "PPI": "ppi m/m",
                "CORE_PPI": "core ppi m/m",
                "RETAIL_SALES": "retail sales m/m",
                "CORE_RETAIL_SALES": "core retail sales m/m",
                "INDUSTRIAL_PRODUCTION": "industrial production m/m",
            }[code]
            return code if item.provider == "consensus" and title == expected_title else None
        if code == "AHE":
            return "AHE_YOY" if title.endswith("y/y") else "AHE_MOM"
        return {"PAYROLLS": "PAYROLLS", "UNEMPLOYMENT": "UNEMPLOYMENT", "JOLTS": "JOLTS"}.get(code)

    def _calendar_coverage(self, item: EconomicEvent) -> EconomicEvent:
        if item.actual is not None or item.mode != EconomicsMode.LIVE:
            return item
        series = self._calendar_series(item)
        if series:
            from core.domain.macro_registry import MACRO_DEFINITIONS

            definition = next(row for row in MACRO_DEFINITIONS if row.id == series)
            explanation = (
                f"Actual coverage: {definition.provider.upper()} {definition.source_series_id}, "
                "current vintage only; requires the exact reference month, metric, "
                "units and elapsed release slot. The matching value is not verified yet."
            )
        else:
            explanation = "Actual coverage: " + {
                "GDP": "BEA publication notice only; numeric GDP estimate not extracted.",
                "GDP_PRICE_INDEX": "no verified GDP price-index release metric adapter.",
                "PCE": "BEA publication notice only; PCE release metric not extracted.",
                "CORE_PCE": "no verified core PCE release metric adapter.",
                "DURABLE_GOODS": "no verified Census durable-goods release metric adapter.",
                "HOUSING_STARTS": "no verified Census housing-starts release metric adapter.",
                "BUILDING_PERMITS": "no verified Census building-permits release metric adapter.",
                "NEW_HOME_SALES": "no verified Census new-home-sales release metric adapter.",
                "ADP": "bounded ADP issuer releases; no unique verified slot match yet.",
                "EXISTING_HOME_SALES": "latest NAR national release; no verified slot match yet.",
                "INITIAL_CLAIMS": "official DOL release; no verified numeric match yet.",
                "ISM_MFG": "ISM issuer newswire; no verified report-period/slot match yet.",
                "ISM_MFG_PRICES": "ISM manufacturing prices table; no verified slot match yet.",
                "ISM_SERVICES": "ISM issuer newswire; no verified report-period/slot match yet.",
            }.get(item.event_code,
                  f"no verified numeric adapter for {item.event_code} / {item.name}.")
        return item.model_copy(update={"notes": list(dict.fromkeys([*item.notes, explanation]))})

    def _calendar_actual(self, item: EconomicEvent) -> EconomicEvent:
        """Attach a verified current series value to its matching release metric/slot.

        This is a presentation join, not an assertion that the value is the
        initial publication vintage or that the schedule is a publication log.
        """
        series = self._calendar_series(item)
        slot = item.scheduled_at
        if (
            series is None
            or slot is None
            or slot > self.clock()
            or item.actual is not None
            or item.provider not in {"fred_release_calendar", "bls_calendar", "consensus"}
            or item.received_at > self.clock()
            or item.status == EventStatus.CANCELLED
        ):
            return item
        local = slot.astimezone(ZoneInfo("America/New_York"))
        year, month = (local.year - 1, 12) if local.month == 1 else (local.year, local.month - 1)
        period = f"{year:04d}-{month:02d}"
        if item.period is not None and item.period != period:
            return item
        if item.provider == "consensus":
            expected_time = (9, 15, 0) if item.event_code == "INDUSTRIAL_PRODUCTION" else (8, 30, 0)
            if (local.hour, local.minute, local.second) != expected_time:
                return item
            # More than one source slot for the same metric is ambiguous. Distinct
            # m/m and y/y metrics, and headline/core releases, never share actuals.
            if (
                sum(
                    e.provider == "consensus"
                    and e.scheduled_at == slot
                    and e.status != EventStatus.CANCELLED
                    and self._calendar_series(e) == series
                    for e in self._events
                )
                > 1
            ):
                return item
        if item.event_code in {"PAYROLLS", "UNEMPLOYMENT", "AHE", "CPI", "CORE_CPI"}:
            if (local.hour, local.minute, local.second) != (8, 30, 0):
                return item
            if item.provider == "consensus" and not any(
                official.provider in {"bls_calendar", "fred_release_calendar"}
                and official.event_code
                == ("CPI" if item.event_code in {"CPI", "CORE_CPI"} else "PAYROLLS")
                and official.scheduled_at == slot
                and official.status != EventStatus.CANCELLED
                and official.period in {None, period}
                for official in self._events
            ):
                return item
        from core.domain.macro_registry import MACRO_DEFINITIONS

        definition = next(row for row in MACRO_DEFINITIONS if row.id == series)
        if series in {"PPI", "CORE_PPI"}:
            # Keep legacy FRED history and fallback, while preferring the direct
            # issuer series when an eligible value exists for this exact slot.
            direct = "BLS_" + series + "_MOM"
            if any(
                row.id == direct
                and row.provider == "bls"
                and row.period == period
                and row.value is not None
                and slot <= row.received_at <= self.clock()
                and (row.released_at is None or slot <= row.released_at <= self.clock())
                for row in self._observations
            ):
                series = direct
                definition = next(row for row in MACRO_DEFINITIONS if row.id == series)
        if item.provider == "consensus" and series != "PAYROLLS":
            if item.unit != "percent" and not (
                item.unit == "value" and item.consensus is None and item.previous is None
            ):
                return item
        if item.provider == "consensus" and series == "PAYROLLS":
            if item.unit not in {"value", "jobs", "thousand jobs"}:
                return item
        candidates = [
            observation
            for observation in self._observations
            if observation.provider == definition.provider
            and observation.id == series
            and observation.period == period
            and observation.value is not None
            and slot <= observation.received_at <= self.clock()
            and (observation.released_at is None or slot <= observation.released_at <= self.clock())
            and observation.source_series_id == definition.source_series_id
            and observation.unit == definition.unit
            and observation.metadata.source_url is not None
            and (
                definition.provider == "bls"
                and urlparse(observation.metadata.source_url).hostname
                in {"api.bls.gov", "www.bls.gov"}
                or definition.provider == "fred"
                and observation.metadata.source_url
                == f"https://fred.stlouisfed.org/series/{definition.source_series_id}"
            )
        ]
        if not candidates:
            return item
        observation = max(candidates, key=lambda row: (row.revision, row.received_at))
        if len({row.value for row in candidates if row.revision == observation.revision}) > 1:
            return item
        revised_previous = item.revised_previous
        evidence_received_at = observation.received_at
        prior_note = []
        if series.startswith("BLS_") and "PPI" in series:
            from core.analysis.economics import month_before

            prior = [
                row
                for row in self._observations
                if row.id == series
                and row.provider == definition.provider
                and row.source_series_id == definition.source_series_id
                and row.unit == definition.unit
                and row.value is not None
                and row.period == month_before(period, 1)
                and slot <= row.received_at <= self.clock()
                and row.metadata.source_url == observation.metadata.source_url
                and (row.released_at is None or row.released_at <= self.clock())
            ]
            if prior:
                latest_prior = max(prior, key=lambda row: row.revision)
                values = {row.value for row in prior if row.revision == latest_prior.revision}
                if len(values) == 1:
                    evidence_received_at = max(evidence_received_at, latest_prior.received_at)
                    # The calendar's previous remains its source's recorded prior.
                    # A differing current prior is qualified, never called the
                    # original issuer's prior-release vintage.
                    if item.previous is not None and round(latest_prior.value, 1) != round(
                        item.previous, 1
                    ):
                        revised_previous = latest_prior.value
                    prior_note = [
                        f"BLS previous-period current vintage ({latest_prior.period}): "
                        f"{latest_prior.value} percent; original prior-release vintage unverified.",
                        f"Actual evidence received: {observation.received_at.isoformat()}; "
                        f"prior evidence received: {latest_prior.received_at.isoformat()}; "
                        f"prior stored revision: {latest_prior.revision}."
                    ]
        actual = observation.value
        unit = observation.unit
        unit_note = None
        if item.provider == "consensus" and series == "PAYROLLS" and item.unit in {"value", "jobs"}:
            actual = actual * 1000
            unit = "jobs"
            unit_note = (
                "BLS payroll change converted from thousand jobs to jobs to match forecast units."
            )
        return EconomicEvent.model_validate(
            {
                **item.model_dump(),
                "status": EventStatus.RELEASED,
                "actual": actual,
                "actual_basis": "current_vintage",
                "revised_previous": revised_previous,
                "period": period,
                "unit": unit,
                "source_url": observation.metadata.source_url,
                "source_revision": observation.metadata.source_revision,
                "received_at": evidence_received_at,
                "notes": [
                    *item.notes,
                    f"Schedule source: {item.source_url}",
                    f"{definition.provider.upper()} series: {observation.source_series_id}; "
                    f"reference period: {period}.",
                    f"Actual is the {definition.provider.upper()} current vintage "
                    "received by Omarkets; "
                    "the original release value and publication instant are unverified.",
                    *prior_note,
                    *([unit_note] if unit_note else []),
                ],
            }
        )
