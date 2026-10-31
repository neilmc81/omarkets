"""Chart focus and watchlist: persistence, routes and the slice-6 gate.

Gate: NQ -> ES -> BTC -> NQ never mixes instruments; switching focus writes no
note and makes no AI call, inside the pre-market window and across a restart.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from core.api import chart, workspace
from core.desk.facts import STRIP_SYMBOLS
from core.desk.models import NoteKind
from core.desk.service import DeskService
from core.domain.instruments import get_instrument
from core.domain.models import MarketObservation
from core.domain.workspace import FocusUpdate, Watchlist
from core.storage.workspace import WorkspaceStore
from tests.api.test_chart_api import StubService, inst
from tests.desk.test_service import FakeWriter, make

# Tuesday Oct 20 2026, 09:05 ET: inside the pre-market window.
PREMARKET = datetime(2026, 10, 20, 13, 5, tzinfo=UTC)


def app_for(store, service=None, desk=None) -> FastAPI:
    app = FastAPI()
    app.include_router(chart.router)
    app.include_router(workspace.router)
    app.state.workspace = store
    app.state.service = service
    app.state.desk = desk
    return app


@pytest.fixture
def store():
    s = WorkspaceStore(":memory:")
    yield s
    s.close()


@pytest.fixture
def client(store):
    with TestClient(app_for(store)) as c:
        yield c


# ------------------------------------------------------------------ defaults


def test_defaults_are_nq_and_the_eight_strip_symbols(client):
    focus = client.get("/api/v1/chart/focus").json()
    assert focus["symbol"] == "NQ" and focus["updated_at"] is None
    assert focus["instrument"] == {"symbol": "NQ", "label": "Nasdaq 100 futures",
                                   "unit": "index points", "price_precision": 2,
                                   "levels": True, "release_pins": True}
    watchlist = client.get("/api/v1/watchlist").json()
    assert watchlist == {"name": "Desk strip", "symbols": list(STRIP_SYMBOLS),
                         "updated_at": None}
    assert len(watchlist["symbols"]) == 8


def test_chart_instruments_say_where_levels_and_pins_exist(client):
    found = {i["symbol"]: i for i in client.get("/api/v1/chart/instruments").json()["instruments"]}
    assert {s for s, i in found.items() if i["levels"]} == {"NQ", "ES", "YM"}
    assert {s for s, i in found.items() if i["release_pins"]} == {"NQ"}
    assert found["US10Y"]["price_precision"] == 3
    assert found["BTC"]["label"] == "Bitcoin"


# ------------------------------------------------------------------ updates


def test_focus_is_validated_and_canonical(client):
    saved = client.put("/api/v1/chart/focus", json={"symbol": "es"}).json()
    assert saved["symbol"] == "ES" and saved["updated_at"] is not None
    assert saved["instrument"]["levels"] is True and saved["instrument"]["release_pins"] is False
    assert client.get("/api/v1/chart/focus").json()["symbol"] == "ES"
    for bad in ({"symbol": "NOPE"}, {"symbol": ""}, {}, {"symbol": "ES", "extra": 1}):
        assert client.put("/api/v1/chart/focus", json=bad).status_code == 422
    assert client.get("/api/v1/chart/focus").json()["symbol"] == "ES"


def test_watchlist_update_validates_against_the_registry(client):
    body = {"name": "  Crypto and rates ", "symbols": ["btc", "US10Y", "NQ"]}
    saved = client.put("/api/v1/watchlist", json=body).json()
    assert saved["name"] == "Crypto and rates" and saved["symbols"] == ["BTC", "US10Y", "NQ"]
    assert client.get("/api/v1/watchlist").json()["symbols"] == ["BTC", "US10Y", "NQ"]
    for bad in ({"name": "x", "symbols": ["NQ", "nq"]},      # repeated
                {"name": "x", "symbols": ["NOPE"]},          # not in the registry
                {"name": "x", "symbols": []},                # empty
                {"name": " ", "symbols": ["NQ"]},            # blank name
                {"name": "x", "symbols": ["NQ"], "id": 2}):  # unknown field
        assert client.put("/api/v1/watchlist", json=bad).status_code == 422
    assert client.get("/api/v1/watchlist").json()["symbols"] == ["BTC", "US10Y", "NQ"]


def test_routes_report_unavailable_without_a_store():
    with TestClient(app_for(None)) as c:
        assert c.get("/api/v1/chart/focus").status_code == 503
        assert c.put("/api/v1/chart/focus", json={"symbol": "ES"}).status_code == 503
        assert c.get("/api/v1/watchlist").status_code == 503
        assert c.get("/api/v1/chart/instruments").status_code == 200


def test_a_stored_value_that_no_longer_validates_reads_as_the_default(tmp_path):
    path = tmp_path / "workspace.sqlite3"
    s = WorkspaceStore(path)
    s._put("chart_focus", '{"symbol": "GONE"}', None)
    s._put("watchlist", '{"name": "Old", "symbols": ["GONE"]}', None)
    with TestClient(app_for(s)) as c:
        assert c.get("/api/v1/chart/focus").json()["symbol"] == "NQ"
        assert c.get("/api/v1/watchlist").json()["name"] == "Desk strip"
    s.close()


@pytest.mark.parametrize("updated_at", ["not a time", "2026-10-31T09:00:00"])
def test_a_bad_stored_timestamp_reads_as_the_default(tmp_path, updated_at):
    s = WorkspaceStore(tmp_path / "workspace.sqlite3")
    s.set_focus(FocusUpdate(symbol="BTC"))
    s.set_watchlist(Watchlist(name="Mine", symbols=["NQ"]))
    s._db.execute("UPDATE workspace SET updated_at=?", (updated_at,))
    s._db.commit()
    with TestClient(app_for(s)) as c:
        focus = c.get("/api/v1/chart/focus")
        watch = c.get("/api/v1/watchlist")
    assert focus.status_code == 200 and focus.json()["symbol"] == "NQ"
    assert watch.status_code == 200 and watch.json()["name"] == "Desk strip"
    s.close()


def test_a_broken_store_file_is_unavailable_not_an_error(tmp_path):
    s = WorkspaceStore(tmp_path / "workspace.sqlite3")
    s._db.execute("DROP TABLE workspace")
    s._db.commit()
    with TestClient(app_for(s)) as c:
        assert c.get("/api/v1/chart/focus").status_code == 503
        assert c.put("/api/v1/chart/focus", json={"symbol": "ES"}).status_code == 503
        assert c.get("/api/v1/watchlist").status_code == 503
    s.close()


def test_focus_and_watchlist_survive_a_restart(tmp_path):
    path = tmp_path / "workspace.sqlite3"
    first = WorkspaceStore(path)
    with TestClient(app_for(first)) as c:
        c.put("/api/v1/chart/focus", json={"symbol": "BTC"})
        c.put("/api/v1/watchlist", json={"name": "Mine", "symbols": ["ES", "BTC"]})
    first.close()
    second = WorkspaceStore(path)
    with TestClient(app_for(second)) as c:
        assert c.get("/api/v1/chart/focus").json()["symbol"] == "BTC"
        assert c.get("/api/v1/watchlist").json()["symbols"] == ["ES", "BTC"]
    second.close()


# ------------------------------------------------------------------ gate


def obs(symbol, ts, value, *, feed="delayed", contract=None, source="cnbc"):
    meta = {"feed_status": feed, **({"contract_code": contract} if contract else {})}
    return MarketObservation(symbol=symbol, timestamp=ts, value=value,
                             unit=get_instrument(symbol).unit, source=source,
                             received_at=ts + timedelta(minutes=10), metadata=meta)


@pytest.fixture
def market(memory_repo):
    """NQ, ES and BTC quotes in today's session, each at a distinct price level."""
    start = datetime(2026, 10, 22, 13, 30, tzinfo=UTC)
    memory_repo.store([
        obs("NQ", start, 29_180.0, contract="NQZ26"),
        obs("NQ", start + timedelta(minutes=12), 29_200.0, contract="NQZ26"),
        obs("ES", start, 6_800.0, contract="ESZ26"),
        obs("ES", start + timedelta(minutes=7), 6_805.0, contract="ESZ26"),
        obs("BTC", start, 110_000.0, feed="live", source="binance"),
        obs("BTC", start + timedelta(minutes=7), 110_250.0, feed="live", source="binance"),
    ])
    return StubService(memory_repo, {
        "NQ": inst("NQ", 29_200.0, settle=29_150.0),
        "ES": inst("ES", 6_805.0, settle=6_790.0),
        "BTC": inst("BTC", 110_250.0, feed="live"),
    })


