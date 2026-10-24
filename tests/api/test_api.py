"""API contract tests using FastAPI's TestClient with isolated dependencies."""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest
from starlette.testclient import TestClient

from core.api.app import build_service, create_app
from core.config.settings import Settings
from core.domain.instruments import all_symbols
from core.domain.models import MarketObservation
from core.providers.fake import FakeMarketDataProvider
from core.storage.duckdb_repo import DuckDBObservationRepository


def _make_client(
    tmp_path, *, seed: int = 3, interval: float = 0.02, frontend_dist=None
) -> TestClient:
    settings = Settings(
        duckdb_path=tmp_path / "api.duckdb",
        provider_update_interval=interval,
        provider_seed=seed,
    )
    repository = DuckDBObservationRepository(settings.resolved_duckdb_path)
    provider = FakeMarketDataProvider(seed=seed, update_interval=interval)
    service = build_service(settings=settings, provider=provider, repository=repository)
    app = create_app(service=service, frontend_dist=frontend_dist)
    return TestClient(app)


def test_frontend_entry_revalidates_but_hashed_assets_remain_cacheable(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>verified build</html>")
    (dist / "asset-hash.js").write_text("console.log('fixture')")
    with _make_client(tmp_path, frontend_dist=dist) as client:
        for path in ["/", "/index.html"]:
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-cache"
            cached = client.get(path, headers={"If-None-Match": response.headers["etag"]})
            assert cached.status_code == 304
            assert cached.headers["cache-control"] == "no-cache"
        assert "cache-control" not in client.get("/asset-hash.js").headers


@pytest.fixture
def client(tmp_path):
    with _make_client(tmp_path) as test_client:
        # Provider streaming starts asynchronously. Wait for its first
        # published batch before asserting quote-dependent endpoint content.
        _wait_for_updates(test_client, 1)
        yield test_client


def test_health_reports_components(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database_ok"] is True
    assert body["provider"] == "fake"
    assert body["provider_status"] == "ok"
    assert body["stream_running"] is True
    assert body["cached_instruments"] == len(all_symbols())
    assert body["subscriber_count"] == 0
    assert body["time"].endswith("Z") or "+00:00" in body["time"]


def test_health_serves_cached_storage_status_while_write_lock_is_busy(client):
    repository = client.app.state.service._repository
    assert repository.health() is True
    repository._lock.acquire()
    try:
        started = time.monotonic()
        response = client.get("/health")
        elapsed = time.monotonic() - started
        assert response.status_code == 200
        assert response.json()["database_ok"] is True
        assert elapsed < 0.5
    finally:
        repository._lock.release()


def test_shell_and_state_reads_remain_responsive_during_storage_write(client):
    repository = client.app.state.service._repository
    repository._lock.acquire()
    try:
        for path in ("/api/v1/shell/status", "/api/v1/state", "/health"):
            started = time.monotonic()
            response = client.get(path)
            assert response.status_code == 200
            assert time.monotonic() - started < 0.5
    finally:
        repository._lock.release()


def test_state_returns_full_universe(client):
    response = client.get("/api/v1/state")
    assert response.status_code == 200
    body = response.json()
    assert set(body["instruments"]) == set(all_symbols())
    assert body["live_count"] + body["stale_count"] + body["unavailable_count"] == len(
        all_symbols()
    )
    assert body["provider"] == "fake"


def test_instruments_listing(client):
    response = client.get("/api/v1/instruments")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == len(all_symbols())
    symbols = {item["symbol"] for item in body["instruments"]}
    assert symbols == set(all_symbols())
    nq = next(i for i in body["instruments"] if i["symbol"] == "NQ")
    assert nq["asset_class"] == "equity_index_future"
    assert nq["state"]["symbol"] == "NQ"


def test_instrument_lookup_is_case_insensitive(client):
    response = client.get("/api/v1/instruments/nq")
    assert response.status_code == 200
    assert response.json()["symbol"] == "NQ"


def test_unknown_instrument_returns_404(client):
    response = client.get("/api/v1/instruments/ZZZ")
    assert response.status_code == 404
    assert "unknown instrument" in response.json()["detail"]


def _wait_for_updates(client: TestClient, minimum: int, time_limit: float = 3.0) -> None:
    """Wait until the service has published at least `minimum` updates."""
    deadline = time.time() + time_limit
    while time.time() < deadline:
        if client.get("/health").json()["updates_published"] >= minimum:
            return
        time.sleep(0.02)
    raise AssertionError(f"service did not reach {minimum} updates in time")


def test_history_returns_stored_points(client):
    _wait_for_updates(client, 3)
    response = client.get("/api/v1/history/NQ", params={"limit": 100})
    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "NQ"
    # Every tick stores one NQ observation, so history must hold several.
    assert body["count"] >= 3
    # Oldest first.
    timestamps = [point["timestamp"] for point in body["observations"]]
    assert timestamps == sorted(timestamps)


def test_history_exposes_stored_quote_provenance_not_current_contract(client, monkeypatch):
    observed = datetime(2026, 9, 16, 18, tzinfo=UTC)
    point = MarketObservation(
        symbol="NQ",
        timestamp=observed,
        value=24000.0,
        unit="index points",
        source="cnbc",
        received_at=observed,
        metadata={
            "contract_code": "NQZ26",
            "feed_status": "delayed",
            "exchange_time_known": True,
        },
    )
    monkeypatch.setattr(client.app.state.service, "history", lambda *args, **kwargs: [point])
    response = client.get("/api/v1/history/NQ")
    assert response.status_code == 200
    assert response.json()["observations"][0] == {
        "timestamp": "2026-09-16T18:00:00Z",
        "value": 24000.0,
        "unit": "index points",
        "source": "cnbc",
        "received_at": "2026-09-16T18:00:00Z",
        "contract_code": "NQZ26",
        "feed_status": "delayed",
        "exchange_time_known": True,
    }


def test_history_keeps_absent_legacy_provenance_unknown(client, monkeypatch):
    observed = datetime(2026, 9, 16, 18, tzinfo=UTC)
    point = MarketObservation(
        symbol="NQ",
        timestamp=observed,
        value=24000.0,
        unit="index points",
        source="cnbc",
        received_at=observed,
        metadata={},
    )
    monkeypatch.setattr(client.app.state.service, "history", lambda *args, **kwargs: [point])
    response = client.get("/api/v1/history/NQ")
    assert response.status_code == 200
    saved = response.json()["observations"][0]
    assert saved["contract_code"] is None
    assert saved["feed_status"] is None
    assert saved["exchange_time_known"] is None


def test_history_unknown_symbol_returns_404(client):
    assert client.get("/api/v1/history/ZZZ").status_code == 404


def test_history_rejects_bad_limit(client):
    assert client.get("/api/v1/history/NQ", params={"limit": 0}).status_code == 422


def test_history_limit_is_respected(client):
    _wait_for_updates(client, 5)
    full = client.get("/api/v1/history/NQ", params={"limit": 100}).json()
    assert full["count"] >= 5

    # Pin the upper bound so later ticks cannot change the expected suffix.
    end = full["observations"][-1]["timestamp"]
    limited = client.get("/api/v1/history/NQ", params={"limit": 2, "end": end}).json()
    assert limited["count"] == 2
    # The limit must keep the most recent rows, not the first ones.
    all_timestamps = [p["timestamp"] for p in full["observations"]]
    limited_timestamps = [p["timestamp"] for p in limited["observations"]]
    assert limited_timestamps == all_timestamps[-2:]


def test_history_rejects_naive_datetime(client):
    naive = client.get("/api/v1/history/NQ", params={"start": "2026-01-02T15:00:00"})
    assert naive.status_code == 422


def test_history_accepts_aware_datetime_range(client):
    _wait_for_updates(client, 3)
    aware = client.get(
        "/api/v1/history/NQ",
        params={"start": "2020-01-01T00:00:00Z", "end": "2100-01-01T00:00:00Z"},
    )
    assert aware.status_code == 200


def test_history_rejects_reversed_range(client):
    reversed_range = client.get(
        "/api/v1/history/NQ",
        params={"start": "2026-01-02T16:00:00Z", "end": "2026-01-02T15:00:00Z"},
    )
    assert reversed_range.status_code == 422


def test_websocket_receives_valid_state_update(client):
    with client.websocket_connect("/ws/market") as websocket:
        message = websocket.receive_json()
        assert message["type"] == "state"
        data = message["data"]
        assert set(data["instruments"]) == set(all_symbols())
        assert data["provider"] == "fake"
        assert "generated_at" in data


def test_websocket_receives_multiple_updates(client):
    with client.websocket_connect("/ws/market") as websocket:
        first = websocket.receive_json()
        second = websocket.receive_json()
        assert first["type"] == second["type"] == "state"
        # A later update supersedes an earlier one.
        assert first["data"]["generated_at"] != second["data"]["generated_at"]


def test_websocket_health_counts_subscriber(client):
    with client.websocket_connect("/ws/market") as websocket:
        websocket.receive_json()
        # The subscription is registered server-side.
        assert client.get("/health").json()["subscriber_count"] == 1
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if client.get("/health").json()["subscriber_count"] == 0:
            break
        time.sleep(0.02)
    assert client.get("/health").json()["subscriber_count"] == 0


def test_service_continues_without_websocket_clients(tmp_path):
    with _make_client(tmp_path) as test_client:
        _wait_for_updates(test_client, 2)
        before = test_client.get("/health").json()["updates_published"]
        assert test_client.get("/health").json()["subscriber_count"] == 0
        _wait_for_updates(test_client, before + 2)
        after = test_client.get("/health").json()["updates_published"]
        assert after >= before + 2


def test_history_survives_restart(tmp_path):
    """Storage is durable truth: observations written before a restart remain."""
    db = tmp_path / "seed.duckdb"
    repo = DuckDBObservationRepository(db)
    repo.initialize()
    ts = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)
    repo.store(
        [
            MarketObservation(
                symbol="NQ",
                timestamp=ts,
                value=24000.0,
                unit="index points",
                source="seed",
                received_at=ts,
                metadata={},
            )
        ]
    )
    repo.close()

    settings = Settings(
        duckdb_path=db,
        provider_update_interval=99.0,  # do not tick during the assertion
        provider_seed=1,
    )
    reopened = DuckDBObservationRepository(db)
    provider = FakeMarketDataProvider(seed=1, update_interval=99.0)
    service = build_service(settings=settings, provider=provider, repository=reopened)
    with TestClient(create_app(service=service)) as test_client:
        body = test_client.get("/api/v1/history/NQ").json()
        seeded = [p for p in body["observations"] if p["source"] == "seed"]
        assert seeded, "seeded observation did not survive restart"
        assert seeded[0]["value"] == 24000.0
        assert seeded[0]["timestamp"] == ts.isoformat().replace("+00:00", "Z")


def test_retired_routes_are_gone_but_quote_context_available(client):
    cross = client.get("/api/v1/cross-market")
    assert cross.status_code == 200
    assert "us2y" in cross.json()
    assert "nq_change_pct" in cross.json()
    paths = client.get("/openapi.json").json()["paths"]
    for retired in (
        "/api/v1/technical",
        "/api/v1/decisions",
        "/api/v1/escalations",
        "/api/v1/analyses",
        "/api/v1/analysts",
        "/api/v1/briefings",
        "/api/v1/workflows",
        "/api/v1/historical",
        "/api/v1/research",
        "/api/v1/notifications",
    ):
        assert not any(path.startswith(retired) for path in paths), retired
    assert client.get("/api/v1/briefings/latest").status_code == 404


def test_retired_websocket_topics_are_ignored(client):
    with client.websocket_connect("/ws/market?topics=technical,decisions") as websocket:
        assert websocket.receive_json()["type"] == "state"


