import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from core.providers.cnbc import CNBCMarketDataProvider, _ProviderFailure, _RateLimited

NOW = datetime(2026, 9, 18, 6, 15, tzinfo=UTC)


async def test_weekend_stream_does_not_request_quotes():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"FormattedQuoteResult": {"FormattedQuote": []}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    p = CNBCMarketDataProvider(
        client=client,
        clock=lambda: datetime(2026, 9, 19, 12, tzinfo=UTC),
        update_interval=0.01,
    )
    task = asyncio.create_task(anext(p.stream_updates()))
    try:
        await asyncio.sleep(0.02)
        assert calls == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await client.aclose()


def record(symbol, last, **overrides):
    base = {
        "symbol": symbol,
        "last": last,
        "change": "1.0",
        "change_pct": "0.5%",
        "last_time": "2026-09-18T02:08:23.000-0400",
        "last_timedate": "2:08 AM EDT",
        "curmktstatus": "REG_MKT",
        "exchange": "Test",
        "previous_day_closing": "100",
        "open": "99",
        "high": "102",
        "low": "98",
    }
    base.update(overrides)
    return base


def payload(groups):
    return {
        "FormattedQuoteResult": {
            "FormattedQuote": [
                row
                for group, symbols in groups.items()
                for row in ([record(s, *v) for s, v in symbols] if group else [])
            ]
        }
    }


def provider(handler, **kwargs):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return CNBCMarketDataProvider(client=client, clock=lambda: NOW, **kwargs), client


async def test_normalizes_futures_indices_and_yields():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "FormattedQuoteResult": {
                    "FormattedQuote": [
                        record("NQZ26", "29,867.75"),
                        record("ESZ26", "7,728.50"),
                        record(".DXY", "100.287"),
                        record("US2Y", "4.688%", change="-0.002"),
                        record("US10Y", "4.943%", change="-0.004"),
                        record(".VIX", "15.44"),
                    ]
                }
            },
        )

    p, client = provider(handler)
    try:
        observations = {o.symbol: o for o in await p.fetch_snapshot()}
    finally:
        await client.aclose()
    assert set(observations) == {"NQ", "ES", "DXY", "US2Y", "US10Y", "VIX"}
    assert observations["NQ"].value == 29867.75
    assert observations["NQ"].unit == "index points"
    assert observations["NQ"].metadata["contract_code"] == "NQZ26"
    assert observations["NQ"].metadata["provider_symbol"] == "NQZ26"
    assert observations["US2Y"].value == 4.688
    assert observations["US2Y"].unit == "percent"
    assert observations["DXY"].value == 100.287
    assert observations["VIX"].value == 15.44
    assert all(o.source == "cnbc" for o in observations.values())


async def test_lagging_exchange_timestamp_is_delayed_not_live():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "FormattedQuoteResult": {
                    "FormattedQuote": [
                        record("NQZ26", "29,867.75", last_time="2026-09-18T01:55:00.000-0400")
                    ]
                }
            },
        )

    p, client = provider(handler, live_lag_seconds=60.0)
    try:
        observation = (await p.fetch_snapshot())[0]
    finally:
        await client.aclose()
    assert observation.metadata["feed_status"] == "delayed"
    assert observation.metadata["delayed"] is True
    assert "delayed" in observation.metadata["stale_reason"]


async def test_fresh_exchange_timestamp_retains_declared_delay():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "FormattedQuoteResult": {
                    "FormattedQuote": [
                        record("NQZ26", "29,867.75", last_time="2026-09-18T02:14:30.000-0400")
                    ]
                }
            },
        )

    p, client = provider(handler, live_lag_seconds=60.0)
    try:
        observation = (await p.fetch_snapshot())[0]
    finally:
        await client.aclose()
    assert observation.metadata["feed_status"] == "delayed"
    assert observation.metadata["expected_delay_seconds"] == 600


async def test_out_of_session_exchange_timestamp_is_stale():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "FormattedQuoteResult": {
                    "FormattedQuote": [
                        record(
                            "NQZ26",
                            "29,867.75",
                            last_time="2026-09-18T02:14:30.000-0400",
                            curmktstatus="CLOSED",
                        )
                    ]
                }
            },
        )

    p, client = provider(handler)
    try:
        observation = (await p.fetch_snapshot())[0]
    finally:
        await client.aclose()
    assert observation.metadata["feed_status"] == "stale"


