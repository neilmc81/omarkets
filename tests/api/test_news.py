from __future__ import annotations

from datetime import UTC, datetime

import pytest
from starlette.testclient import TestClient

from core.api.app import create_app
from core.domain.news import SourceItem, SourceMetadata, SourceType
from core.providers.fake import FakeMarketDataProvider
from core.providers.news.base import NewsSource
from core.services.market_state import MarketStateService
from core.services.news import NewsPolling, NewsService
from core.storage.duckdb_repo import DuckDBObservationRepository
from core.storage.events import SQLiteEventRepository

NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)


class StubSource(NewsSource):
    def __init__(self) -> None:
        super().__init__(clock=lambda: NOW)
        self.items = [
            SourceItem(
                source="stub",
                source_type=SourceType.NEWS,
                source_id="1",
                headline="Nvidia beats estimates",
                published_at=NOW,
                received_at=NOW,
                raw={"id": "1"},
            ),
            SourceItem(
                source="stub",
                source_type=SourceType.NEWS,
                source_id="2",
                headline="Treasury sanctions oil shipping network",
                published_at=NOW,
                received_at=NOW,
                raw={"id": "2"},
            ),
        ]

    @property
    def name(self) -> str:
        return "stub"

    @property
    def metadata(self) -> SourceMetadata:
        return SourceMetadata(name="stub", display_name="Stub", source_type=SourceType.NEWS)

    async def fetch(self) -> list[SourceItem]:
        return list(self.items)


@pytest.fixture
def client(tmp_path):
    market = MarketStateService(
        provider=FakeMarketDataProvider(update_interval=1000),
        repository=DuckDBObservationRepository(":memory:"),
    )
    source = StubSource()
    news = NewsService(
        repository=SQLiteEventRepository(tmp_path / "events.sqlite3", "live"),
        sources=[source],
        polling=NewsPolling(default_seconds=3600),
        clock=lambda: NOW,
    )
    with TestClient(create_app(service=market, news_service=news)) as test_client:
        test_client.portal.call(news.ingest_source, source)
        yield test_client, news
    assert news.subscriber_count == market.subscriber_count == 0


def test_health_includes_news(client):
    http, _ = client
    body = http.get("/health").json()
    assert body["news"]["enabled"] is True
    assert body["news"]["source_count"] == 1
    assert body["news"]["healthy_sources"] == 1
    assert body["news"]["event_count"] == 2


def test_events_listing_and_filters(client):
    http, _ = client
    body = http.get("/api/v1/events").json()
    assert body["count"] == 2
    assert {event["headline"] for event in body["events"]} == {
        "Nvidia beats estimates",
        "Treasury sanctions oil shipping network",
    }
    by_instrument = http.get("/api/v1/events?instrument=NVDA").json()
    assert by_instrument["count"] == 1
    assert by_instrument["events"][0]["headline"] == "Nvidia beats estimates"
    by_type = http.get("/api/v1/events?event_type=sanctions").json()
    assert by_type["count"] == 1


def test_latest_events(client):
    http, _ = client
    body = http.get("/api/v1/events/latest?limit=1").json()
    assert body["count"] == 1


def test_event_detail_and_404(client):
    http, _ = client
    listed = http.get("/api/v1/events").json()["events"]
    event_id = listed[0]["id"]
    body = http.get(f"/api/v1/events/{event_id}").json()
    assert body["event"]["id"] == event_id
    assert body["occurrence_count"] == 1
    assert len(body["occurrences"]) == 1
    assert http.get("/api/v1/events/ev:nope").status_code == 404


def test_sources_and_status_endpoints(client):
    http, _ = client
    body = http.get("/api/v1/sources").json()
    assert body["enabled"] is True
    assert "news" in body["source_types"]
    assert body["sources"][0]["name"] == "stub"
    status = http.get("/api/v1/sources/status").json()
    assert status["sources"][0]["status"] == "healthy"
    stats = http.get("/api/v1/sources/stats").json()["stats"]["stub"]
    # The startup poll and the explicit ingest may both run; either way the two
    # items are each stored once and any re-fetch is counted as a duplicate.
    assert stats["fetched"] == 2
    assert stats["stored"] + stats["duplicates"] == 2


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=1001",
        "start=2026-09-18T12:00:00",
        "start=2026-09-19T00:00:00Z&end=2026-09-18T00:00:00Z",
        "source_type=bogus",
    ],
)
def test_events_validation(client, query):
    http, _ = client
    assert http.get("/api/v1/events?" + query).status_code == 422


def test_websocket_news_topic_and_cleanup(client):
    http, news = client
    with http.websocket_connect("/ws/market?topics=news") as ws:
        assert ws.receive_json()["type"] == "state"
        http.portal.call(news._publish, "market_event_created", ["ev:1"])
        message = ws.receive_json()
        assert message["type"] == "market_event_created"
        assert message["data"]["ids"] == ["ev:1"]
    assert news.subscriber_count == 0


def test_default_socket_does_not_subscribe_to_news(client):
    http, news = client
    with http.websocket_connect("/ws/market") as ws:
        assert ws.receive_json()["type"] == "state"
        assert news.subscriber_count == 0


def test_news_disabled_reports_unavailable(tmp_path):
    market = MarketStateService(
        provider=FakeMarketDataProvider(update_interval=1000),
        repository=DuckDBObservationRepository(":memory:"),
    )
    news = NewsService(enabled=False)
    with TestClient(create_app(service=market, news_service=news)) as http:
        assert http.get("/api/v1/events/latest").status_code == 503
        body = http.get("/health").json()
        assert body["news"]["enabled"] is False
    assert news.subscriber_count == 0
