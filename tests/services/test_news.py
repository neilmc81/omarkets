from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from core.domain.news import (
    IngestionStatus,
    SourceItem,
    SourceMetadata,
    SourceStatus,
    SourceType,
)
from core.ingest.normalize import normalize_item
from core.providers.news.base import NewsSource
from core.services.news import NewsMailbox, NewsPolling, NewsService, build_sources
from core.storage.events import SQLiteEventRepository

NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


class StubSource(NewsSource):
    def __init__(self, name="stub", items=(), *, fail=False, clock=None, enabled=True) -> None:
        super().__init__(clock=clock or Clock(), enabled=enabled)
        self._name = name
        self._items = list(items)
        self.fail = fail
        self.fetch_count = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def metadata(self) -> SourceMetadata:
        return SourceMetadata(name=self._name, display_name=self._name, source_type=SourceType.NEWS)

    async def fetch(self) -> list[SourceItem]:
        self.fetch_count += 1
        if self.fail:
            raise RuntimeError("boom")
        return list(self._items)


def item(source_id: str, headline: str, *, source: str = "stub", **overrides) -> SourceItem:
    values = {
        "source": source,
        "source_type": SourceType.NEWS,
        "source_id": source_id,
        "headline": headline,
        "published_at": NOW,
        "received_at": NOW,
        "raw": {"id": source_id},
    }
    values.update(overrides)
    return SourceItem.model_validate(values)


@pytest.fixture
def repo():
    repository = SQLiteEventRepository(":memory:", "live")
    repository.initialize()
    yield repository
    repository.close()


def test_build_sources_enable_and_disable() -> None:
    sources = build_sources(disabled=["cnbc_top"], clock=Clock())
    by_name = {source.name: source for source in sources}
    assert by_name["cnbc_top"].enabled is False
    assert by_name["fed_press_monetary"].enabled is True
    only = build_sources(enabled=["bbc_world"], clock=Clock())
    assert {source.name for source in only if source.enabled} == {"bbc_world"}


async def test_return_refresh_stores_news_without_paid_processor_dispatch(repo):
    source = StubSource(items=[item("return", "Missed economic release")])
    service = NewsService(sources=[source], repository=repo, clock=Clock())
    processed = []
    service.register_processor(lambda event: processed.append(event.id))
    result = await service.ingest_source(source, dispatch_processors=False)
    assert result.stats.stored == 1
    assert len(result.events) == 1
    assert processed == []
    # Later normal polling of identical evidence must not replay paid dispatch.
    duplicate = await service.ingest_source(source)
    assert duplicate.stats.duplicates == 1
    assert processed == []


def test_recent_feed_excludes_legacy_future_publications_before_limit(repo) -> None:
    source = StubSource(clock=Clock())
    service = NewsService(sources=[source], repository=repo, clock=Clock())
    current = normalize_item(item("current", "Current release"), source.metadata)
    future = normalize_item(
        item("future", "Future schedule", published_at=NOW + timedelta(days=30)), source.metadata
    )
    repo.upsert_event(current)
    repo.upsert_event(future)
    service._recent = [future, current]
    assert [event.id for event in service.events(limit=1)] == [current.id]
    assert [event.id for event in service.latest_events(limit=1)] == [current.id]
    assert repo.event_by_id(future.id) is not None


def test_polling_interval_override_validation() -> None:
    policy = NewsPolling(default_seconds=90)
    assert policy.interval("anything") == 90
    policy = NewsPolling(overrides={"x": 45})
    assert policy.interval("x") == 45
    with pytest.raises(ValueError):
        NewsPolling(overrides={"x": 1}).interval("x")


def test_service_requires_unique_source_names(repo) -> None:
    with pytest.raises(ValueError):
        NewsService(repository=repo, sources=[StubSource("a"), StubSource("a")], clock=Clock())


def test_process_items_stores_normalizes_and_extracts(repo) -> None:
    source = StubSource(items=[item("1", "Nvidia beats as Fed holds rates")])
    service = NewsService(repository=repo, sources=[source], clock=Clock())
    result = service.process_items(source, source._items)
    assert result.stats.fetched == 1
    assert result.stats.normalized == 1
    assert result.stats.stored == 1
    assert result.stats.duplicates == 0
    event = result.events[0]
    assert "NVDA" in event.instruments
    assert event.ingestion_status is IngestionStatus.STORED
    assert repo.count() == 1
    assert repo.occurrences_for(event.id)


