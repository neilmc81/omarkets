"""Tests for the Phase 9 Omarkets MCP voice adapter.

The adapter is the boundary between a voice client (Qwen) and the Omarkets HTTP
API. These tests cover the pure projections, the HTTP client's failure modes, the
JSON-RPC surface, and the read-only safety contract — all without a running
backend.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from core.interfaces.mcp import tools
from core.interfaces.mcp.client import (
    ApiResponse,
    MarketOsClient,
    MarketOsProtocolError,
    MarketOsUnavailable,
    validate_base_url,
)
from core.interfaces.mcp.server import (
    SERVER_INFO,
    TOOL_NAMES,
    TOOLS,
    McpServer,
)

# ---------------------------------------------------------------------------
# Fixtures: representative Omarkets payloads (shape copied from the real API).
# ---------------------------------------------------------------------------

QUOTES = [
    {
        "symbol": "NQ",
        "value": 30521.5,
        "change_pct": 2.02,
        "feed_status": "delayed",
        "status": "live",
    },
    {
        "symbol": "ES",
        "value": 5500.0,
        "change_pct": 1.1,
        "feed_status": "delayed",
        "status": "live",
    },
    {"symbol": "US2Y", "value": 3.9, "change_bp": -1.2, "feed_status": "live", "status": "live"},
    {"symbol": "US10Y", "value": 4.1, "change_bp": -3.7, "feed_status": "live", "status": "live"},
    {"symbol": "DXY", "value": 100.38, "change_pct": 0.16, "feed_status": "live", "status": "live"},
    {"symbol": "VIX", "value": 14.6, "change_pct": -2.0, "feed_status": "live", "status": "live"},
]

STATUS = {
    "version": 2,
    "generated_at": "2026-09-21T14:51:43Z",
    "primary": QUOTES[0],
    "strip": QUOTES[:3],
    "session": {"phase": "overnight", "maintenance_break": False},
    "next_event": {
        "id": "consensus:abc",
        "name": "Unemployment Claims",
        "event_code": "CLAIMS",
        "importance": "high",
        "scheduled_at": "2026-09-24T12:30:00Z",
        "time_et": "08:30 EDT",
        "consensus": 220000,
        "previous": 218000,
    },
    "data_health": "degraded",
    "ai_health": "ok",
    "providers": [],
}

CONTEXT = {
    "version": 2,
    "generated_at": "2026-09-21T14:51:43Z",
    "cross_market": QUOTES,
    "upcoming_events": [],
    "diagnostics": [],
}

CROSS_MARKET = {
    "generated_at": "2026-09-21T14:51:43Z",
    "nq_change_pct": {"value": 2.02, "availability": "available"},
    "es_change_pct": {"value": 1.1, "availability": "available"},
    "nq_vs_es": {"value": 0.9, "availability": "available"},
    "nq_vs_es_label": "outperforming",
    "tolerance_pct": 0.1,
    "nq_feed_status": "delayed",
    "es_feed_status": "delayed",
    "us2y": {},
    "us10y": {},
    "dxy": {},
    "vix": {},
    "vxn": {},
}

# The real ``/api/v1/technical/NQ`` returns a ``TechnicalState``: ``vwap`` is a
# nested ``VwapState``, structure/range live in ``structure``/``volatility``, and
# the break is under ``timeframes``. The adapter must read this shape, not only
# the flat ``ShellTechnical`` served by ``/shell/status``.
CALENDAR_EVENT = {
    "id": "e1",
    "name": "CPI",
    "event_code": "CPI",
    "country": "US",
    "category": "inflation",
    "importance": "critical",
    "status": "scheduled",
    "scheduled_at": "2026-09-21T12:30:00Z",
    "timezone": "America/New_York",
    "unit": "percent",
    "frequency": "monthly",
    "consensus": 0.3,
    "previous": 0.2,
    "provider": "consensus",
    "source_event_id": "CPI:1",
    "received_at": "2026-09-20T00:00:00Z",
    "mode": "live",
}

CALENDAR_TODAY = {
    "generated_at": "2026-09-21T14:00:00Z",
    "mode": "live",
    "events": [
        {
            "id": "e1",
            "name": "CPI",
            "event_code": "CPI",
            "importance": "critical",
            "scheduled_at": "2026-09-21T12:30:00Z",
            "time_et": "08:30 EDT",
            "consensus": 0.3,
            "previous": 0.2,
        },
        {
            "id": "e2",
            "name": "Consumer Confidence",
            "event_code": "CONF",
            "importance": "low",
            "scheduled_at": "2026-09-21T14:00:00Z",
            "time_et": "10:00 EDT",
        },
    ],
    "providers": [],
    "stale": False,
    "coverage": [],
}

MACRO = {
    "generated_at": "2026-09-21T14:00:00Z",
    "mode": "live",
    "series": [
        {
            "definition": {"id": "CPI_MOM", "name": "CPI month over month", "unit": "percent"},
            "latest": {"value": 0.4, "period": "2026-08"},
            "status": "live",
        },
        {
            "definition": {"id": "CPI_YOY", "name": "CPI year over year", "unit": "percent"},
            "latest": {"value": 3.4, "period": "2026-08"},
            "status": "live",
        },
    ],
    "providers": [],
    "upcoming_events": [],
}

EVENTS = {
    "generated_at": "2026-09-21T14:00:00Z",
    "count": 2,
    "events": [
        {
            "id": "ev:1",
            "headline": "Semiconductor export restrictions tighten",
            "event_type": "geopolitical",
            "published_at": "2026-09-21T10:00:00Z",
            "instruments": ["NVDA"],
            "countries": ["US", "CN"],
        },
        {
            "id": "ev:2",
            "headline": "Bond yields jump after auction",
            "event_type": "market",
            "published_at": "2026-09-21T09:00:00Z",
            "instruments": [],
            "countries": ["US"],
        },
    ],
}

DESK = {
    "generated_at": "2026-09-21T14:00:00Z",
    "notes": [
        {
            "id": "ask:20260921T1350",
            "kind": "ask",
            "title": "Market now",
            "created_at": "2026-09-21T13:50:00Z",
            "status": "ok",
            "model": "fixture",
            "draft": None,
        },
        {
            "id": "release:20260921T1230:CPI",
            "kind": "release",
            "title": "CPI",
            "created_at": "2026-09-21T12:56:00Z",
            "status": "ok",
            "model": "fixture",
            "draft": {
                "headline": "Hotter CPI pushes yields up",
                "summary": "Inflation beat forecasts and NQ fell 0.4%.",
                "nq_tone": "negative",
            },
        },
        {
            "id": "premarket:2026-09-21",
            "kind": "premarket",
            "title": "Pre-market",
            "created_at": "2026-09-21T13:00:00Z",
            "status": "ok",
            "model": "fixture",
            "draft": {
                "headline": "Quiet open ahead of the Fed",
                "summary": "Futures are flat with yields steady.",
                "nq_tone": "neutral",
            },
        },
    ],
}


class FakeClient:
    """A MarketOsClient test double with per-path payloads."""

    base_url = "http://127.0.0.1:8765"

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if path in self.routes:
            value = self.routes[path]
            return value(params) if callable(value) else value
        return None

    def post_json(self, path: str, body: dict[str, Any]) -> Any:
        return self.get_json(path, body)


# ---------------------------------------------------------------------------
# Pure projections
# ---------------------------------------------------------------------------


def test_status_projects_quotes_and_flags_delay():
    view = tools.build_status(status=STATUS, context=CONTEXT)
    assert view["primary"]["symbol"] == "NQ"
    assert "2.02 percent" in view["spoken"]
    assert "NQ" in view["spoken"]
    # NQ and ES are delayed, so the caveat must be spoken.
    assert view["stale"] is True
    assert "stale" in view["spoken"] or "delayed" in view["spoken"]
    # Values are copied verbatim, not recalculated.
    assert view["quotes"][0]["value"] == 30521.5


def test_status_without_next_event_has_no_dangling_sentence():
    status = dict(STATUS, next_event=None)
    view = tools.build_status(status=status, context=CONTEXT)
    assert "Next on the calendar" not in view["spoken"]
    assert not view["spoken"].endswith("  ")


def test_status_unavailable_raises_voice_error():
    with pytest.raises(tools.VoiceToolError):
        tools.build_status(status=None, context=None)


def test_spoken_numbers_are_speech_friendly():
    assert tools.spoken_change_bp(8.4) == "up 8.4 basis points"
    assert tools.spoken_change_bp(-8.4) == "down 8.4 basis points"
    assert tools.spoken_change_bp(0) == "flat"
    assert tools.spoken_change_pct(-0.72) == "down 0.72 percent"
    assert tools.spoken_change_pct(0.72) == "up 0.72 percent"
    # Nulls never become a number.
    assert tools.spoken_change_bp(None) is None
    assert tools.spoken_change_pct(None) is None


def test_next_event_prefers_named_match_and_never_invents_consensus():
    view = tools.build_next_event(CALENDAR_TODAY["events"])
    assert view["event"]["name"] == "CPI"
    assert "consensus 0.3" in view["spoken"]

    missing = tools.build_next_event(
        [{"id": "x", "name": "No Consensus", "scheduled_at": "2026-09-22T00:00:00Z"}]
    )
    assert "consensus" not in missing["spoken"]


def test_next_event_renders_time_from_real_economic_event():
    """``EconomicEvent`` has no ``time_et``; the time must still be spoken.

    12:30Z in America/New_York is 08:30 EDT.
    """
    view = tools.build_next_event([CALENDAR_EVENT])
    assert "CPI" in view["spoken"]
    assert "08:30" in view["spoken"]
    assert "EDT" in view["spoken"]


def test_calendar_renders_time_from_real_economic_event():
    view = tools.build_calendar([CALENDAR_EVENT])
    assert "08:30" in view["spoken"]


def test_event_time_honours_the_event_timezone():
    event = dict(CALENDAR_EVENT, timezone="UTC")
    view = tools.build_next_event([event])
    assert "12:30" in view["spoken"]


def test_calendar_filters_to_high_and_critical():
    view = tools.build_calendar(CALENDAR_TODAY["events"])
    assert len(view["events"]) == 1
    assert view["events"][0]["name"] == "CPI"

    all_view = tools.build_calendar(CALENDAR_TODAY["events"], include_all=True)
    assert len(all_view["events"]) == 2


def test_macro_filters_by_series_substring():
    view = tools.build_macro(MACRO, series="YOY")
    assert len(view["series"]) == 1
    assert "3.40" in view["spoken"]


def test_latest_events_returns_ranked_headlines():
    view = tools.build_latest_events(EVENTS["events"], materiality={"ev:1": "high"})
    assert view["events"][0]["event_id"] == "ev:1"
    assert view["events"][0]["materiality"] == "high"
    assert "Semiconductor" in view["spoken"]


def test_latest_events_collapses_repeated_families_and_drops_future():
    family = [
        {
            "id": f"ev:{month}",
            "headline": f"Personal Income and Outlays, {month} 2026",
            "published_at": f"2026-{month:02d}-01T12:00:00Z",
        }
        for month in (8, 9, 10, 11)
    ]
    real = {
        "id": "ev:real",
        "headline": "Bond yields jump after auction",
        "published_at": "2026-09-21T09:00:00Z",
    }
    view = tools.build_latest_events(
        [*family, real],
        materiality={"ev:real": "high"},
        now="2026-09-21T15:00:00Z",
    )
    ids = [e["event_id"] for e in view["events"]]
    # The repeated monthly family collapses to one entry, and the future-dated
    # scheduled releases are not read out as if they had happened.
    assert ids.count("ev:real") == 1
    assert len(ids) <= 2
    assert "Personal Income" in view["spoken"] or "Bond yields" in view["spoken"]


def test_latest_events_are_newest_first_within_an_importance_level():
    """``market_latest_events`` must not read out stale news as current.

    The API returns events newest-first; the projection previously re-sorted
    ascending by ``published_at``, so the oldest event was spoken first and the
    oldest instance of a repeated release was the one kept.
    """
    now = "2026-09-21T15:10:43.119771+00:00"
    older = {"id": "older", "headline": "Older past event", "published_at": "2026-09-21T09:00:00Z"}
    newer = {"id": "newer", "headline": "Newer past event", "published_at": "2026-09-21T14:00:00Z"}
    view = tools.build_latest_events(
        [older, newer],
        materiality={"older": "high", "newer": "high"},
        now=now,
    )
    assert [e["event_id"] for e in view["events"]] == ["newer", "older"]
    assert view["spoken"].index("Newer") < view["spoken"].index("Older")


def test_latest_events_importance_still_outranks_recency():
    """A CRITICAL event is spoken before a newer HIGH one."""
    now = "2026-09-21T15:10:43.119771+00:00"
    critical_old = {
        "id": "crit",
        "headline": "Critical old event",
        "published_at": "2026-09-01T09:00:00Z",
    }
    high_new = {"id": "high", "headline": "High new event", "published_at": "2026-09-21T14:00:00Z"}
    view = tools.build_latest_events(
        [critical_old, high_new],
        materiality={"crit": "critical", "high": "high"},
        now=now,
    )
    assert [e["event_id"] for e in view["events"]] == ["crit", "high"]


def test_latest_events_family_collapse_keeps_the_newest_instance():
    """Four monthly instances of one release collapse to the most recent."""
    now = "2026-09-21T15:10:43.119771+00:00"
    family = [
        {
            "id": f"ev:{month}",
            "headline": f"Personal Income and Outlays, month {month}",
            "published_at": f"2026-{month:02d}-01T12:00:00Z",
        }
        for month in (6, 7, 8, 9)
    ]
    view = tools.build_latest_events(family, now=now)
    assert [e["event_id"] for e in view["events"]] == ["ev:9"]


def test_latest_events_keeps_future_when_nothing_else_exists():
    future = {
        "id": "ev:f",
        "headline": "CPI release",
        "published_at": "2026-12-01T12:00:00Z",
    }
    view = tools.build_latest_events([future], now="2026-09-21T15:00:00Z")
    assert len(view["events"]) == 1
    # But it must be labelled upcoming so it is not spoken as if it happened.
    assert view["events"][0]["upcoming"] is True
    assert "coming up" in view["spoken"]


def test_latest_events_past_items_are_not_upcoming():
    past = {"id": "ev:p", "headline": "Yields jump", "published_at": "2026-09-21T09:00:00Z"}
    view = tools.build_latest_events([past], now="2026-09-21T15:00:00Z")
    assert view["events"][0]["upcoming"] is False
    assert "coming up" not in view["spoken"]


def test_latest_events_same_second_is_not_treated_as_future():
    """``now`` uses ``+00:00`` while API timestamps use ``Z``.

    A raw string comparison would rank a same-second event as future and drop it
    in favour of an older one; the comparison must be timezone-aware.
    """
    now = "2026-09-21T15:10:43.119771+00:00"
    same_second = {
        "id": "ev:same",
        "headline": "Same second event",
        "published_at": "2026-09-21T15:10:43Z",
    }
    older = {
        "id": "ev:older",
        "headline": "Older event",
        "published_at": "2026-09-21T15:10:42Z",
    }
    view = tools.build_latest_events([same_second, older], now=now)
    ids = [e["event_id"] for e in view["events"]]
    assert "ev:same" in ids
    assert "ev:older" in ids


def test_latest_events_future_is_dropped_when_past_exists():
    now = "2026-09-21T15:10:43.119771+00:00"
    past = {"id": "ev:p", "headline": "Past event", "published_at": "2026-09-21T15:10:42Z"}
    future = {"id": "ev:f", "headline": "Future event", "published_at": "2026-09-21T15:10:44Z"}
    view = tools.build_latest_events([past, future], now=now)
    ids = [e["event_id"] for e in view["events"]]
    assert ids == ["ev:p"]


def test_latest_events_missing_timestamp_is_not_hidden():
    """An event with no publication time must not be treated as future."""
    now = "2026-09-21T15:10:43.119771+00:00"
    no_ts = {"id": "ev:none", "headline": "No timestamp event"}
    past = {"id": "ev:p", "headline": "Past event", "published_at": "2026-09-21T15:10:42Z"}
    view = tools.build_latest_events([no_ts, past], now=now)
    ids = [e["event_id"] for e in view["events"]]
    assert "ev:none" in ids


def test_latest_analysis_is_the_newest_written_note():
    view = tools.build_latest_analysis(DESK["notes"])
    assert view["analysis"]["note_id"] == "release:20260921T1230:CPI"
    assert "Hotter CPI pushes yields up" in view["spoken"]
    assert "economic-release note" in view["spoken"]


def test_latest_analysis_kind_filter_and_empty():
    view = tools.build_latest_analysis(DESK["notes"], kind="premarket")
    assert view["analysis"]["kind"] == "premarket"
    assert tools.build_latest_analysis([])["analysis"] is None
    failed = [{**DESK["notes"][1], "status": "failed"}]
    assert tools.build_latest_analysis(failed)["analysis"] is None


def test_event_note_matches_descriptor_conservatively():
    found = tools.build_event_note(DESK["notes"], "that CPI release")
    assert found["event_id"] == "release:20260921T1230:CPI"
    missing = tools.build_event_note(DESK["notes"], "semiconductor export rules")
    assert missing["event_id"] is None and "couldn't match" in missing["spoken"]


def test_match_recent_event_is_conservative():
    match = tools.match_recent_event(EVENTS["events"], "semiconductor export story")
    assert match == {
        "event_id": "ev:1",
        "headline": "Semiconductor export restrictions tighten",
        "score": 2,
    }
    # Nothing meaningful shared -> no match, never an invented event.
    assert tools.match_recent_event(EVENTS["events"], "a totally unrelated topic") is None
    # A single shared-token match is only safe when it is unambiguous.
    assert tools.match_recent_event(EVENTS["events"], "semiconductor")["event_id"] == "ev:1"


def test_match_recent_event_rejects_ambiguous_single_token():
    events = [
        {"id": "a", "headline": "Fed signals patience on rates"},
        {"id": "b", "headline": "Fed officials split on cuts"},
    ]
    # "rates" appears in only one, so it is fine; "fed" appears in both, so the
    # resolver must refuse rather than guess.
    assert tools.match_recent_event(events, "rates")["event_id"] == "a"
    assert tools.match_recent_event(events, "fed") is None


def test_open_omarkets_rejects_unknown_destination():
    assert tools.build_open_omarkets("macro")["destination"] == "macro"
    with pytest.raises(tools.VoiceToolError):
        tools.build_open_omarkets("technical")
    with pytest.raises(tools.VoiceToolError):
        tools.build_open_omarkets("admin")


def test_usage_reports_weekly_spend_against_limit():
    view = tools.build_usage(
        {"week_spent_usd": 0.42, "weekly_limit_usd": 10, "week_remaining_usd": 9.5,
         "paused": False}
    )
    assert "$0.42" in view["spoken"] and "$10.00" in view["spoken"]
    assert "voice" in view["spoken"]
    with pytest.raises(tools.VoiceToolError):
        tools.build_usage(None)


def test_base_url_must_be_loopback():
    assert validate_base_url("http://127.0.0.1:8765") == "http://127.0.0.1:8765"
    assert validate_base_url("http://localhost:9000") == "http://localhost:9000"
    with pytest.raises(ValueError):
        validate_base_url("http://example.com:8765")
    with pytest.raises(ValueError):
        validate_base_url("file:///etc/passwd")
    with pytest.raises(ValueError):
        validate_base_url("http://user:pass@127.0.0.1:8765")


def test_client_offline_raises_unavailable(monkeypatch):
    def boom(*args, **kwargs):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    client = MarketOsClient("http://127.0.0.1:8765")
    with pytest.raises(MarketOsUnavailable):
        client.get_json("/api/v1/shell/status")


def test_client_timeout_raises_unavailable(monkeypatch):
    def timeout(*args, **kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr("urllib.request.urlopen", timeout)
    client = MarketOsClient("http://127.0.0.1:8765")
    with pytest.raises(MarketOsUnavailable):
        client.get_json("/api/v1/shell/status")


def test_client_malformed_json_returns_none(monkeypatch):
    class FakeResponse:
        status = 200

        def read(self):
            return b"not json"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: FakeResponse())
    client = MarketOsClient("http://127.0.0.1:8765")
    assert client.get_json("/api/v1/shell/status") is None


def test_client_404_returns_none(monkeypatch):
    def not_found(*args, **kwargs):
        raise urllib.error.HTTPError("url", 404, "not found", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", not_found)
    client = MarketOsClient("http://127.0.0.1:8765")
    assert client.get_json("/api/v1/events/nope") is None


def test_client_500_raises_unavailable(monkeypatch):
    def server_error(*args, **kwargs):
        raise urllib.error.HTTPError("url", 503, "unavailable", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", server_error)
    client = MarketOsClient("http://127.0.0.1:8765")
    with pytest.raises(MarketOsUnavailable):
        client.get_json("/api/v1/shell/status")


def test_api_response_is_used_for_error_status():
    assert ApiResponse(status=404, data=None).status == 404


# ---------------------------------------------------------------------------
# JSON-RPC surface
# ---------------------------------------------------------------------------


def rpc(server: McpServer, method: str, params: dict | None = None, msg_id: int = 1) -> dict:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        message["params"] = params
    return json.loads(server.handle(json.dumps(message)))


def build_server(routes: dict[str, Any] | None = None, launched: list | None = None) -> McpServer:
    routes = routes or {}
    recorded = launched if launched is not None else []
    return McpServer(FakeClient(routes), launcher=lambda dest, url: recorded.append(dest))


def test_initialize_and_tools_list():
    server = build_server()
    init = rpc(server, "initialize", {"protocolVersion": "2025-06-18"})
    assert init["result"]["serverInfo"] == SERVER_INFO
    listed = rpc(server, "tools/list")
    names = {t["name"] for t in listed["result"]["tools"]}
    assert names == set(TOOL_NAMES) | {"open_marketos"}
    assert len(TOOLS) == len(TOOL_NAMES)
    for tool in TOOLS:
        assert tool["inputSchema"]["type"] == "object"


def test_unknown_tool_fails_closed():
    server = build_server()
    result = rpc(server, "tools/call", {"name": "run_shell", "arguments": {"cmd": "x"}})
    assert result["result"]["isError"] is True
    assert "not exposed" in result["result"]["content"][0]["text"]


def test_every_tool_dispatches_against_a_populated_backend():
    routes = {
        "/api/v1/shell/status": STATUS,
        "/api/v1/shell/context": CONTEXT,
        "/api/v1/calendar/upcoming": CALENDAR_TODAY,
        "/api/v1/calendar/today": CALENDAR_TODAY,
        "/api/v1/calendar/week": CALENDAR_TODAY,
        "/api/v1/macro/state": MACRO,
        "/api/v1/events/latest": EVENTS,
        "/api/v1/desk": DESK,
        "/api/v1/desk/notes/premarket:2026-09-21": DESK["notes"][2],
        "/api/v1/ai/budget": {"week_spent_usd": 0.1, "weekly_limit_usd": 10},
    }
    server = build_server(routes)
    for name in TOOL_NAMES:
        args: dict[str, Any] = {}
        if name == "market_calendar":
            args = {"scope": "today"}
        if name == "market_event_analysis":
            args = {"event_id": "premarket:2026-09-21"}
        if name == "market_latest_briefing":
            args = {"briefing_type": "premarket"}
        result = rpc(server, "tools/call", {"name": name, "arguments": args})
        assert result["result"]["isError"] is False, (name, result["result"])


def test_get_many_preserves_order_and_propagates_errors():
    import time

    class SlowClient(FakeClient):
        def get_json(self, path, params=None):
            # Both legs sleep, so a serial implementation would take ~0.10s and
            # fail the bound below; only a concurrent one fits in ~0.05s.
            time.sleep(0.05)
            return path

    server = McpServer(SlowClient({}), launcher=lambda d, u: None)
    start = time.perf_counter()
    values = server._get_many(("/a/slow", None), ("/b/slow", None))
    elapsed = time.perf_counter() - start
    assert values == ["/a/slow", "/b/slow"]
    assert elapsed < 0.09, f"expected concurrent fetch, took {elapsed:.3f}s"


def test_get_many_discards_partial_results_on_mixed_failure():
    """One successful leg plus one failed leg must fail the whole call.

    Returning the partial result would silently drop a section from a spoken
    answer, which is exactly the silent-partial case the helper exists to avoid.
    """

    class MixedClient(FakeClient):
        def get_json(self, path, params=None):
            if path == "/b/fail":
                raise MarketOsProtocolError("bad payload")
            return path

    server = McpServer(MixedClient({}), launcher=lambda d, u: None)
    with pytest.raises(MarketOsProtocolError):
        server._get_many(("/a/ok", None), ("/b/fail", None))


def test_get_many_lowest_index_error_wins_deterministically():
    """When several legs fail, the first request's error is the one surfaced."""

    class TwoFailsClient(FakeClient):
        def get_json(self, path, params=None):
            if path == "/a":
                raise MarketOsUnavailable("first")
            raise MarketOsProtocolError("second")

    server = McpServer(TwoFailsClient({}), launcher=lambda d, u: None)
    with pytest.raises(MarketOsUnavailable):
        server._get_many(("/a", None), ("/b", None))