async def test_date_only_value_is_marked_stale_without_invented_time():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "FormattedQuoteResult": {
                    "FormattedQuote": [
                        record(
                            ".VIX", "15.44", last_time="2026-09-17", last_timedate="09/17/26 EDT"
                        )
                    ]
                }
            },
        )

    p, client = provider(handler)
    try:
        observation = (await p.fetch_snapshot())[0]
    finally:
        await client.aclose()
    assert observation.symbol == "VIX"
    assert observation.metadata["feed_status"] == "stale"
    assert observation.metadata["exchange_time_known"] is False
    assert "unavailable" in observation.metadata["stale_reason"]


async def test_rejects_impossible_yield_and_volatility_values():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "FormattedQuoteResult": {
                    "FormattedQuote": [
                        record("US2Y", "4688%"),
                        record(".VIX", "-1.0"),
                        record("NQZ26", "0"),
                    ]
                }
            },
        )

    p, client = provider(handler)
    try:
        observations = {o.symbol: o for o in await p.fetch_snapshot()}
    finally:
        await client.aclose()
    assert "US2Y" not in observations
    assert "VIX" not in observations
    assert observations["NQ"].value == 0


async def test_unmapped_and_unknown_symbols_are_ignored():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "FormattedQuoteResult": {
                    "FormattedQuote": [
                        record("NQZ26", "29,867.75"),
                        record("WEIRD", "1"),
                    ]
                }
            },
        )

    p, client = provider(handler)
    try:
        observations = await p.fetch_snapshot()
    finally:
        await client.aclose()
    assert [o.symbol for o in observations] == ["NQ"]


async def test_rate_limit_and_malformed_payload_fail_cleanly():
    def limited(request):
        return httpx.Response(429)

    p, client = provider(limited)
    try:
        with pytest.raises(_RateLimited):
            await p.fetch_snapshot()
    finally:
        await client.aclose()

    def malformed(request):
        return httpx.Response(200, json={"unexpected": True})

    p, client = provider(malformed)
    try:
        with pytest.raises(_ProviderFailure):
            await p.fetch_snapshot()
    finally:
        await client.aclose()


async def test_stream_records_poll_failure_in_health(monkeypatch):
    p, client = provider(lambda request: httpx.Response(200, json={}))
    attempted = asyncio.Event()
    never = asyncio.Event()

    async def collect_fails():
        raise _ProviderFailure("upstream unavailable")

    async def pause(_seconds):
        attempted.set()
        await never.wait()

    monkeypatch.setattr(p, "_collect", collect_fails)
    monkeypatch.setattr("core.providers.cnbc.asyncio.sleep", pause)
    stream = p.stream_updates()
    task = asyncio.create_task(stream.__anext__())
    try:
        await asyncio.wait_for(attempted.wait(), timeout=1)
        health = await p.health()
        assert health.healthy is False
        assert health.detail == "_ProviderFailure"
        assert p.diagnostics()["consecutive_failures"] == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await stream.aclose()
        await client.aclose()


async def test_vendor_symbols_use_active_contract():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json={"FormattedQuoteResult": {"FormattedQuote": [record("NQZ26", "1")]}},
        )

    p, client = provider(handler, symbols=("NQ", "ES"))
    try:
        await p.fetch_snapshot()
    finally:
        await client.aclose()
    assert "NQZ26" in seen["url"]
    assert "ESZ26" in seen["url"]


async def test_multiple_calls_produce_multiple_observations_no_duplicates():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={"FormattedQuoteResult": {"FormattedQuote": [record("NQZ26", "100")]}},
        )

    p, client = provider(handler, symbols=("NQ",))
    try:
        first = await p.fetch_snapshot()
        second = await p.fetch_snapshot()
    finally:
        await client.aclose()
    assert len(first) == len(second) == 1
    assert len(calls) == 2


async def test_closed_stream_rechecks_wall_clock_promptly_after_laptop_resume(monkeypatch):
    now = [datetime(2026, 10, 3, 12, tzinfo=UTC)]
    sleeps = []
    batches = [[object()]]
    p = CNBCMarketDataProvider(clock=lambda: now[0])

    async def sleep(seconds):
        sleeps.append(seconds)
        # Wall time jumps while the sleeping laptop's monotonic timer remains
        # pending. The stream must recheck within one bounded sleep on resume.
        now[0] = datetime(2026, 10, 5, 12, 8, tzinfo=UTC)

    async def collect():
        return batches[0]

    monkeypatch.setattr("core.providers.cnbc.asyncio.sleep", sleep)
    monkeypatch.setattr(p, "_collect", collect)
    stream = p.stream_updates()
    assert await anext(stream) == batches[0]
    assert sleeps == [30.0]
    await stream.aclose()


