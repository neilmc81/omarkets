import sys
from datetime import timedelta
from pathlib import Path

import uvicorn

from core.api.app import create_app
from core.domain.economics import MacroDefinition, MacroRegistry
from core.providers.economic_demo import DemoEconomicProvider
from core.providers.fake import FakeMarketDataProvider
from core.services.economics import EconomicService
from core.services.market_state import MarketStateService
from core.storage.duckdb_repo import DuckDBObservationRepository
from core.storage.economics import SQLiteEconomicRepository
from tests.services.test_economics import T0, FakeClock

clock = FakeClock()
folder = Path(sys.argv[2])
demo = DemoEconomicProvider(clock=clock)
economics = EconomicService(
    repository=SQLiteEconomicRepository(folder / "economics.sqlite3", "demo"),
    registry=MacroRegistry(
        definitions=[
            MacroDefinition(
                id="CPI_MOM",
                name="DEMO CPI",
                category="inflation",
                unit="percent",
                frequency="monthly",
                provider="demo",
                source_series_id="demo:CPI_MOM",
            )
        ]
    ),
    mode="demo",
    clock=clock,
)
market = MarketStateService(
    provider=FakeMarketDataProvider(clock=clock, update_interval=1000),
    repository=DuckDBObservationRepository(folder / "market.duckdb"),
    clock=clock,
)
app = create_app(service=market, economic_service=economics)


@app.post("/test/stage/{seconds}")
async def stage(seconds: int):
    if seconds not in (0, 30, 60):
        raise ValueError("invalid fixture stage")
    clock.now = T0 + timedelta(seconds=seconds)
    economics.accept_batch(await demo.fetch())
    return {"seconds": seconds, "subscribers": economics.subscriber_count}


uvicorn.run(
    app, host="127.0.0.1", port=int(sys.argv[1]), access_log=False, timeout_graceful_shutdown=3
)
