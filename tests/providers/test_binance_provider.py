from datetime import UTC, datetime

import httpx
import pytest

from core.providers.binance import BinanceCryptoProvider

NOW = datetime(2026, 9, 18, 6, 15, tzinfo=UTC)


def provider(handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return BinanceCryptoProvider(client=client, clock=lambda: NOW), client


async def test_normalizes_btc_spot():
    def handler(request):
        assert request.url.params["symbol"] == "BTCUSDT"
        return httpx.Response(
            200,
            json={
                "symbol": "BTCUSDT",
                "lastPrice": "77552.43",
                "prevClosePrice": "76456.00",
            },
        )

    p, client = provider(handler)
    try:
        observations = await p.fetch_snapshot()
    finally:
        await client.aclose()
    assert len(observations) == 1
    btc = observations[0]
    assert btc.symbol == "BTC"
    assert btc.value == 77552.43
    assert btc.unit == "USD"
    assert btc.source == "binance"
    assert btc.metadata["previous_close"] == 76456.00
    assert btc.metadata["feed_status"] == "live"


async def test_missing_or_invalid_price_fails():
    def handler(request):
        return httpx.Response(200, json={"symbol": "BTCUSDT", "lastPrice": "0"})

    p, client = provider(handler)
    try:
        with pytest.raises(RuntimeError):
            await p.fetch_snapshot()
    finally:
        await client.aclose()


async def test_rate_limit_fails_cleanly():
    def handler(request):
        return httpx.Response(429)

    p, client = provider(handler)
    try:
        with pytest.raises(RuntimeError):
            await p.fetch_snapshot()
    finally:
        await client.aclose()
