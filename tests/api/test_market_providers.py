from datetime import UTC, datetime

from starlette.testclient import TestClient

from core.api.app import create_app
from core.domain.models import MarketObservation
from core.providers.base import MarketDataProvider, ProviderHealth
from core.services.market_state import MarketStateService, StalePolicy
from core.storage.duckdb_repo import DuckDBObservationRepository

NOW = datetime(2026, 9, 18, 6, 15, tzinfo=UTC)


class StubProvider(MarketDataProvider):
    @property
    def name(self):
        return "cnbc"

    @property
    def instruments(self):
        return ()

    async def fetch_snapshot(self):
        return []

    async def stream_updates(self):
        if False:  # pragma: no cover
            yield []

    async def health(self):
        return ProviderHealth(name=self.name, healthy=True, checked_at=NOW, detail=None)

    async def aclose(self):
        return None


def observation(symbol, value, **metadata):
    unit = {"BTC": "USD"}.get(symbol, "index points")
    return MarketObservation(
        symbol=symbol,
        timestamp=NOW,
        value=value,
        unit=unit,
        source="cnbc",
        received_at=NOW,
        metadata={"feed_status": "live", **metadata},
    )


def client_with(observations):
    service = MarketStateService(
        provider=StubProvider(),
        repository=DuckDBObservationRepository(":memory:"),
        policy=StalePolicy(),
        clock=lambda: NOW,
    )
    for item in observations:
        service._accept_observation(item)
    app = create_app(service=service)
    return TestClient(app)


def test_markets_status_reports_feed_and_source():
    with client_with(
        [observation("NQ", 20000.0, provider_symbol="NQZ26", contract_code="NQZ26")]
    ) as http:
        body = http.get("/api/v1/markets/status").json()
    rows = {row["symbol"]: row for row in body["markets"]}
    assert rows["NQ"]["feed_status"] == "live"
    assert rows["NQ"]["source"] == "cnbc"
    assert rows["NQ"]["provider_symbol"] == "NQZ26"
    assert rows["NQ"]["contract_code"] == "NQZ26"
    assert rows["BTC"]["value"] is None
    assert rows["BTC"]["feed_status"] == "unavailable"


def test_providers_endpoint_lists_routes_and_feed_status():
    with client_with([observation("NQ", 20000.0)]) as http:
        body = http.get("/api/v1/market/providers").json()
    assert body["provider_mode"] == "cnbc"
    routes = {route["symbol"]: route for route in body["routes"]}
    assert routes["NQ"]["provider"] == "cnbc"
    assert routes["NQ"]["feed_status"] == "live"
    assert routes["BTC"]["provider"] == "binance"


def test_contracts_endpoint_returns_active_contracts():
    with client_with([observation("NQ", 20000.0, provider_symbol="NQZ26")]) as http:
        body = http.get("/api/v1/contracts").json()
    contracts = {item["symbol"]: item for item in body["contracts"]}
    assert contracts["NQ"]["contract_code"].startswith("NQ")
    assert contracts["NQ"]["expiry"]
    assert contracts["ES"]["contract_code"].startswith("ES")


def test_state_includes_feed_status_fields():
    with client_with([observation("NQ", 20000.0)]) as http:
        body = http.get("/api/v1/state").json()
    assert body["instruments"]["NQ"]["feed_status"] == "live"
    assert "provider_symbol" in body["instruments"]["NQ"]
    assert "latency_seconds" in body["instruments"]["NQ"]