def test_focus_cycle_never_mixes_instruments(store, market):
    """NQ -> ES -> BTC -> NQ: each step's bars, label, precision, calendar and
    levels are that instrument's only, and the last NQ matches the first."""
    expected = {"NQ": (29_000, 30_000, "Nasdaq 100 futures", True),
                "ES": (6_000, 7_000, "S&P 500 futures", True),
                "BTC": (100_000, 120_000, "Bitcoin", False)}
    seen = []
    with TestClient(app_for(store, market)) as c:
        for symbol in ("NQ", "ES", "BTC", "NQ"):
            focus = c.put("/api/v1/chart/focus", json={"symbol": symbol}).json()
            assert focus["symbol"] == symbol
            shown = c.get("/api/v1/chart/focus").json()["instrument"]
            bars = c.get(f"/api/v1/bars/{shown['symbol']}?interval=5m&range=1d").json()
            low, high, label, has_levels = expected[symbol]
            assert bars["symbol"] == symbol and bars["label"] == label == shown["label"]
            prices = [b[k] for b in bars["bars"] if b["count"] for k in ("open", "close")]
            assert prices and all(low <= p <= high for p in prices), symbol
            # Crypto sessions are UTC days with no cash hours; Globex ones have them.
            assert all((s["rth_opens_at"] is None) == (symbol == "BTC")
                       for s in bars["sessions"])
            assert shown["levels"] is has_levels
            levels = c.get(f"/api/v1/levels/{symbol}")
            assert levels.status_code == (200 if has_levels else 404)
            if has_levels:
                assert levels.json()["symbol"] == symbol
            assert shown["release_pins"] is (symbol == "NQ")
            seen.append(bars)
    assert seen[0] == seen[-1]