def test_get_many_single_request_fast_path():
    server = build_server({"/only": {"ok": True}})
    assert server._get_many(("/only", None)) == [{"ok": True}]
    assert server._get_many() == []


def test_open_omarkets_calls_launcher_with_explicit_destination():
    launched: list[str] = []
    server = build_server({}, launched)
    result = rpc(
        server, "tools/call", {"name": "open_omarkets", "arguments": {"destination": "macro"}}
    )
    assert result["result"]["isError"] is False
    assert launched == ["macro"]


def test_old_open_marketos_tool_name_still_works():
    launched: list[str] = []
    server = build_server({}, launched)
    result = rpc(
        server, "tools/call", {"name": "open_marketos", "arguments": {"destination": "news"}}
    )
    assert result["result"]["isError"] is False
    assert launched == ["news"]


def test_tools_list_includes_deprecated_open_marketos_by_default(monkeypatch):
    monkeypatch.delenv("OMARKETS_MCP_LEGACY_TOOLS", raising=False)
    server = build_server({}, [])
    listed = {t["name"]: t for t in rpc(server, "tools/list", {})["result"]["tools"]}
    assert listed["open_marketos"]["inputSchema"] == listed["open_omarkets"]["inputSchema"]
    assert listed["open_marketos"]["description"].startswith("Deprecated alias of open_omarkets.")


