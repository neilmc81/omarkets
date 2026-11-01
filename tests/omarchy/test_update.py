"""Tests for the Omarkets bar data collector (omarchy/omarkets/update.py).

The collector is the core→shell transport boundary. It must copy the core's
normalized payload verbatim, validate its shape/version, write atomically, and
retain the last good status across failed attempts. It is imported by path because
it is a standalone stdlib script, not part of the Python package.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

UPDATE_PATH = Path(__file__).resolve().parents[2] / "omarchy" / "omarkets" / "update.py"


def load_module():
    spec = importlib.util.spec_from_file_location("omarkets_update", UPDATE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


update = load_module()

SAMPLE_STATUS = {
    "version": 2,
    "generated_at": "2026-09-20T14:00:00Z",
    "primary": {"symbol": "NQ", "value": 24780.5, "change_pct": 0.42, "feed_status": "delayed"},
    "strip": [
        {"symbol": "NQ", "change_pct": 0.42, "feed_status": "delayed"},
        {"symbol": "US2Y", "change_bp": 4.1, "feed_status": "live"},
        {"symbol": "VIX", "value": 16.8, "feed_status": "live"},
    ],
    "session": {"phase": "overnight", "maintenance_break": False},
    "next_event": {"name": "CPI", "scheduled_at": "2026-09-24T12:30:00Z", "importance": "critical"},
    "data_health": "ok",
    "ai_health": "ok",
}


def test_collect_copies_core_payload_verbatim(monkeypatch):
    monkeypatch.setattr(update, "get", lambda base, path: (SAMPLE_STATUS, None))
    record = update.collect("http://127.0.0.1:8765")
    assert record["version"] == update.SUPPORTED_VERSION
    assert record["error"] is None
    # The status object is passed through unchanged — no shell-side reshaping.
    assert record["status"] == SAMPLE_STATUS
    assert record["status"]["primary"]["change_pct"] == 0.42


def test_collect_records_version_mismatch(monkeypatch):
    newer = dict(SAMPLE_STATUS, version=99)
    monkeypatch.setattr(update, "get", lambda base, path: (newer, None))
    record = update.collect("http://127.0.0.1:8765")
    assert record["error"] == "partial"
    assert "version" in record["errors"]


def test_collect_raises_when_status_unavailable(monkeypatch):
    monkeypatch.setattr(update, "get", lambda base, path: (None, "unreachable: refused"))
    try:
        update.collect("http://127.0.0.1:8765")
    except RuntimeError as exc:
        assert "unreachable" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected RuntimeError")


def test_collect_raises_on_malformed_payload(monkeypatch):
    monkeypatch.setattr(update, "get", lambda base, path: ("not-a-dict", None))
    try:
        update.collect("http://127.0.0.1:8765")
    except RuntimeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected RuntimeError")


def test_ascii_port_validation():
    assert update._ASCII_DIGITS.fullmatch("8765")
    assert not update._ASCII_DIGITS.fullmatch("\u00b2")  # isdigit() true, int() fails
    assert not update._ASCII_DIGITS.fullmatch("80x")


def test_main_rejects_bad_port(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(update, "OUT", str(tmp_path / "bar.json"))
    assert update.main(["update.py", "not-a-port"]) == 2
    assert update.main(["update.py", "99999"]) == 2
    assert not (tmp_path / "bar.json").exists()


def test_write_atomic_replaces_and_is_valid_json(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(update, "OUT", str(tmp_path / "bar.json"))
    update.write_atomic(
        {"version": 1, "status": SAMPLE_STATUS, "updatedAt": "2026-09-20T14:00:00Z"}
    )
    written = json.loads((tmp_path / "bar.json").read_text())
    assert written["status"]["primary"]["value"] == 24780.5
    assert [p.name for p in tmp_path.iterdir()] == ["bar.json"]


def test_main_retains_last_good_status_and_marks_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(update, "OUT", str(tmp_path / "bar.json"))
    # Seed a good file, then fail.
    update.write_atomic(
        {"version": 1, "status": SAMPLE_STATUS, "updatedAt": "2026-09-20T14:00:00Z"}
    )

    def boom(base):
        raise RuntimeError("network down")

    monkeypatch.setattr(update, "collect", boom)
    assert update.main(["update.py", "8765"]) == 1
    # The previous good status and timestamp remain, but the UI learns that
    # a retry is needed after the first missed request.
    kept = json.loads((tmp_path / "bar.json").read_text())
    assert kept["status"] == SAMPLE_STATUS
    assert kept["updatedAt"] == "2026-09-20T14:00:00Z"
    assert kept["consecutiveFailures"] == 1
    assert kept["error"] == "transport"
    assert kept["attemptedAt"]


def test_failure_count_recovers_after_success(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(update, "OUT", str(tmp_path / "bar.json"))
    monkeypatch.setattr(
        update, "collect", lambda base: (_ for _ in ()).throw(RuntimeError("timeout"))
    )
    assert update.main(["update.py"]) == 1
    assert update.main(["update.py"]) == 1
    failed = json.loads((tmp_path / "bar.json").read_text())
    assert failed["consecutiveFailures"] == 2
    assert "status" not in failed

    monkeypatch.setattr(
        update,
        "collect",
        lambda base: {
            "status": SAMPLE_STATUS,
            "updatedAt": "2026-09-20T14:00:00Z",
            "consecutiveFailures": 0,
        },
    )
    assert update.main(["update.py"]) == 0
    recovered = json.loads((tmp_path / "bar.json").read_text())
    assert recovered["consecutiveFailures"] == 0
    assert recovered["status"] == SAMPLE_STATUS


def test_desk_summary_keeps_only_the_latest_note_headline():
    desk = {"session_text": "US market open", "notes": [{
        "id": "release:x", "kind": "release", "title": "CPI", "created_at": "2026-10-14T12:51:00Z",
        "draft": {"headline": "Hot inflation", "summary": "Cuts less likely.",
                  "nq_tone": "negative", "what_it_means": "long text"},
    }]}
    summary = update.desk_summary(desk)
    assert summary == {"session_text": "US market open", "note": {
        "id": "release:x", "kind": "release", "title": "CPI",
        "created_at": "2026-10-14T12:51:00Z", "headline": "Hot inflation",
        "summary": "Cuts less likely.", "tone": "negative", "tone_for": None}}
    assert update.desk_summary({"notes": [{"id": "x", "draft": None}]})["note"] is None


@pytest.mark.parametrize(("note", "tone_for"), [
    ({"tone_for": "ES", "instrument": "ES"}, "ES"),
    ({"tone_for": "NQ", "instrument": None}, "NQ"),  # desk-wide event note
    ({"instrument": "ES"}, "ES"),                    # an older core
    ({}, None),
])
def test_desk_summary_names_the_instrument_the_tone_is_about(note, tone_for):
    desk = {"notes": [{
        "id": "move:ES:x", "kind": "move", "title": "ES move", "created_at": "2026-10-14T12:51:00Z",
        "draft": {"headline": "ES slid", "summary": "Selling.", "tone": "negative"}, **note,
    }]}
    assert update.desk_summary(desk)["note"]["tone_for"] == tone_for
    assert update.desk_summary(None) is None


def test_theme_summary_keeps_valid_palette_roles_only():
    theme = {"version": "abc", "colors": {
        "green": "#549e6a", "red": "#ff5345", "orange": "#a2734b", "blue": "javascript:x",
        "magenta": "#d2689c"}}
    assert update.theme_summary(theme) == {"version": "abc", "colors": {
        "green": "#549e6a", "red": "#ff5345", "orange": "#a2734b"}}
    assert update.theme_summary(None) is None
    assert update.theme_summary({"colors": "nope"}) is None


# --- W1 popup inputs: sparklines and the hero's prior-week range ------------

CONTEXT = {"version": 2, "cross_market": [
    {"symbol": "NQ", "nq_effect": "hurts", "short_label": "Nasdaq 100 futures"},
    {"symbol": "ES"}, {"symbol": "US2Y"}, {"symbol": "bad symbol"}, {"symbol": "ES"},
]}
SPARKLINES = {"generated_at": "2026-10-30T21:00:00Z", "interval": "15m", "lines": [
    {"symbol": "NQ", "points": [
        {"time": "2026-10-30T13:30:00Z", "value": 28600.0},
        {"time": "2026-10-30T13:45:00Z", "value": None},
        {"time": "2026-10-30T14:00:00Z", "value": 28542}]},
    {"symbol": "ES", "points": []},
    {"symbol": "US2Y", "points": [{"time": "t", "value": "4.59"}]},
    {"symbol": "VIX", "points": [7]},
]}


def levels(symbol="NQ", **week_low):
    low = {"key": "prior_week_low", "value": 28410.0, "value_text": "28,410.00",
           "window_start": "2026-10-18T22:00:00Z", "window_end": "2026-10-23T21:00:00Z",
           "unavailable_reason": None, **week_low}
    return {"symbol": symbol, "trade_date": "2026-10-30", "contract": "NQZ26", "delayed": True,
            "levels": [
                {"key": "prior_settle", "value": 29560.25, "value_text": "29,560.25"},
                {"key": "prior_week_high", "value": 29880.0, "value_text": "29,880.00",
                 "window_start": "2026-10-18T22:00:00Z", "window_end": "2026-10-23T21:00:00Z",
                 "unavailable_reason": None},
                low,
            ]}


def fake_api(responses: dict, calls: list | None = None):
    def get(base, path):
        if calls is not None:
            calls.append(path)
        for prefix, answer in responses.items():
            if path.startswith(prefix):
                return answer
        return None, "http 404"
    return get


def test_popup_symbols_follow_the_context_rows_then_the_bar_strip():
    assert update.popup_symbols(SAMPLE_STATUS, CONTEXT) == ["NQ", "ES", "US2Y"]
    assert update.popup_symbols(SAMPLE_STATUS, None) == ["NQ", "US2Y", "VIX"]
    assert update.popup_symbols({}, {"cross_market": []}) == []
    many = {"cross_market": [{"symbol": f"S{i}"} for i in range(30)]}
    assert len(update.popup_symbols({}, many)) == update.MAX_SPARKLINES


def test_sparklines_summary_keeps_values_and_gaps_only():
    summary = update.sparklines_summary(SPARKLINES)
    assert summary["interval"] == "15m"
    assert summary["lines"]["NQ"] == {
        "start": "2026-10-30T13:30:00Z", "end": "2026-10-30T14:00:00Z",
        "values": [28600.0, None, 28542.0]}
    # An empty line is kept (the popup draws nothing); a non-number is a gap,
    # never a guessed value; a malformed line is dropped.
    assert summary["lines"]["ES"] == {"start": None, "end": None, "values": []}
    assert summary["lines"]["US2Y"]["values"] == [None]
    assert "VIX" not in summary["lines"]
    assert update.sparklines_summary(SAMPLE_STATUS) is None
    assert update.sparklines_summary(None) is None


def test_hero_summary_carries_the_prior_week_range_and_settle():
    hero = update.hero_summary(levels(), "NQ")
    assert hero == {
        "symbol": "NQ", "trade_date": "2026-10-30", "contract": "NQZ26", "delayed": True,
        "prior_week_high": 29880.0, "prior_week_high_text": "29,880.00",
        "prior_week_low": 28410.0, "prior_week_low_text": "28,410.00",
        "prior_settle": 29560.25, "prior_settle_text": "29,560.25",
        "week_start": "2026-10-18T22:00:00Z", "week_end": "2026-10-23T21:00:00Z",
        "week_unavailable_reason": None,
    }


def test_hero_summary_never_invents_a_level():
    missing = update.hero_summary(levels(value=None, value_text="unavailable",
                                         unavailable_reason="contract rolled"), "NQ")
    assert missing["prior_week_low"] is None and missing["prior_week_low_text"] is None
    assert missing["prior_week_high"] == 29880.0
    # Another instrument's levels are never shown as the focus's.
    assert update.hero_summary(levels("ES"), "NQ") is None
    assert update.hero_summary({"symbol": "NQ"}, "NQ")["prior_week_high"] is None
    assert update.hero_summary(None, "NQ") is None


def test_collect_adds_sparklines_and_hero_for_the_focus(monkeypatch):
    calls: list = []
    status = dict(SAMPLE_STATUS, focus="NQ")
    monkeypatch.setattr(update, "get", fake_api({
        "/api/v1/shell/status": (status, None),
        "/api/v1/shell/context": (CONTEXT, None),
        "/api/v1/desk": (None, None),
        "/api/v1/theme": (None, None),
        "/api/v1/sparklines": (SPARKLINES, None),
        "/api/v1/levels/NQ": (levels(), None),
    }, calls))
    record = update.collect("http://127.0.0.1:8765")
    assert "/api/v1/sparklines?symbols=NQ,ES,US2Y" in calls
    assert "/api/v1/levels/NQ" in calls
    assert record["error"] is None
    assert record["sparklines"]["lines"]["NQ"]["values"][-1] == 28542.0
    assert record["hero"]["prior_week_high"] == 29880.0
    # Existing fields are untouched: the current Panel keeps working.
    assert record["status"] == status
    assert record["context"] == CONTEXT


def test_collect_without_key_levels_has_no_hero_and_no_error(monkeypatch):
    status = dict(SAMPLE_STATUS, focus="BTC")
    monkeypatch.setattr(update, "get", fake_api({
        "/api/v1/shell/status": (status, None),
        "/api/v1/sparklines": (SPARKLINES, None),
    }))
    record = update.collect("http://127.0.0.1:8765")
    assert record["hero"] is None
    assert record["errors"]["levels"] is None
    # The context endpoint failing still yields sparklines for the bar strip.
    assert record["errors"]["context"] == "http 404"
    assert record["sparklines"]["lines"]["NQ"]


def test_collect_records_sparkline_and_level_failures_as_partial(monkeypatch):
    monkeypatch.setattr(update, "get", fake_api({
        "/api/v1/shell/status": (SAMPLE_STATUS, None),
        "/api/v1/shell/context": (CONTEXT, None),
        "/api/v1/desk": (None, None),
        "/api/v1/theme": (None, None),
        "/api/v1/sparklines": (None, "http 503"),
        "/api/v1/levels/NQ": (None, "TimeoutError"),
    }))
    record = update.collect("http://127.0.0.1:8765")
    assert record["error"] == "partial"
    assert record["errors"]["sparklines"] == "http 503"
    assert record["errors"]["levels"] == "TimeoutError"
    assert record["sparklines"] is None and record["hero"] is None
    assert record["status"] == SAMPLE_STATUS
