"""Chart API contracts: bars, session, levels, risk tone, pins, sparklines."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from core.api import chart
from core.domain.models import MarketObservation

# Thursday Oct 22 2026, 11:00 ET.
NOW = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)


def obs(symbol, ts, value, feed="delayed", contract="NQZ26"):
    return MarketObservation(symbol=symbol, timestamp=ts, value=value, unit="index points",
                             source="cnbc", received_at=ts + timedelta(minutes=10),
                             metadata={"feed_status": feed, "contract_code": contract})


def inst(symbol, value, *, pct=0.0, change=0.0, status="live", feed="delayed", settle=None):
    return SimpleNamespace(symbol=symbol, display_name=symbol, unit="index points", value=value,
                           change=change, change_pct=pct, status=status, feed_status=feed,
                           contract_code="NQZ26" if symbol == "NQ" else None,
                           previous_close=settle, timestamp=NOW - timedelta(minutes=10))


class StubService:
    def __init__(self, repo, instruments, now=NOW):
        self.repo = repo
        self.instruments = instruments
        self.now = now

    def current_state(self, now=None):
        return SimpleNamespace(generated_at=self.now, instruments=self.instruments)

    def history(self, symbol, *, start=None, end=None, limit=500):
        return self.repo.history(symbol, start=start, end=end, limit=limit)

    def ohlc(self, symbol, *, start, end, bucket_seconds, within=()):
        return self.repo.ohlc(symbol, start=start, end=end, bucket_seconds=bucket_seconds,
                              within=within)


@pytest.fixture
def client(memory_repo):
    start = datetime(2026, 10, 22, 13, 30, tzinfo=UTC)
    memory_repo.store([obs("NQ", start, 29_180.0), obs("NQ", start + timedelta(minutes=12),
                                                       29_200.0)])
    app = FastAPI()
    app.include_router(chart.router)
    app.state.service = StubService(memory_repo, {
        "NQ": inst("NQ", 29_200.0, pct=0.3, settle=29_150.0),
        "US2Y": inst("US2Y", 4.6, change=0.04, feed="live"),
        "DXY": inst("DXY", 101.6, feed="live"),
        "VIX": inst("VIX", 18.0, pct=-1.0),
    })
    app.state.desk = None
    with TestClient(app) as test_client:
        yield test_client


def test_bars_contract(client):
    body = client.get("/api/v1/bars/nq?interval=5m&range=1d").json()
    assert body["symbol"] == "NQ" and body["interval"] == "5m" and body["range"] == "1d"
    assert body["start"] == "2026-10-21T22:00:00Z"
    assert [(b["time"], b["count"]) for b in body["bars"]] == [
        ("2026-10-22T13:30:00Z", 1), ("2026-10-22T13:35:00Z", 0), ("2026-10-22T13:40:00Z", 1)]
    gap = body["bars"][1]
    assert gap["open"] is None and gap["close"] is None
    assert body["delayed"] is True and body["feed_status"] == "delayed"
    assert body["sources"] == ["cnbc"] and body["contracts"] == ["NQZ26"]
    assert body["last_at"] == "2026-10-22T13:42:00Z"
    [session] = body["sessions"]
    assert [s["name"] for s in session["segments"]] == [
        "asia", "europe", "premarket", "cash", "after_hours"]
    assert body["note"] is None


def test_month_bars_reject_five_minutes_and_mention_thinning(client, monkeypatch):
    rejected = client.get("/api/v1/bars/NQ?interval=5m&range=1m")
    assert rejected.status_code == 422 and "15m, 1h" in rejected.json()["detail"]
    # Thirty days of sessions start just inside the 30-day full-detail retention.
    body = client.get("/api/v1/bars/NQ?interval=15m&range=1m").json()
    assert body["note"] is None and body["fine_since"] == "2026-09-22T15:00:00Z"
    monkeypatch.setattr(chart, "FINE_RETENTION", timedelta(days=10))
    body = client.get("/api/v1/bars/NQ?interval=15m&range=1m").json()
    assert body["note"].startswith("Bars before Oct 12 are built from one stored quote a minute")


def test_bars_validate_symbol_and_parameters(client):
    assert client.get("/api/v1/bars/NOPE").status_code == 404
    assert client.get("/api/v1/bars/NQ?interval=2m").status_code == 422
    assert client.get("/api/v1/bars/NQ?range=1y").status_code == 422


def test_session_contract(client):
    body = client.get("/api/v1/session").json()
    assert body["phase"] == "rth"
    assert body["text"].startswith("US market open")
    assert body["session"]["trade_date"] == "2026-10-22"
    assert body["maintenance_starts_at"] == "2026-10-22T21:00:00Z"
    assert body["maintenance_ends_at"] == "2026-10-22T22:00:00Z"
    assert body["next_open_at"] is None
    from core.desk.explainers import segment
    for seg in body["session"]["segments"]:
        assert seg["explainer"] == segment(seg["name"]).text
    assert {"date": "2026-11-26", "name": "Thanksgiving Day", "early_close": False} not in \
        body["holidays"]  # more than 21 days ahead


def test_levels_contract(client):
    body = client.get("/api/v1/levels/NQ").json()
    levels = {level["key"]: level for level in body["levels"]}
    assert levels["prior_settle"]["value"] == 29_150.0
    assert levels["gap_vs_settle"]["value"] == 30.0
    assert levels["prior_day_high"]["value"] is None
    assert levels["prior_day_high"]["unavailable_reason"]
    assert all(level["explainer"] for level in body["levels"])
    assert client.get("/api/v1/levels/VIX").status_code == 404


def utc(*parts):
    return datetime(*parts, tzinfo=UTC)


def es_quote(ts, value, settle):
    return MarketObservation(symbol="ES", timestamp=ts, value=value, unit="index points",
                             source="cnbc", received_at=ts + timedelta(minutes=10),
                             metadata={"feed_status": "delayed", "contract_code": "ESZ26",
                                       "previous_close": settle})


@pytest.fixture
def es_client(memory_repo):
    """ES on Mon Nov 30 2026, 10:00 ET, after the Thanksgiving half day."""
    memory_repo.store([
        # Wed Nov 25 session (settle of Tue: 6,760).
        es_quote(utc(2026, 11, 25, 16, 0), 6_820.0, 6_760.0),
        # Fri Nov 27: overnight from Thu 18:00 ET, then the short cash session.
        es_quote(utc(2026, 11, 27, 9, 0), 6_830.0, 6_800.0),
        es_quote(utc(2026, 11, 27, 14, 30), 6_812.0, 6_800.0),
        es_quote(utc(2026, 11, 27, 17, 55), 6_840.0, 6_800.0),
        # After the 13:00 ET cash close but before the 13:15 ET Globex close.
        es_quote(utc(2026, 11, 27, 18, 10), 6_844.0, 6_800.0),
        # Mon Nov 30 (Friday settled 6,842).
        es_quote(utc(2026, 11, 30, 14, 30), 6_850.0, 6_842.0),
    ])
    app = FastAPI()
    app.include_router(chart.router)
    app.state.service = StubService(memory_repo, {"ES": SimpleNamespace(
        value=6_850.0, previous_close=6_842.0, contract_code="ESZ26", feed_status="delayed")},
        now=utc(2026, 11, 30, 15, 0))
    app.state.desk = None
    with TestClient(app) as test_client:
        yield test_client


def test_es_levels_for_a_past_half_day_use_that_days_quotes(es_client):
    body = es_client.get("/api/v1/levels/es?date=2026-11-27").json()
    assert body["symbol"] == "ES" and body["trade_date"] == "2026-11-27"
    # As they stood at the 13:00 ET early cash close.
    assert body["generated_at"] == "2026-11-27T18:00:00Z"
    assert body["contract"] == "ESZ26" and body["delayed"] is True
    levels = {level["key"]: level for level in body["levels"]}
    # The settle is Friday's own (Wednesday's settle), not Monday's live one.
    assert levels["prior_settle"]["value"] == 6_800.0
    assert levels["gap_vs_settle"]["value"] == 12.0
    assert levels["overnight_high"]["value"] == 6_830.0
    assert levels["overnight_high"]["window_start"] == "2026-11-26T23:00:00Z"
    assert levels["prior_day_high"]["value"] == 6_820.0
    assert levels["prior_day_high"]["window_end"] == "2026-11-25T22:00:00Z"


def test_es_levels_now_use_the_short_friday_as_prior_day(es_client):
    body = es_client.get("/api/v1/levels/ES").json()
    assert body["trade_date"] == "2026-11-30"
    levels = {level["key"]: level for level in body["levels"]}
    assert levels["prior_settle"]["value"] == 6_842.0
    assert (levels["prior_day_high"]["value"], levels["prior_day_low"]["value"]) == (6_844.0,
                                                                                   6_812.0)
    assert levels["prior_day_high"]["window_end"] == "2026-11-27T18:15:00Z"
    # The current session's own date gives the same live answer.
    assert es_client.get("/api/v1/levels/ES?date=2026-11-30").json() == body


def test_levels_date_must_be_a_past_or_current_trading_day(es_client):
    holiday = es_client.get("/api/v1/levels/ES?date=2026-11-26")
    assert holiday.status_code == 422 and "not a trading day" in holiday.json()["detail"]
    assert es_client.get("/api/v1/levels/ES?date=2026-12-01").status_code == 422
    assert es_client.get("/api/v1/levels/ES?date=nope").status_code == 422


def test_risk_tone_contract(client):
    body = client.get("/api/v1/risk-tone").json()
    assert body["tone"] == "mixed"  # NQ +0.3% helps, 2-year +4bp hurts
    assert body["label"] == "mixed"
    assert body["reasons"][0].startswith(("NQ +0.30%", "2-year yield +4.0bp"))
    assert [i["symbol"] for i in body["inputs"]] == ["NQ", "US2Y", "DXY", "VIX"]


def test_pins_need_the_desk(client):
    assert client.get("/api/v1/chart/pins").status_code == 503


def test_pins_contract(client):
    calls = []

    def chart_pins(start, end):
        calls.append((start, end))
        return []

    client.app.state.desk = SimpleNamespace(chart_pins=chart_pins)
    body = client.get("/api/v1/chart/pins?range=5d").json()
    assert body["range"] == "5d" and body["pins"] == []
    assert calls[0][0].isoformat() == "2026-10-15T22:00:00+00:00"


def test_sparklines_contract(client):
    body = client.get("/api/v1/sparklines").json()
    lines = {line["symbol"]: line["points"] for line in body["lines"]}
    assert body["interval"] == "15m"
    assert [p["value"] for p in lines["NQ"]] == [29_200.0]
    assert lines["DXY"] == []


def test_btc_bars_use_24x7_utc_day_sessions_and_never_say_closed(memory_repo):
    """Phase 0 gate: the BTC chart shows 24x7 sessions and never "market closed"."""
    from core.services.market_state import StalePolicy, build_market_state

    sunday = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)  # CME shut until 18:00 ET
    quotes = []
    at = datetime(2026, 10, 20, tzinfo=UTC)
    while at < sunday - timedelta(hours=2):  # the feed stopped two hours ago
        quotes.append(MarketObservation(symbol="BTC", timestamp=at, value=80_000.0,
                                        unit="USD", source="binance", received_at=at,
                                        metadata={"feed_status": "live"}))
        at += timedelta(minutes=30)
    memory_repo.store(quotes)

    class Service(StubService):
        def current_state(self, now=None):
            return build_market_state({"BTC": quotes[-1]}, policy=StalePolicy(), now=sunday,
                                      provider="binance")

    app = FastAPI()
    app.include_router(chart.router)
    app.state.service = Service(memory_repo, {})
    with TestClient(app) as test_client:
        body = test_client.get("/api/v1/bars/BTC?interval=1h&range=5d").json()
        day = test_client.get("/api/v1/bars/BTC?interval=1h&range=1d").json()
        nq = test_client.get("/api/v1/bars/NQ?interval=1h&range=1d").json()

    assert [s["trade_date"] for s in body["sessions"]] == [
        "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-24", "2026-10-25"]
    for session in body["sessions"]:
        assert session["opens_at"].endswith("T00:00:00Z")
        assert session["rth_opens_at"] is None and session["segments"] == []
        assert session["early_close"] is False
    assert body["start"] == "2026-10-21T00:00:00Z" and body["end"] == "2026-10-25T12:00:00Z"
    # Saturday has bars; the weekend is not cut out as it is for NQ.
    assert any(b["time"].startswith("2026-10-24") and b["count"] for b in body["bars"])
    assert body["status"] == "stale" and body["market_closed"] is False
    assert [s["trade_date"] for s in day["sessions"]] == ["2026-10-25"]
    # NQ on the same Sunday still shows Friday's Globex session.
    assert [s["trade_date"] for s in nq["sessions"]] == ["2026-10-23"]


def test_btc_quiet_spell_across_utc_midnight_stays_a_gap(memory_repo):
    """A 24x7 market trades through midnight: a missing hour there is a gap bar."""
    from core.analysis import bars as B
    from core.domain.calendars import ALWAYS_OPEN
    from core.domain.chart import ChartRange, Interval

    quotes = [MarketObservation(symbol="BTC", timestamp=at, value=80_000.0, unit="USD",
                                source="binance", received_at=at,
                                metadata={"feed_status": "live"})
              for at in (datetime(2026, 10, 22, 23, 0, tzinfo=UTC),
                         datetime(2026, 10, 23, 1, 0, tzinfo=UTC))]
    memory_repo.store(quotes)
    now = datetime(2026, 10, 23, 2, 0, tzinfo=UTC)
    sessions = B.range_sessions(ChartRange.FIVE_DAYS, now, ALWAYS_OPEN)
    _, end = B.window(sessions, now)
    built, _ = B.read_bars(memory_repo.ohlc, "BTC", sessions, Interval.H1, end=end)
    assert [(b.time.isoformat(), b.count) for b in built] == [
        ("2026-10-22T23:00:00+00:00", 1), ("2026-10-23T00:00:00+00:00", 0),
        ("2026-10-23T01:00:00+00:00", 1)]
