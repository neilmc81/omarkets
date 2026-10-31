"""Global focus: Today's strip and the shell bar follow the saved focus.

Gate: NQ -> ES -> BTC -> NQ moves Today's strip and the shell's primary quote
together, never mixing instruments, and the last NQ payloads equal the first.
Reading or switching the focus never reaches the desk writer or writes a note.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from core.api import chart, focus, shell, workspace
from core.api.focus import TODAY_STRIP_SIZE, read_focus
from core.api.shell_view import STRIP_SYMBOLS, bar_symbols, build_status, focus_symbols
from core.desk import facts as F
from core.desk.facts import STRIP_SYMBOLS as DESK_STRIP
from core.desk.service import DeskService
from core.domain.instruments import get_instrument
from core.domain.models import DataStatus, FeedStatus, InstrumentState, MarketState, ProviderStatus
from core.domain.shell import ShellHealth
from core.domain.workspace import FocusUpdate, Watchlist
from core.storage.workspace import WorkspaceStore
from tests.desk.test_service import FakeWriter, make

NOW = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)
# Tuesday Oct 20 2026, 09:05 ET: inside the pre-market window.
PREMARKET = datetime(2026, 10, 20, 13, 5, tzinfo=UTC)

#: A distinct price level per instrument, so a mixed-up quote is caught.
PRICES = {"NQ": 29_200.0, "ES": 6_805.0, "YM": 47_100.0, "BTC": 110_250.0,
          "US10Y": 4.12, "US2Y": 3.55, "DXY": 99.1, "VIX": 17.2, "WTI": 61.4,
          "GOLD": 4_010.0}


def state_for(symbol: str, value: float) -> InstrumentState:
    inst = get_instrument(symbol)
    return InstrumentState(
        symbol=symbol, display_name=inst.display_name, asset_class=inst.asset_class,
        quote_type=inst.quote_type, unit=inst.unit, value=value, change=value * 0.001,
        change_pct=0.1, timestamp=NOW - timedelta(minutes=1), stale_after_seconds=900,
        status=DataStatus.LIVE, feed_status=FeedStatus.LIVE)


def market_state() -> MarketState:
    return MarketState(
        generated_at=NOW, provider="cnbc", provider_status=ProviderStatus.OK,
        instruments={s: state_for(s, v) for s, v in PRICES.items()},
        live_count=len(PRICES), stale_count=0, unavailable_count=0)


class Market:
    def current_state(self, now=None):
        return market_state()


def app_for(store, *, desk=None, service=None) -> FastAPI:
    app = FastAPI()
    for module in (chart, workspace, focus, shell):
        app.include_router(module.router)
    app.state.workspace = store
    app.state.service = service if service is not None else Market()
    app.state.desk = desk
    app.state.economics = None
    app.state.news = None
    return app


@pytest.fixture
def store():
    s = WorkspaceStore(":memory:")
    yield s
    s.close()


# ------------------------------------------------------------------ pure


def test_focus_symbols_lead_with_the_focus_once():
    assert focus_symbols("ES", ["NQ", "ES", "VIX"], 8) == ["ES", "NQ", "VIX"]
    assert focus_symbols("BTC", ["NQ", "ES", "VIX"], 2) == ["BTC", "NQ"]


def test_bar_symbols_default_is_the_old_nq_bar():
    assert bar_symbols("NQ") == list(STRIP_SYMBOLS) == ["NQ", "US2Y", "VIX"]
    assert bar_symbols("ES") == ["ES", "US2Y", "VIX"]
    assert bar_symbols("NQ", ["BTC", "NQ", "GOLD", "WTI"]) == ["NQ", "BTC", "GOLD"]


def test_primary_is_only_ever_the_focus_quote():
    """A focus with no quote has no primary; it never borrows NQ's."""
    status = build_status(now=NOW, market=market_state(), cross=None, events=[],
                          data_health=ShellHealth.OK, ai_health=ShellHealth.OK, focus="SOX")
    assert status.focus == "SOX" and status.primary is None
    assert [q.symbol for q in status.strip] == ["US2Y", "VIX"]


