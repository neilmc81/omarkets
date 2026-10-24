"""Closed-market state: weekend and daily-break quotes are "closed", not stale.

Quotes, the desk strip, risk tone and economic provider health all use the
session engine, so a quiet weekend never shows as broken feeds, while an old
quote during an open market and a failing provider still do.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from core.analysis.risk_tone import risk_tone
from core.desk import facts as F
from core.domain.economics import EconomicProviderStatus
from core.domain.instruments import INSTRUMENTS
from core.domain.models import DataStatus, MarketObservation
from core.domain.sessions import closed_since_last_session, market_closed
from core.services.market_state import StalePolicy, build_instrument_state, build_market_state
from tests.services.test_economics import FakeClock, StubProvider, batch, service

# Friday 23 Oct 2026, 16:58 ET: the last print before the weekend close.
FRIDAY_CLOSE = datetime(2026, 10, 23, 20, 58, tzinfo=UTC)
# Saturday 24 Oct 2026, 12:00 ET: futures closed until Sunday 18:00 ET.
SATURDAY = datetime(2026, 10, 24, 16, 0, tzinfo=UTC)
# Thursday 22 Oct 2026, 10:00 ET: RTH, market open.
THURSDAY_OPEN = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)
# Thursday 22 Oct 2026, 17:30 ET: the daily maintenance break.
THURSDAY_BREAK = datetime(2026, 10, 22, 21, 30, tzinfo=UTC)
THURSDAY_CLOSE = datetime(2026, 10, 22, 20, 58, tzinfo=UTC)


def obs(symbol: str, at: datetime, value: float = 100.0) -> MarketObservation:
    return MarketObservation(symbol=symbol, timestamp=at, value=value,
                             unit=INSTRUMENTS[symbol].unit, source="cnbc", received_at=at,
                             metadata={"previous_close": value * 0.99})


def state_of(symbol: str, at: datetime, now: datetime):
    return build_instrument_state(symbol, obs(symbol, at), policy=StalePolicy(), now=now)


def test_session_helpers():
    assert market_closed(SATURDAY) and market_closed(THURSDAY_BREAK)
    assert not market_closed(THURSDAY_OPEN)
    assert closed_since_last_session(FRIDAY_CLOSE, SATURDAY)
    # A feed that died earlier in the week is not excused by the weekend.
    assert not closed_since_last_session(FRIDAY_CLOSE - timedelta(days=2), SATURDAY)


def test_weekend_quote_is_closed_and_not_an_issue():
    nq = state_of("NQ", FRIDAY_CLOSE, SATURDAY)
    assert nq.status is DataStatus.STALE and nq.market_closed
    market = build_market_state({"NQ": obs("NQ", FRIDAY_CLOSE)}, policy=StalePolicy(),
                                now=SATURDAY, provider="cnbc")
    (quote,) = F.quote_strip(market, ("NQ",))
    assert quote.closed and not quote.stale and quote.closed_text == "Markets closed"


def test_open_market_old_quote_is_still_stale_and_an_issue():
    nq = state_of("NQ", THURSDAY_OPEN - timedelta(hours=1), THURSDAY_OPEN)
    assert nq.status is DataStatus.STALE and not nq.market_closed
    market = build_market_state({"NQ": obs("NQ", THURSDAY_OPEN - timedelta(hours=1))},
                                policy=StalePolicy(), now=THURSDAY_OPEN, provider="cnbc")
    (quote,) = F.quote_strip(market, ("NQ",))
    assert quote.stale and not quote.closed


def test_maintenance_break_is_closed():
    assert state_of("NQ", THURSDAY_CLOSE, THURSDAY_BREAK).market_closed


def test_crypto_trades_through_the_weekend():
    assert not state_of("BTC", FRIDAY_CLOSE, SATURDAY).market_closed


def test_risk_tone_says_markets_closed():
    market = build_market_state(
        {s: obs(s, FRIDAY_CLOSE) for s in ("NQ", "US2Y", "DXY", "VIX")},
        policy=StalePolicy(), now=SATURDAY, provider="cnbc")
    tone = risk_tone(market, now=SATURDAY)
    assert tone.tone == "closed" and tone.label == "markets closed"
    assert tone.reasons[0].startswith("Markets closed")


# ----------------------------------------------------------------- economics

def _economics(now: datetime):
    clock = FakeClock()
    clock.now = now
    return clock, service(clock=clock)


def test_provider_check_paused_by_closed_market_stays_healthy():
    clock, svc = _economics(SATURDAY)
    svc.accept_batch(batch(checked_at=SATURDAY))
    interval = svc.polling.interval("bls")
    clock.now = SATURDAY + timedelta(seconds=interval * 2 + 1800)
    (health,) = [h for h in svc.provider_health() if h.name == "bls"]
    assert health.status is EconomicProviderStatus.HEALTHY
    assert svc.current_state().series[0].status is DataStatus.LIVE


def test_provider_check_overdue_while_open_is_degraded():
    clock, svc = _economics(THURSDAY_OPEN - timedelta(days=2))
    start = clock.now
    svc.accept_batch(batch(checked_at=start))
    interval = svc.polling.interval("bls")
    clock.now = start + timedelta(seconds=interval * 2 + 60)
    assert market_closed(clock.now) is False
    (health,) = [h for h in svc.provider_health() if h.name == "bls"]
    assert health.status is EconomicProviderStatus.DEGRADED


async def test_provider_error_while_closed_is_still_an_issue():
    _, svc = _economics(SATURDAY)
    await svc.check_provider(StubProvider("bls", RuntimeError("boom")))
    (health,) = [h for h in svc.provider_health() if h.name == "bls"]
    assert health.status is EconomicProviderStatus.UNAVAILABLE


def test_desk_strip_handles_plain_namespace_without_market_closed():
    state = SimpleNamespace(generated_at=THURSDAY_OPEN, instruments={
        "NQ": SimpleNamespace(value=1.0, change=None, change_pct=None, feed_status="live",
                              status="stale", timestamp=THURSDAY_OPEN, display_name="x")})
    (quote,) = F.quote_strip(state, ("NQ",))
    assert quote.stale and not quote.closed