async def test_switching_focus_writes_no_note_and_calls_no_ai(tmp_path, monkeypatch):
    """Inside the pre-market window, before and after a restart."""
    writer = FakeWriter()
    desk, _, notes = make(PREMARKET, writer=writer)
    planned = [job[0] for job in await desk.due_jobs(PREMARKET)]
    assert "premarket:2026-10-20" in planned  # the window is open: NQ's note is due

    def forbidden(*args, **kwargs):
        raise AssertionError("a focus change reached the desk")

    for name in ("tick", "ask", "_write", "_run"):
        if hasattr(DeskService, name):
            monkeypatch.setattr(desk, name, forbidden)
    path = tmp_path / "workspace.sqlite3"
    for _restart in range(2):
        store = WorkspaceStore(path)
        with TestClient(app_for(store, desk=desk)) as c:
            assert c.get("/api/v1/chart/focus").json()["symbol"] == "NQ"
            for symbol in ("ES", "BTC", "NQ", "ES", "BTC", "NQ"):
                assert c.put("/api/v1/chart/focus", json={"symbol": symbol}).status_code == 200
            c.put("/api/v1/watchlist", json={"name": "x", "symbols": ["BTC", "ES"]})
        store.close()
    assert writer.calls == [] and notes == []
    assert desk.store.recent(include_failed=True) == []
    # The desk's plan does not follow the focus: still NQ's pre-market only.
    after = await desk.due_jobs(PREMARKET)
    assert [job[0] for job in after] == planned
    assert [job[0] for job in after if job[1] is NoteKind.PREMARKET] == ["premarket:2026-10-20"]


def test_workspace_store_is_its_own_file(tmp_path):
    from core.config.settings import Settings

    settings = Settings(data_dir=tmp_path)
    assert settings.resolved_workspace_db_path == tmp_path / "workspace.sqlite3"
    assert settings.resolved_workspace_db_path != settings.resolved_desk_db_path