# ------------------------------------------------------------------ defaults


def test_defaults_without_a_store_are_nq_and_the_desk_strip():
    with TestClient(app_for(None)) as c:
        body = c.get("/api/v1/focus/strip").json()
        status = c.get("/api/v1/shell/status").json()
    assert body["focus"]["symbol"] == "NQ" and body["watchlist_name"] == "Desk strip"
    assert body["symbols"] == list(DESK_STRIP)
    assert status["focus"] == "NQ" and status["primary"]["symbol"] == "NQ"
    assert [q["symbol"] for q in status["strip"]] == ["NQ", "US2Y", "VIX"]


def test_nq_default_matches_the_desk_strip_and_the_old_bar(store):
    """NQ regression: Today's tiles are exactly the desk view's quotes and the
    bar is exactly the old NQ, US2Y, VIX payload."""
    state = market_state()
    old_bar = build_status(now=NOW, market=state, cross=None, events=[],
                           data_health=ShellHealth.OK, ai_health=ShellHealth.OK)
    with TestClient(app_for(store)) as c:
        body = c.get("/api/v1/focus/strip").json()
    desk_quotes = [q.model_dump(mode="json") for q in F.quote_strip(state)]
    assert body["quotes"] == desk_quotes
    assert old_bar.focus == "NQ" and [q.symbol for q in old_bar.strip] == list(STRIP_SYMBOLS)


def test_a_broken_store_reads_as_the_defaults():
    class Broken:
        def focus(self):
            raise sqlite3.OperationalError("disk I/O error")

        watchlist = focus

    found = read_focus(type("State", (), {"workspace": Broken()})())
    assert found.symbol == "NQ" and found.saved_watchlist is None
    with TestClient(app_for(Broken())) as c:
        assert c.get("/api/v1/focus/strip").json()["focus"]["symbol"] == "NQ"
        assert c.get("/api/v1/shell/status").json()["primary"]["symbol"] == "NQ"


def test_no_market_service_leaves_an_empty_strip(store):
    app = app_for(store)
    app.state.service = None
    with TestClient(app) as c:
        body = c.get("/api/v1/focus/strip").json()
    assert body["quotes"] == [] and body["focus"]["symbol"] == "NQ"


# ------------------------------------------------------------------ watchlist


def test_saved_watchlist_drives_the_bar_and_today(store):
    store.set_focus(FocusUpdate(symbol="ES"))
    store.set_watchlist(Watchlist(name="Mine", symbols=["BTC", "ES", "GOLD", "WTI"]))
    with TestClient(app_for(store)) as c:
        body = c.get("/api/v1/focus/strip").json()
        status = c.get("/api/v1/shell/status").json()
        context = c.get("/api/v1/shell/context").json()
    assert body["watchlist_name"] == "Mine"
    assert body["symbols"] == ["ES", "BTC", "GOLD", "WTI"]
    assert [q["symbol"] for q in status["strip"]] == ["ES", "BTC", "GOLD"]
    assert context["cross_market"][0]["symbol"] == "ES"


def test_today_strip_is_capped(store):
    many = ["NQ", "ES", "YM", "BTC", "US10Y", "US2Y", "DXY", "VIX", "WTI", "GOLD"]
    store.set_watchlist(Watchlist(name="Long", symbols=many))
    store.set_focus(FocusUpdate(symbol="GOLD"))
    with TestClient(app_for(store)) as c:
        body = c.get("/api/v1/focus/strip").json()
    assert len(body["symbols"]) == TODAY_STRIP_SIZE and body["symbols"][0] == "GOLD"


# ------------------------------------------------------------------ gate


def test_focus_is_read_off_the_event_loop(store, monkeypatch):
    """The store lock and SQLite read never run on the event loop, so a busy
    workspace writer can't stall every other request."""
    on_loop: list[bool] = []
    real = focus.read_focus

    def spy(state):
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return real(state)

    monkeypatch.setattr(focus, "read_focus", spy)
    monkeypatch.setattr(shell, "read_focus", spy)
    with TestClient(app_for(store)) as c:
        for path in ("/api/v1/focus/strip", "/api/v1/shell/status", "/api/v1/shell/context"):
            assert c.get(path).status_code == 200
    assert on_loop == [False, False, False]