@pytest.mark.asyncio
async def test_news_normalization_worker_does_not_block_loop_or_own_storage(repo, monkeypatch):
    import threading

    import core.services.news as module

    owner = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    original = module._normalize_batch
    source = StubSource(items=[item("worker", "Nvidia quarterly earnings")])
    service = NewsService(repository=repo, sources=[source], clock=Clock())

    def blocked(items, metadata):
        assert threading.get_ident() != owner
        entered.set()
        assert release.wait(2)
        return original(items, metadata)

    processor_threads = []
    service.register_processor(lambda event: processor_threads.append(threading.get_ident()))
    monkeypatch.setattr(module, "_normalize_batch", blocked)
    task = asyncio.create_task(service.ingest_source(source))
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.001)
        assert entered.is_set()
        for _ in range(5):
            await asyncio.sleep(0.001)
        assert not task.done()
    finally:
        release.set()
    result = await task
    assert result.stats.normalized == result.stats.stored == 1
    assert processor_threads == [owner]
    assert repo.count() == 1


@pytest.mark.asyncio
async def test_async_news_chunks_preserve_totals_and_refetch_idempotence(repo):
    source = StubSource(items=[item(str(n), f"Company {n} quarterly earnings") for n in range(12)])
    service = NewsService(repository=repo, sources=[source], clock=Clock())
    result = await service.ingest_source(source)
    assert result.stats.fetched == result.stats.normalized == len(result.events) == 12
    before = repo.count()
    repeated = await service.ingest_source(source)
    assert repeated.stats.duplicates == 12
    assert repeated.stats.stored == 0
    assert repo.count() == before


def test_duplicate_from_same_source_counts_as_duplicate(repo) -> None:
    source = StubSource(items=[item("1", "Fed holds rates")])
    service = NewsService(repository=repo, sources=[source], clock=Clock())
    seen = []
    service.register_processor(lambda event: seen.append(event.id))
    service.process_items(source, source._items)
    result = service.process_items(source, source._items)
    assert result.stats.duplicates == 1
    assert result.stats.stored == 0
    assert repo.count() == 1
    assert len(seen) == 1