def test_legacy_tools_flag_off_hides_alias_but_calls_still_work(monkeypatch):
    monkeypatch.setenv("OMARKETS_MCP_LEGACY_TOOLS", "0")
    launched: list[str] = []
    server = build_server({}, launched)
    names = [t["name"] for t in rpc(server, "tools/list", {})["result"]["tools"]]
    assert "open_marketos" not in names and "open_omarkets" in names
    result = rpc(server, "tools/call", {"name": "open_marketos", "arguments": {}})
    assert result["result"]["isError"] is False


def test_legacy_marketos_mcp_shim_execs_new_launcher():
    shim = Path(__file__).resolve().parents[2] / "omarchy" / "marketos-mcp"
    if not shim.exists():
        pytest.skip("compatibility shim is not shipped in releases")
    text = shim.read_text()
    assert "deprecated" in text.lower() and "omarkets-mcp" in text
    assert os.access(shim, os.X_OK)
    proc = subprocess.run(
        [str(shim)],
        input='{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n',
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert '"open_omarkets"' in proc.stdout, proc.stderr


def test_mcp_api_url_accepts_old_env_name(monkeypatch):
    monkeypatch.delenv("OMARKETS_API_URL", raising=False)
    monkeypatch.setenv("MARKETOS_API_URL", "http://127.0.0.1:9001")
    assert McpServer(launcher=lambda d, u: None).client.base_url == "http://127.0.0.1:9001"
    monkeypatch.setenv("OMARKETS_API_URL", "http://127.0.0.1:9002")
    assert McpServer(launcher=lambda d, u: None).client.base_url == "http://127.0.0.1:9002"


def test_backend_offline_yields_spoken_error_not_crash():
    class OfflineClient(FakeClient):
        def get_json(self, path, params=None):
            raise MarketOsUnavailable("Omarkets isn't responding right now.")

    server = McpServer(OfflineClient({}), launcher=lambda d, u: None)
    result = rpc(server, "tools/call", {"name": "market_status", "arguments": {}})
    assert result["result"]["isError"] is True
    assert "Omarkets isn't responding right now." in result["result"]["content"][0]["text"]


def test_protocol_error_is_reported():
    class BrokenClient(FakeClient):
        def get_json(self, path, params=None):
            raise MarketOsProtocolError("bad payload")

    server = McpServer(BrokenClient({}), launcher=lambda d, u: None)
    result = rpc(server, "tools/call", {"name": "market_macro", "arguments": {}})
    assert result["result"]["isError"] is True
    assert "bad payload" in result["result"]["content"][0]["text"]


def test_invalid_arguments_are_rejected():
    server = build_server({"/api/v1/calendar/today": CALENDAR_TODAY})
    bad_scope = rpc(
        server, "tools/call", {"name": "market_calendar", "arguments": {"scope": "forever"}}
    )
    assert bad_scope["result"]["isError"] is True

    bad_limit = rpc(
        server, "tools/call", {"name": "market_latest_events", "arguments": {"limit": 999}}
    )
    assert bad_limit["result"]["isError"] is True


def test_string_boolean_is_rejected():
    """A permissive ``bool("false")`` is True; the schema says boolean."""
    server = build_server({"/api/v1/calendar/today": CALENDAR_TODAY})
    bad = rpc(
        server,
        "tools/call",
        {"name": "market_calendar", "arguments": {"scope": "today", "include_all": "false"}},
    )
    assert bad["result"]["isError"] is True
    good = rpc(
        server,
        "tools/call",
        {"name": "market_calendar", "arguments": {"scope": "today", "include_all": True}},
    )
    assert good["result"]["isError"] is False


def test_unknown_method_is_jsonrpc_error():
    server = build_server()
    result = rpc(server, "does/not/exist")
    assert result["error"]["code"] == -32601


def test_parse_error_is_jsonrpc_error():
    server = build_server()
    result = json.loads(server.handle("{not json"))
    assert result["error"]["code"] == -32700


def test_no_mutating_tools_are_exposed():
    """Phase 9 is read-only: no admin, config, model-policy or delete tools."""
    forbidden = {
        "run_shell",
        "delete",
        "prune",
        "analyze",
        "configure",
        "set_threshold",
        "update_config",
    }
    for name in TOOL_NAMES:
        assert not any(word in name for word in forbidden), name
    assert not any(tool["name"].startswith("omarkets_admin") for tool in TOOLS)


@pytest.mark.parametrize("bad_params", [[1, 2], "hello", 5, True])
def test_non_object_params_do_not_crash_the_server(bad_params):
    """A malformed ``params`` must fail closed, not kill the stdio process.

    The MCP client would lose the whole connection if ``handle`` raised, so a
    non-object ``params`` is treated as empty and answered normally.
    """
    server = build_server({"/api/v1/shell/status": STATUS, "/api/v1/shell/context": CONTEXT})
    for method in ("tools/call", "initialize"):
        message = {"jsonrpc": "2.0", "id": 1, "method": method, "params": bad_params}
        response = server.handle(json.dumps(message))
        assert response is not None
        parsed = json.loads(response)
        assert parsed["id"] == 1
        assert "result" in parsed or "error" in parsed


def test_non_object_params_keeps_the_connection_alive():
    """After a malformed request the next valid request still gets an answer."""
    server = build_server({"/api/v1/shell/status": STATUS, "/api/v1/shell/context": CONTEXT})
    server.handle(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": [1]}))
    follow_up = rpc(server, "ping", msg_id=2)
    assert follow_up["result"] == {}


def test_event_id_is_url_encoded_as_a_single_path_segment():
    """A note id containing '/', '?' or '#' must not escape the notes route."""
    requested: list[str] = []

    class RecordingClient(FakeClient):
        def get_json(self, path, params=None):
            requested.append(path)
            return DESK["notes"][1]

    server = McpServer(RecordingClient({}), launcher=lambda d, u: None)
    rpc(
        server,
        "tools/call",
        {"name": "market_event_analysis", "arguments": {"event_id": "ev:1/../../health"}},
    )
    assert requested == ["/api/v1/desk/notes/ev%3A1%2F..%2F..%2Fhealth"]

    requested.clear()
    rpc(
        server,
        "tools/call",
        {"name": "market_event_analysis", "arguments": {"event_id": "ev:1?limit=999"}},
    )
    assert requested == ["/api/v1/desk/notes/ev%3A1%3Flimit%3D999"]


def test_event_id_with_control_character_is_encoded_not_a_crash():
    """A newline in an id must be encoded, not passed raw to urllib."""
    requested: list[str] = []

    class RecordingClient(FakeClient):
        def get_json(self, path, params=None):
            requested.append(path)
            return DESK["notes"][1]

    server = McpServer(RecordingClient({}), launcher=lambda d, u: None)
    result = rpc(
        server,
        "tools/call",
        {"name": "market_event_analysis", "arguments": {"event_id": "ev:1\nX"}},
    )
    assert result["result"]["isError"] is False
    assert requested == ["/api/v1/desk/notes/ev%3A1%0AX"]
    # The connection survives: a subsequent request is answered.
    assert rpc(server, "ping", msg_id=2)["result"] == {}


# ---------------------------------------------------------------------------
# Briefings are stored pre-market / week-ahead / review notes
# ---------------------------------------------------------------------------


def test_latest_briefing_retrieves_a_stored_note_without_writing_one():
    view = tools.build_latest_briefing(DESK["notes"])
    assert view["briefing"]["note_id"] == "premarket:2026-09-21"
    assert "pre-market note" in view["spoken"]


def test_latest_briefing_absent_is_a_spoken_non_answer():
    view = tools.build_latest_briefing(DESK["notes"], briefing_type="primer")
    assert view["briefing"] is None
    assert "don't have" in view["spoken"]


def test_latest_briefing_tool_dispatches_and_is_read_only():
    server = build_server({"/api/v1/desk": DESK})
    result = rpc(
        server,
        "tools/call",
        {"name": "market_latest_briefing", "arguments": {"briefing_type": "premarket"}},
    )
    assert result["result"]["isError"] is False
    body = json.loads(result["result"]["content"][0]["text"])
    assert body["briefing"]["note_id"] == "premarket:2026-09-21"


def test_brief_includes_the_latest_note():
    server = build_server(
        {"/api/v1/shell/status": STATUS, "/api/v1/shell/context": CONTEXT, "/api/v1/desk": DESK}
    )
    body = server.dispatch("market_brief", {})
    assert "Hotter CPI pushes yields up" in body["spoken"]


@pytest.mark.parametrize("name", tools.RETIRED_TOOLS)
def test_retired_tools_answer_plainly_without_calling_the_backend(name):
    class NoCalls(FakeClient):
        def get_json(self, path, params=None):
            raise AssertionError(f"retired tool called {path}")

    server = McpServer(NoCalls({}), launcher=lambda d, u: None)
    result = rpc(server, "tools/call", {"name": name, "arguments": {}})
    assert result["result"]["isError"] is False
    body = json.loads(result["result"]["content"][0]["text"])
    assert body["available"] is False and "retired" in body["spoken"]


def test_no_tool_exposes_sql_or_mutation():
    for tool in TOOLS:
        assert "sql" not in tool["inputSchema"].get("properties", {})
        assert not any(word in tool["name"] for word in ("set", "update", "admin", "configure"))


def test_retired_technical_tool_and_destination_not_exposed():
    from core.interfaces.mcp.launcher import VALID_DESTINATIONS

    assert "market_technical" not in TOOL_NAMES
    assert "technical" not in tools.OPEN_DESTINATIONS
    assert "technical" not in VALID_DESTINATIONS
    assert "technical" not in tools.build_status(status=STATUS, context=CONTEXT)
    brief = tools.build_brief(status=STATUS, context=CONTEXT, note=None)
    assert "VWAP" not in brief["spoken"]