def test_focus_cycle_moves_today_and_the_shell_together(store):
    """NQ -> ES -> BTC -> NQ: Today's first tile, the bar's primary quote and
    the popup's first quote are the focus's own; the last NQ equals the first."""
    seen = []
    with TestClient(app_for(store)) as c:
        for symbol in ("NQ", "ES", "BTC", "NQ"):
            assert c.put("/api/v1/chart/focus", json={"symbol": symbol}).status_code == 200
            today = c.get("/api/v1/focus/strip").json()
            status = c.get("/api/v1/shell/status").json()
            context = c.get("/api/v1/shell/context").json()
            label = get_instrument(symbol).display_name

            assert today["focus"]["symbol"] == symbol
            assert today["symbols"][0] == symbol and today["symbols"].count(symbol) == 1
            assert today["quotes"][0]["symbol"] == symbol
            assert today["quotes"][0]["value"] == PRICES[symbol]
            assert {q["symbol"] for q in today["quotes"]} <= set(today["symbols"])

            assert status["focus"] == symbol
            assert status["primary"]["symbol"] == symbol
            assert status["primary"]["value"] == PRICES[symbol]
            assert status["primary"]["label"] == label
            assert [q["symbol"] for q in status["strip"]] == [symbol, "US2Y", "VIX"]
            assert context["cross_market"][0]["symbol"] == symbol
            for q in [*today["quotes"], *status["strip"], *context["cross_market"]]:
                if q["symbol"] in PRICES:  # VXN has no market quote here
                    assert q["value"] == PRICES[q["symbol"]], q["symbol"]
            seen.append((today, {k: v for k, v in status.items() if k != "generated_at"}))
    assert seen[0] == seen[-1]


async def test_focus_reads_and_switches_never_reach_the_desk(tmp_path, monkeypatch):
    """Inside the pre-market window, before and after a restart."""
    writer = FakeWriter()
    desk, _, notes = make(PREMARKET, writer=writer)
    planned = [job[0] for job in await desk.due_jobs(PREMARKET)]
    assert "premarket:2026-10-20" in planned

    def forbidden(*args, **kwargs):
        raise AssertionError("a focus read or switch reached the desk")

    for name in ("tick", "ask", "view", "_write", "_run"):
        if hasattr(DeskService, name):
            monkeypatch.setattr(desk, name, forbidden)
    path = tmp_path / "workspace.sqlite3"
    for _restart in range(2):
        store = WorkspaceStore(path)
        with TestClient(app_for(store, desk=desk)) as c:
            for symbol in ("ES", "BTC", "NQ"):
                assert c.put("/api/v1/chart/focus", json={"symbol": symbol}).status_code == 200
                for route in ("/api/v1/focus/strip", "/api/v1/shell/status",
                              "/api/v1/shell/context"):
                    assert c.get(route).status_code == 200, route
        store.close()
    assert writer.calls == [] and notes == []
    assert desk.store.recent(include_failed=True) == []
    assert [job[0] for job in await desk.due_jobs(PREMARKET)] == planned


# ------------------------------------------------------------------ sparklines


class SparkService(Market):
    def ohlc(self, symbol, *, start, end, bucket_seconds, within=()):
        return []


def test_sparklines_follow_the_symbols_asked_for(store):
    with TestClient(app_for(store, service=SparkService())) as c:
        default = c.get("/api/v1/sparklines").json()
        asked = c.get("/api/v1/sparklines?symbols=btc,ES,BTC").json()
        unknown = c.get("/api/v1/sparklines?symbols=NQ,NOPE")
        empty = c.get("/api/v1/sparklines?symbols=,")
    assert [line["symbol"] for line in default["lines"]] == list(DESK_STRIP)
    assert [line["symbol"] for line in asked["lines"]] == ["BTC", "ES"]
    assert unknown.status_code == 422 and "NOPE" in unknown.json()["detail"]
    assert empty.status_code == 422