@pytest.mark.parametrize("published_at", [NOW, None], ids=["published", "unpublished"])
async def test_restart_refetch_preserves_canonical_outside_seed_cache(tmp_path, published_at):
    path = tmp_path / "events.sqlite3"
    repository = SQLiteEventRepository(path, "live")
    repository.initialize()
    clock = Clock()
    first = StubSource("wire_a", clock=clock)
    second = StubSource("wire_b", clock=clock)
    original_item = item(
        "original", "Fed holds rates", source=first.name, published_at=published_at,
        summary="Original release details", source_url="https://example.test/original",
        metadata={"evidence": "original"}, raw={"evidence": "original"},
    )
    service = NewsService(repository=repository, clock=clock)
    original = service.process_items(first, [original_item]).events[0]
    original = service.process_items(
        second,
        [original_item.model_copy(update={"source": second.name, "source_id": "second"})],
    ).events[0]
    assert original.revision == original.occurrence_count == 2
    # More rows than the startup cache: this canonical really is on disk only.
    with repository.connection:
        for index in range(1001):
            moment = NOW + timedelta(minutes=index + 1)
            repository.upsert_event(original.model_copy(update={
                "id": f"filler:{index}", "deduplication_key": f"filler:{index}",
                "headline": f"Unrelated archive entry {index}",
                "published_at": moment, "received_at": moment,
            }))
    repository.close()

    clock.advance(days=30)
    repository = SQLiteEventRepository(path, "live")
    restarted = NewsService(repository=repository, enabled=False, clock=clock)
    processed = []
    restarted.register_processor(processed.append)
    await restarted.start()
    try:
        assert len(restarted.subscribe_events()) == 1000
        assert original.id not in restarted._events
        mailbox = restarted.subscribe()
        refetched = original_item.model_copy(update={
            "received_at": clock(), "summary": "Refetched details must not replace history",
            "metadata": {"evidence": "refetched"}, "raw": {"evidence": "refetched"},
        })
        result = restarted.process_items(first, [refetched])
        assert result.stats.duplicates == 1
        assert result.stats.stored == result.stats.rejected == 0
        assert result.events == [original]
        assert repository.event_by_id(original.id) == original
        assert processed == []
        assert mailbox.pending == {}
        assert len(repository.occurrences_for(original.id)) == 2
        assert repository.count() == 1002

        # A genuinely new report merges metadata while preserving base facts.
        restarted._seed()
        assert original.id not in restarted._events
        third = StubSource("wire_c", clock=clock)
        report = refetched.model_copy(update={"source": third.name, "source_id": "third"})
        expected = restarted._merge(original, normalize_item(report, third.metadata))
        merged = restarted.process_items(third, [report])
        assert merged.stats.stored == merged.stats.duplicates == merged.stats.rejected == 0
        assert merged.events == [expected]
        assert expected.revision == expected.occurrence_count == 3
        assert expected.sources == ["wire_a", "wire_b", "wire_c"]
        assert expected.first_seen_at == NOW
        assert expected.last_seen_at == clock()
        assert expected.received_at == original.received_at
        assert expected.published_at == original.published_at
        assert expected.source == original.source
        assert expected.summary == original.summary
        assert expected.metadata == original.metadata
        assert expected.raw_payload == original.raw_payload
        assert repository.event_by_id(original.id) == expected
        assert len(repository.occurrences_for(original.id)) == 3
        assert processed == [expected]
        assert set(mailbox.pending) == {"market_event_updated"}
    finally:
        await restarted.stop()


@pytest.mark.parametrize("hours, matches", [(1, True), (72, False)])
def test_durable_key_fallback_respects_existing_time_window(repo, hours, matches):
    source = StubSource()
    event = normalize_item(item("original", "Fed holds rates"), source.metadata)
    canonical = event.model_copy(update={"id": "legacy:canonical"})
    with repo.connection:
        repo.upsert_event(canonical)
    incoming = event.model_copy(update={"published_at": NOW + timedelta(hours=hours)})
    service = NewsService(repository=repo, clock=Clock())
    assert service._find_existing(incoming) == (canonical if matches else None)


def test_cross_source_dedup_merges_occurrences(repo) -> None:
    first = StubSource(
        "wire_a", items=[item("1", "Fed signals a rate cut as inflation cools", source="wire_a")]
    )
    second = StubSource(
        "wire_b",
        items=[item("9", "Fed signals a rate cut as inflation cools", source="wire_b")],
    )
    service = NewsService(repository=repo, sources=[first, second], clock=Clock())
    service.process_items(first, first._items)
    result = service.process_items(second, second._items)
    assert result.stats.stored == 0
    assert repo.count() == 1
    event = service.latest_events()[0]
    assert event.occurrence_count == 2
    assert set(event.sources) == {"wire_a", "wire_b"}
    assert len(repo.occurrences_for(event.id)) == 2


def test_distinct_events_are_not_merged(repo) -> None:
    source = StubSource(
        items=[
            item("1", "Nvidia earnings beat estimates"),
            item("2", "Treasury sanctions oil network"),
        ]
    )
    service = NewsService(repository=repo, sources=[source], clock=Clock())
    service.process_items(source, source._items)
    assert repo.count() == 2


async def test_fetch_failure_is_isolated_and_recorded(repo) -> None:
    failing = StubSource("bad", fail=True)
    good = StubSource("good", items=[item("1", "Fed holds rates")])
    service = NewsService(repository=repo, sources=[failing, good], clock=Clock())
    result = await service.ingest_source(failing)
    assert result.stats.failed == 1
    assert result.status is SourceStatus.DEGRADED
    assert failing.health().status is SourceStatus.DEGRADED
    assert failing.health().last_error is not None
    # The healthy source is unaffected and reports success in its result.
    ok = await service.ingest_source(good)
    assert ok.stats.stored == 1
    assert ok.status is SourceStatus.HEALTHY
    health = {entry.name: entry.status for entry in service.source_health()}
    assert health["good"] is SourceStatus.HEALTHY


