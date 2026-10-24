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
    def __init__(self, repo, instruments):
        self.repo = repo
        self.instruments = instruments

    def current_state(self, now=None):
        return SimpleNamespace(generated_at=NOW, instruments=self.instruments)

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
