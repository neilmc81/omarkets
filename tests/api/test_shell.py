"""Tests for the shell endpoints and their pure projection builders."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from starlette.testclient import TestClient

from core.api.app import create_app
from core.api.shell_view import (
    aggregate_health,
    build_context,
    build_cross_quotes,
    build_session,
    fmt_local_short,
    build_status,
    build_strip,
    pick_next_event,
)
from core.domain.cross_market import CrossAssetChange, CrossMarketState, Value
from core.domain.economics import (
    EconomicEvent,
    EconomicsMode,
    EventStatus,
    Importance,
    MacroCategory,
)
from core.domain.models import (
    DataStatus,
    FeedStatus,
    InstrumentState,
    MarketState,
    ProviderStatus,
)
from core.domain.shell import SHELL_SCHEMA_VERSION, ShellHealth
from core.providers.fake import FakeMarketDataProvider
from core.services.market_state import MarketStateService
from core.storage.duckdb_repo import DuckDBObservationRepository

NOW = datetime(2026, 9, 20, 14, 0, tzinfo=UTC)


def instrument(symbol: str, value: float, *, feed=FeedStatus.LIVE, status=DataStatus.LIVE):
    return InstrumentState(
        symbol=symbol,
        display_name=f"{symbol} display",
        asset_class="equity_index_future",
        quote_type="price",
        unit="index points",
        value=value,
        change=1.5,
        change_pct=0.42,
        status=status,
        feed_status=feed,
        stale_after_seconds=900,
    )


def market_state() -> MarketState:
    return MarketState(
        generated_at=NOW,
        provider="cnbc",
        provider_status=ProviderStatus.OK,
        instruments={
            "NQ": instrument("NQ", 24780.5),
            "US2Y": instrument("US2Y", 4.92, feed=FeedStatus.DELAYED, status=DataStatus.STALE),
            "VIX": instrument("VIX", 16.8),
        },
        live_count=2,
        stale_count=1,
        unavailable_count=0,
    )


def event(**overrides) -> EconomicEvent:
    values = {
        "id": "econ:1",
        "event_code": "CPI",
        "name": "CPI",
        "category": MacroCategory.INFLATION,
        "importance": Importance.CRITICAL,
        "unit": "percent",
        "frequency": "monthly",
        "status": EventStatus.SCHEDULED,
        "scheduled_at": NOW + timedelta(minutes=38),
        "provider": "bea_calendar",
        "source_event_id": "1",
        "received_at": NOW,
        "mode": EconomicsMode.LIVE,
    }
    values.update(overrides)
    return EconomicEvent.model_validate(values)


# --- pure builders --------------------------------------------------------


def test_strip_copies_values_and_feed_status_verbatim():
    strip = build_strip(market_state(), ("NQ", "US2Y", "VIX"))
    assert [q.symbol for q in strip] == ["NQ", "US2Y", "VIX"]
    assert strip[0].change_pct == 0.42
    # Delayed/stale is preserved, never upgraded to live.
    assert strip[1].feed_status is FeedStatus.DELAYED
    assert strip[1].status is DataStatus.STALE


def test_strip_missing_symbols_are_skipped_not_invented():
    strip = build_strip(market_state(), ("NQ", "GOLD"))
    assert [q.symbol for q in strip] == ["NQ"]


def test_next_event_picks_soonest_future_high_or_critical():
    soon = event(id="a", scheduled_at=NOW + timedelta(hours=2), importance=Importance.HIGH)
    nearer = event(id="b", scheduled_at=NOW + timedelta(minutes=10), importance=Importance.CRITICAL)
    past = event(id="c", scheduled_at=NOW - timedelta(hours=1), importance=Importance.CRITICAL)
    low = event(id="d", scheduled_at=NOW + timedelta(minutes=1), importance=Importance.LOW)
    picked = pick_next_event([soon, nearer, past, low], now=NOW)
    assert picked is not None and picked.id == "b"


def test_next_event_ignores_unknown_time():
    naive = event(id="x", scheduled_at=None, released_at=None)
    assert pick_next_event([naive], now=NOW) is None


def test_event_display_times_are_prerendered_in_et():
    from core.api.shell_view import build_event

    built = build_event(event(scheduled_at=datetime(2026, 9, 30, 12, 30, tzinfo=UTC)))
    assert built.time_et == "08:30 EDT"
    assert built.time_local is not None


def test_session_flags_maintenance_distinctly():
    session = build_session(NOW)
    assert session.phase in {"rth", "overnight", "maintenance", "closed"}
    assert session.maintenance_break is (session.phase == "maintenance")
    assert (session.next_open_at is not None) is (session.phase in {"maintenance", "closed"})
    closed = build_session(datetime(2026, 9, 19, 12, tzinfo=UTC))
    assert closed.next_open_at == datetime(2026, 9, 20, 22, tzinfo=UTC)
    assert closed.next_open_local is not None
    assert build_session(datetime(2026, 9, 19, 12, tzinfo=UTC), ZoneInfo("Asia/Dubai")).next_open_local == "Mon 02:00 UTC+4"
    if session.next_open_at is None:
        assert session.next_open_local is None


@pytest.mark.parametrize(
    ("zone", "expected"),
    [
        ("Asia/Dubai", "Mon 02:00 UTC+4"),  # tzdata has no letters, only "+04"
        ("America/New_York", "Sun 18:00 EDT"),
        ("Europe/London", "Sun 23:00 BST"),
        ("Asia/Kolkata", "Mon 03:30 IST"),
        ("Asia/Kathmandu", "Mon 03:45 UTC+5:45"),
        ("America/St_Johns", "Sun 19:30 NDT"),
        ("America/Sao_Paulo", "Sun 19:00 UTC-3"),
        ("UTC", "Sun 22:00 UTC"),
    ],
)
def test_local_short_time_never_prints_a_long_zone_name(zone, expected):
    assert fmt_local_short(datetime(2026, 9, 20, 22, tzinfo=UTC), ZoneInfo(zone)) == expected


def test_build_status_has_version_and_safe_defaults_with_no_state():
    status = build_status(
        now=NOW,
        market=None,
        cross=None,
        events=[],
        data_health=ShellHealth.DOWN,
        ai_health=ShellHealth.UNKNOWN,
    )
    assert status.version == SHELL_SCHEMA_VERSION
    assert status.primary is None
    assert status.strip == []
    assert status.next_event is None
    assert status.data_health is ShellHealth.DOWN
    dumped = status.model_dump()
    for retired in ("technical", "latest_material", "latest_analysis", "analysis_eligibility"):
        assert retired not in dumped


def test_cross_quotes_prefer_market_state_for_value():
    cross = CrossMarketState(
        generated_at=NOW,
        nq_change_pct=Value.unavailable("stale; relative strength withheld"),
        es_change_pct=Value.unavailable("stale"),
        nq_vs_es=Value.unavailable("stale"),
        tolerance_pct=0.1,
        nq_feed_status=FeedStatus.DELAYED,
        es_feed_status=FeedStatus.DELAYED,
        us2y=CrossAssetChange(
            symbol="US2Y",
            price=Value.available(4.92),
            change=Value.available(0.066),
            change_pct=Value.available(1.4),
            change_bp=Value.available(6.6),
            feed_status=FeedStatus.DELAYED,
            status=DataStatus.STALE,
        ),
        us10y=CrossAssetChange(
            symbol="US10Y",
            price=Value.available(4.27),
            change=Value.available(0.05),
            change_pct=Value.available(1.0),
            change_bp=Value.available(5.3),
            feed_status=FeedStatus.DELAYED,
            status=DataStatus.STALE,
        ),
        dxy=CrossAssetChange(
            symbol="DXY",
            price=Value.available(100.2),
            change=Value.available(-0.03),
            change_pct=Value.available(-0.03),
            change_bp=Value.unavailable("not a yield"),
            feed_status=FeedStatus.DELAYED,
            status=DataStatus.STALE,
        ),
        vix=CrossAssetChange(
            symbol="VIX",
            price=Value.available(14.8),
            change=Value.available(-0.6),
            change_pct=Value.available(-4.0),
            change_bp=Value.unavailable("not a yield"),
            feed_status=FeedStatus.DELAYED,
            status=DataStatus.STALE,
        ),
        vxn=CrossAssetChange(
            symbol="VXN",
            price=Value.available(19.3),
            change=Value.available(-0.6),
            change_pct=Value.available(-3.3),
            change_bp=Value.unavailable("not a yield"),
            feed_status=FeedStatus.DELAYED,
            status=DataStatus.STALE,
        ),
    )
    quotes = build_cross_quotes(cross, ("NQ", "ES", "US2Y"), market_state())
    by = {q.symbol: q for q in quotes}
    # NQ/ES come from the market state even though cross withheld them.
    assert by["NQ"].value == 24780.5
    assert by["NQ"].change_pct == 0.42
    # US2Y gets the market value plus the cross-market basis-point move.
    assert by["US2Y"].change_bp == 6.6


def test_cross_quotes_preserve_unavailable_status():
    """An unavailable feed must not be relabelled as merely stale."""
    cross = CrossMarketState(
        generated_at=NOW,
        nq_change_pct=Value.unavailable("no feed"),
        es_change_pct=Value.unavailable("no feed"),
        nq_vs_es=Value.unavailable("no feed"),
        tolerance_pct=0.1,
        nq_feed_status=FeedStatus.UNAVAILABLE,
        es_feed_status=FeedStatus.UNAVAILABLE,
        us2y=CrossAssetChange(
            symbol="US2Y",
            price=Value.unavailable("x"),
            change=Value.unavailable("x"),
            change_pct=Value.unavailable("x"),
            change_bp=Value.unavailable("x"),
            feed_status=FeedStatus.UNAVAILABLE,
            status=DataStatus.UNAVAILABLE,
        ),
        us10y=CrossAssetChange(
            symbol="US10Y",
            price=Value.unavailable("x"),
            change=Value.unavailable("x"),
            change_pct=Value.unavailable("x"),
            change_bp=Value.unavailable("x"),
            feed_status=FeedStatus.UNAVAILABLE,
            status=DataStatus.UNAVAILABLE,
        ),
        dxy=CrossAssetChange(
            symbol="DXY",
            price=Value.unavailable("x"),
            change=Value.unavailable("x"),
            change_pct=Value.unavailable("x"),
            change_bp=Value.unavailable("x"),
            feed_status=FeedStatus.UNAVAILABLE,
            status=DataStatus.UNAVAILABLE,
        ),
        vix=CrossAssetChange(
            symbol="VIX",
            price=Value.unavailable("x"),
            change=Value.unavailable("x"),
            change_pct=Value.unavailable("x"),
            change_bp=Value.unavailable("x"),
            feed_status=FeedStatus.UNAVAILABLE,
            status=DataStatus.UNAVAILABLE,
        ),
        vxn=CrossAssetChange(
            symbol="VXN",
            price=Value.unavailable("x"),
            change=Value.unavailable("x"),
            change_pct=Value.unavailable("x"),
            change_bp=Value.unavailable("x"),
            feed_status=FeedStatus.UNAVAILABLE,
            status=DataStatus.UNAVAILABLE,
        ),
    )
    quotes = build_cross_quotes(cross, ("NQ",), None)
    assert quotes[0].feed_status is FeedStatus.UNAVAILABLE
    assert quotes[0].status is DataStatus.UNAVAILABLE
    assert quotes[0].value is None


def test_context_builder_drops_past_events():
    past = event(id="p", scheduled_at=NOW - timedelta(days=2))
    future = event(id="f", scheduled_at=NOW + timedelta(days=1))
    ctx = build_context(
        now=NOW, cross=None, market=None, events=[past, future], diagnostics=[]
    )
    assert [e.id for e in ctx.upcoming_events] == ["f"]


# --- endpoint -------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    market = MarketStateService(
        provider=FakeMarketDataProvider(update_interval=1000),
        repository=DuckDBObservationRepository(":memory:"),
    )
    with TestClient(create_app(service=market)) as http:
        yield http


def test_shell_status_endpoint_degrades_safely_without_optional_services(client):
    body = client.get("/api/v1/shell/status").json()
    assert body["version"] == SHELL_SCHEMA_VERSION
    # No economics/analysts configured: safe defaults, not 500.
    assert body["next_event"] is None
    assert "latest_analysis" not in body
    assert "session" in body
    assert body["data_health"] in {"ok", "degraded", "down", "unknown"}
    assert body["ai_health"] in {"ok", "degraded", "disabled", "down", "unknown"}
    assert isinstance(body["strip"], list)


def test_shell_context_endpoint_degrades_safely(client):
    body = client.get("/api/v1/shell/context").json()
    assert body["version"] == SHELL_SCHEMA_VERSION
    # The fake provider supplies market state, so cross-market is populated;
    # with no economics/analysts configured the optional sections stay empty.
    assert isinstance(body["cross_market"], list)
    assert body["upcoming_events"] == []
    assert "recent_analyses" not in body


class _FakeDesk:
    def __init__(self, ready: bool) -> None:
        self.writer = type(
            "Writer", (), {"ready": ready, "detail": None if ready else "no key"}
        )()

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


@pytest.mark.parametrize(("ready", "expected"), [(True, "ok"), (False, "degraded")])
def test_ai_health_reflects_the_desk_writer(ready, expected):
    market = MarketStateService(
        provider=FakeMarketDataProvider(update_interval=1000),
        repository=DuckDBObservationRepository(":memory:"),
    )
    with TestClient(create_app(service=market, desk_service=_FakeDesk(ready))) as http:
        assert http.get("/api/v1/shell/status").json()["ai_health"] == expected
        providers = {p["name"]: p for p in http.get("/api/v1/shell/status").json()["providers"]}
        assert providers["desk"]["health"] == expected
        assert "jev" not in providers and "analysts" not in providers


def test_ai_health_is_disabled_without_the_desk():
    assert aggregate_health(market_healthy=True, writer_ready=None) == (
        ShellHealth.OK,
        ShellHealth.DISABLED,
    )
    # A dead writer never makes market data look down.
    assert aggregate_health(market_healthy=True, writer_ready=False)[0] is ShellHealth.OK


def test_status_carries_the_core_risk_tone_line():
    status = build_status(now=NOW, market=market_state(), cross=None, events=[],
                          data_health=ShellHealth.OK, ai_health=ShellHealth.OK)
    # NQ +0.42% helps; the 2-year is stale and left out; VIX +0.42% is neutral.
    assert status.risk_tone is not None
    assert status.risk_tone.tone == "leaning_risk_on"
    assert status.risk_tone.line.startswith("Risk tone: leaning risk-on · NQ +0.42%")
    unknown = build_status(now=NOW, market=None, cross=None, events=[],
                           data_health=ShellHealth.DOWN, ai_health=ShellHealth.UNKNOWN)
    assert unknown.risk_tone is None