@pytest.mark.parametrize(
    "symbol,delay", [("NQ", 600), ("ES", 600), ("WTI", 600), ("GOLD", 600), ("VXN", 900)]
)
async def test_known_provider_delay_provenance(symbol, delay):
    from datetime import timedelta

    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    p = CNBCMarketDataProvider(client=client, clock=lambda: NOW)
    try:
        row = record(symbol, "100", last_time=(NOW - timedelta(seconds=delay)).isoformat())
        obs = p._normalize(symbol, row, NOW)
        assert obs is not None
        assert obs.metadata["feed_status"] == "delayed"
        assert obs.metadata["expected_delay_seconds"] == delay
        assert obs.metadata["baseline_period"] == "2026-09-18"
    finally:
        await client.aclose()


@pytest.mark.parametrize("symbol", ["NQ", "ES"])
async def test_normal_ten_minute_delay_is_usable_with_original_times(symbol):
    from datetime import timedelta

    from core.domain.models import DataStatus
    from core.services.market_state import StalePolicy, build_instrument_state

    p, client = provider(lambda request: httpx.Response(200, json={}))
    try:
        exchange_at = NOW - timedelta(seconds=600)
        observation = p._normalize(symbol, record(symbol, "100", last_time=exchange_at.isoformat()),
                                   NOW)
        assert observation is not None
        state = build_instrument_state(symbol, observation, policy=StalePolicy(), now=NOW)
        assert state.status is DataStatus.LIVE
        assert state.feed_status.value == "delayed"
        assert state.timestamp == exchange_at
        assert state.received_at == NOW
        assert state.latency_seconds == 600
        assert state.excess_age_seconds == 0
        stale = build_instrument_state(symbol, observation, policy=StalePolicy(),
                                       now=NOW + timedelta(seconds=61))
        assert stale.status is DataStatus.STALE
        assert stale.timestamp == exchange_at
    finally:
        await client.aclose()


async def test_wti_requests_the_explicit_crude_contract_not_the_continuous_symbol():
    """Regression (Oct 20, 2026): ``@CL.1`` served the expired November contract's
    last trade for eight hours. WTI must name a live contract month."""
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(
            200, json={"FormattedQuoteResult": {"FormattedQuote": [record("CLX26", "71.5")]}}
        )

    p, client = provider(handler, symbols=("WTI",))
    try:
        observations = await p.fetch_snapshot()
    finally:
        await client.aclose()
    # NOW is Sep 18, 2026: November crude (last trade Oct 20) is the front month.
    assert "CLX26" in seen["url"]
    assert "@CL.1" not in seen["url"]
    assert observations[0].symbol == "WTI"
    assert observations[0].metadata["contract_code"] == "CLX26"
    assert observations[0].metadata["contract_expiry"] == "2026-10-20"


def test_expected_delays_live_in_the_adapter_table():
    from core.providers.cnbc import EXPECTED_DELAY_SECONDS

    # The values the inline literal held before Phase 0 moved them here.
    assert EXPECTED_DELAY_SECONDS == {"NQ": 600, "ES": 600, "WTI": 600, "GOLD": 600, "VXN": 900}


async def test_a_declared_delay_reads_delayed_not_stale(monkeypatch):
    """Phase 0 gate: a test instrument with a declared delay reads delayed, not stale.

    DXY stands in for a new CNBC symbol whose feed runs 12 minutes behind.
    Without a table entry the quote turns stale; with one it is a usable,
    clearly labelled delayed quote.
    """
    from datetime import timedelta

    from core.desk.facts import quote_strip
    from core.domain.models import DataStatus, FeedStatus
    from core.providers import cnbc
    from core.services.market_state import StalePolicy, build_instrument_state, build_market_state

    p, client = provider(lambda request: httpx.Response(200, json={}))
    row = record(".DXY", "101.5", last_time=(NOW - timedelta(minutes=12)).isoformat())

    def read():
        observation = p._normalize("DXY", row, NOW)
        state = build_instrument_state("DXY", observation, policy=StalePolicy(), now=NOW)
        market = build_market_state({"DXY": observation}, policy=StalePolicy(), now=NOW,
                                    provider="cnbc")
        (quote,) = quote_strip(market, ("DXY",))
        return state, quote

    try:
        undeclared, quote = read()
        assert undeclared.status is DataStatus.STALE
        assert undeclared.expected_delay_seconds == 0
        assert quote.stale

        monkeypatch.setitem(cnbc.EXPECTED_DELAY_SECONDS, "DXY", 900)
        declared, quote = read()
        assert declared.status is DataStatus.LIVE
        assert declared.feed_status is FeedStatus.DELAYED
        assert declared.expected_delay_seconds == 900
        assert not declared.market_closed
        assert quote.delayed and not quote.stale and not quote.closed
    finally:
        await client.aclose()