async def test_malformed_item_rejected_without_failing_batch(repo) -> None:
    source = StubSource(items=[item("1", "Valid headline"), item("2", "Another valid")])
    service = NewsService(repository=repo, sources=[source], clock=Clock())
    # Inject a malformed item through the pipeline.
    from core.domain.news import SourceItem as SI

    malformed = SI.model_construct(
        source="stub",
        source_type=SourceType.NEWS,
        source_id="bad",
        source_url=None,
        headline="   ",
        summary=None,
        published_at=None,
        published_at_assumed_utc=False,
        received_at=NOW,
        language="en",
        event_type=None,
        metadata={},
        raw={},
        central_bank=None,
        economic=None,
    )
    result = service.process_items(source, [*source._items, malformed])
    assert result.stats.rejected == 1
    assert result.stats.stored == 2


def test_subscribe_events_and_processor_seam(repo) -> None:
    seen: list[str] = []
    source = StubSource(items=[item("1", "Fed holds rates")])
    service = NewsService(repository=repo, sources=[source], clock=Clock())
    service.register_processor(lambda event: seen.append(event.id))
    service.process_items(source, source._items)
    assert seen == [service.latest_events()[0].id]
    assert [event.id for event in service.subscribe_events()] == seen


def test_processor_failure_does_not_break_pipeline(repo) -> None:
    source = StubSource(items=[item("1", "Fed holds rates")])

    def boom(_event) -> None:
        raise RuntimeError("processor exploded")

    service = NewsService(repository=repo, sources=[source], clock=Clock())
    service.register_processor(boom)
    result = service.process_items(source, source._items)
    assert result.stats.stored == 1


def test_mailbox_coalesces_ids(repo) -> None:
    from core.services.news import NewsMessage, NewsMessageData

    mailbox = NewsMailbox(max_ids=2)
    mailbox.offer(
        NewsMessage(type="market_event_created", data=NewsMessageData(ids=["a"], occurred_at=NOW))
    )
    mailbox.offer(
        NewsMessage(type="market_event_created", data=NewsMessageData(ids=["b"], occurred_at=NOW))
    )
    mailbox.offer(
        NewsMessage(type="market_event_created", data=NewsMessageData(ids=["c"], occurred_at=NOW))
    )
    message = asyncio.run(mailbox.get())
    assert message.data.ids == ["*"]


def test_events_filters_and_cached_reads(repo) -> None:
    source = StubSource(
        items=[
            item("1", "Nvidia beats estimates"),
            item("2", "Treasury sanctions oil network", source_type=SourceType.GEOPOLITICAL),
        ]
    )
    service = NewsService(repository=repo, sources=[source], clock=Clock())
    service.process_items(source, source._items)
    assert len(service.events(instrument="NVDA")) == 1
    assert len(service.events(source_type=SourceType.GEOPOLITICAL)) == 1
    assert service.event(service.latest_events()[0].id) is not None
    assert service.event("missing") is None


async def test_start_seeds_and_stops_cleanly(repo) -> None:
    source = StubSource(items=[item("1", "Fed holds rates")])
    service = NewsService(
        repository=repo, sources=[source], polling=NewsPolling(default_seconds=30), clock=Clock()
    )
    await service.start()
    assert service.enabled
    # Let the poll task run once.
    for _ in range(20):
        if source.fetch_count:
            break
        await asyncio.sleep(0.01)
    assert source.fetch_count >= 1
    assert service.subscriber_count == 0
    await service.stop()


async def test_disabled_service_does_not_poll(repo) -> None:
    source = StubSource(items=[item("1", "x")])
    service = NewsService(repository=repo, sources=[source], enabled=False, clock=Clock())
    await service.start()
    await asyncio.sleep(0.02)
    assert source.fetch_count == 0
    await service.stop()


