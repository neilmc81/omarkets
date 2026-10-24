from datetime import UTC, datetime, timedelta

from core.domain.models import FeedStatus, MarketObservation
from core.services.market_state import StalePolicy, build_instrument_state

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def observation(**overrides):
    fields = {
        "symbol": "NQ",
        "timestamp": NOW,
        "value": 20000.0,
        "unit": "index points",
        "source": "cnbc",
        "received_at": NOW,
        "metadata": {},
    }
    fields.update(overrides)
    return MarketObservation(**fields)


def test_demo_source_is_feed_demo():
    state = build_instrument_state("NQ", observation(source="fake"), policy=StalePolicy(), now=NOW)
    assert state.feed_status is FeedStatus.DEMO


def test_declared_live_feed_is_live():
    state = build_instrument_state(
        "NQ", observation(metadata={"feed_status": "live"}), policy=StalePolicy(), now=NOW
    )
    assert state.feed_status is FeedStatus.LIVE


def test_declared_delayed_feed_is_delayed_not_live():
    state = build_instrument_state(
        "NQ", observation(metadata={"feed_status": "delayed"}), policy=StalePolicy(), now=NOW
    )
    assert state.feed_status is FeedStatus.DELAYED


def test_old_observation_is_stale_feed_regardless_of_declaration():
    old = observation(timestamp=NOW - timedelta(hours=2), metadata={"feed_status": "live"})
    state = build_instrument_state("NQ", old, policy=StalePolicy(), now=NOW)
    assert state.status.value == "stale"
    assert state.feed_status is FeedStatus.STALE


def test_missing_observation_is_unavailable_feed():
    state = build_instrument_state("NQ", None, policy=StalePolicy(), now=NOW)
    assert state.feed_status is FeedStatus.UNAVAILABLE


def test_contract_and_latency_metadata_pass_through():
    obs = observation(
        metadata={
            "provider_symbol": "NQZ26",
            "contract_code": "NQZ26",
            "contract_expiry": "2026-12-18",
            "feed_status": "live",
        },
        received_at=NOW + timedelta(seconds=2),
    )
    # Evaluated once the quote has been received; a future receipt is unavailable.
    state = build_instrument_state("NQ", obs, policy=StalePolicy(), now=NOW + timedelta(seconds=2))
    assert state.provider_symbol == "NQZ26"
    assert state.contract_code == "NQZ26"
    assert state.contract_expiry == "2026-12-18"
    assert state.latency_seconds == 2.0
