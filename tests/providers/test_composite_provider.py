from datetime import UTC, datetime

import pytest

from core.domain.models import MarketObservation
from core.providers.base import MarketDataProvider, ProviderHealth
from core.providers.composite import CompositeMarketDataProvider

NOW = datetime(2026, 9, 18, 6, 15, tzinfo=UTC)


def observation(symbol, value=1.0):
    unit = {"BTC": "USD", "GOLD": "USD per troy ounce"}.get(symbol, "index points")
    return MarketObservation(
        symbol=symbol,
        timestamp=NOW,
        value=value,
        unit=unit,
        source="stub",
        received_at=NOW,
    )


class StubProvider(MarketDataProvider):
    def __init__(self, name, batches, *, symbol="NQ", fail=False):
        self._name = name
        self._symbol = symbol
        self._batches = batches
        self._fail = fail
        self.started = 0
        self.closed = 0

    @property
    def name(self):
        return self._name

    @property
    def instruments(self):
        return ()

    async def start(self):
        self.started += 1

    async def fetch_snapshot(self):
        if self._fail:
            raise RuntimeError("boom")
        return [observation(self._symbol)]

    async def stream_updates(self):
        if self._fail:
            raise RuntimeError("stream boom")
        for batch in self._batches:
            yield batch

    async def health(self):
        return ProviderHealth(name=self._name, healthy=not self._fail, checked_at=NOW, detail=None)

    async def aclose(self):
        self.closed += 1


async def test_composite_merges_child_streams():
    children = [
        StubProvider("a", [[observation("NQ")], [observation("ES")]], symbol="NQ"),
        StubProvider("b", [[observation("BTC")]], symbol="BTC"),
    ]
    composite = CompositeMarketDataProvider(children)
    symbols = []
    async for batch in composite.stream_updates():
        symbols.extend(o.symbol for o in batch)
        if len(symbols) >= 3:
            break
    assert set(symbols) == {"NQ", "ES", "BTC"}
    await composite.aclose()
    assert [child.closed for child in children] == [1, 1]


async def test_composite_one_failing_child_does_not_stop_others():
    children = [
        StubProvider("a", [[observation("NQ")]], symbol="NQ"),
        StubProvider("b", [], symbol="ES", fail=True),
    ]
    composite = CompositeMarketDataProvider(children)
    batches = []
    async for batch in composite.stream_updates():
        batches.append(batch)
        break
    assert [o.symbol for o in batches[0]] == ["NQ"]
    await composite.aclose()


async def test_composite_snapshot_tolerates_child_failure():
    composite = CompositeMarketDataProvider(
        [
            StubProvider("a", [], symbol="NQ"),
            StubProvider("b", [], symbol="ES", fail=True),
        ]
    )
    observations = await composite.fetch_snapshot()
    assert [o.symbol for o in observations] == ["NQ"]
    await composite.aclose()


async def test_composite_rejects_duplicate_names_and_empty():
    with pytest.raises(ValueError):
        CompositeMarketDataProvider([StubProvider("a", []), StubProvider("a", [])])
    with pytest.raises(ValueError):
        CompositeMarketDataProvider([])


async def test_composite_health_aggregates_children():
    composite = CompositeMarketDataProvider(
        [StubProvider("a", []), StubProvider("b", [], fail=True)]
    )
    report = await composite.health()
    assert report.healthy is False
    assert "a:ok" in report.detail
    assert "b:degraded" in report.detail
    await composite.aclose()


async def test_composite_start_starts_every_child():
    children = [StubProvider("a", []), StubProvider("b", [])]
    composite = CompositeMarketDataProvider(children)
    await composite.start()
    assert [child.started for child in children] == [1, 1]
    await composite.aclose()