async def test_source_health_persisted_across_restart(repo) -> None:
    source = StubSource(items=[item("1", "Fed holds rates")])
    service = NewsService(repository=repo, sources=[source], clock=Clock())
    await service.ingest_source(source)
    # A fresh service with no sources reads persisted health as a fallback.
    other = NewsService(repository=repo, sources=[], clock=Clock())
    names = {health.name for health in other.source_health()}
    assert "stub" in names


class SlowSource(StubSource):
    def __init__(self, delay: float, **kwargs) -> None:
        super().__init__(**kwargs)
        self.delay = delay

    async def fetch(self) -> list[SourceItem]:
        await asyncio.sleep(self.delay)
        return await super().fetch()


async def test_ordinary_download_time_is_not_logged_as_slow(repo, caplog, monkeypatch):
    """Oct 21, 2026: a 250 ms total warned on ~every poll of every source, because
    a remote download normally takes 0.3-2 s. Only real outliers are warnings."""
    import core.services.news as news

    source = SlowSource(0.3, items=[item("a", "Headline")])
    service = NewsService(sources=[source], repository=repo, clock=Clock())
    with caplog.at_level("DEBUG", logger="core.services.news"):
        await service.ingest_source(source)
    timing = [r for r in caplog.records if "news_ingest_duration" in r.getMessage()]
    assert [r.levelname for r in timing] == ["DEBUG"]

    caplog.clear()
    monkeypatch.setattr(news, "SLOW_FETCH_MS", 100.0)
    with caplog.at_level("DEBUG", logger="core.services.news"):
        await service.ingest_source(source)
    timing = [r for r in caplog.records if "news_ingest_duration" in r.getMessage()]
    assert [r.levelname for r in timing] == ["WARNING"]


def test_catalog_intervals_only_slow_sources_down() -> None:
    """Oct 23, 2026: catalog ``poll_seconds`` was never applied, so Energy (900 s)
    and the EC (600 s) were polled every 300 s like everything else."""
    policy = NewsPolling.from_catalog(300)
    assert policy.interval("energy_press") == 900
    assert policy.interval("ec_press") == 600
    assert policy.interval("eia_today") == 1800
    assert policy.interval("fed_press_all") == 300
    assert policy.interval("cnbc_top") == 300  # catalog 120 s never speeds a feed up
    assert NewsPolling.from_catalog(300, {"energy_press": 60}).interval("energy_press") == 60


async def test_poll_waits_out_a_rate_limit_backoff(repo, monkeypatch) -> None:
    import core.services.news as news

    source = StubSource(items=[item("1", "Fed holds rates")])
    source.backoff_seconds = 1800.0
    service = NewsService(
        repository=repo, sources=[source], polling=NewsPolling(default_seconds=300), clock=Clock()
    )
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        if seconds:  # processing yields with sleep(0); only the poll wait matters
            waits.append(seconds)
            raise asyncio.CancelledError

    monkeypatch.setattr(news.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await service._poll(source)
    assert waits == [1800.0]


async def test_boj_html_page_keeps_stored_items_and_reports_cleanly(repo) -> None:
    from pathlib import Path

    import httpx

    from core.providers.news.catalog import SOURCE_DEFINITIONS
    from core.providers.news.central_bank import BoJWhatsNewSource

    fixtures = Path(__file__).parents[1] / "fixtures/news"
    bodies = [
        (fixtures / "boj-whatsnew-2026-10-23.xml").read_bytes(),
        (fixtures / "boj-html-page-2026-10-23.html").read_bytes(),
    ]
    definition = next(d for d in SOURCE_DEFINITIONS if d.name == "boj_whatsnew")
    clock = Clock(datetime(2026, 10, 23, 3, tzinfo=UTC))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=bodies[0]))
    ) as client:
        source = BoJWhatsNewSource(
            metadata=definition.metadata(), url=definition.url, client=client, clock=clock
        )
        service = NewsService(repository=repo, sources=[source], clock=clock)
        await service.ingest_source(source)
        stored = len(service.events(limit=500))
        assert stored > 0
        bodies.pop(0)
        result = await service.ingest_source(source)
    assert result.stats.failed == 1
    health = source.health()
    assert health.status == SourceStatus.DEGRADED
    assert health.last_error == "ValueError: received a web page (HTML) instead of the feed"
    assert len(service.events(limit=500)) == stored
