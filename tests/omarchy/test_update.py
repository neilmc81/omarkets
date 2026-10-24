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
        "summary": "Cuts less likely.", "tone": "negative"}}
    assert update.desk_summary({"notes": [{"id": "x", "draft": None}]})["note"] is None
    assert update.desk_summary(None) is None


def test_theme_summary_keeps_valid_palette_roles_only():
    theme = {"version": "abc", "colors": {
        "green": "#549e6a", "red": "#ff5345", "orange": "#a2734b", "blue": "javascript:x",
        "magenta": "#d2689c"}}
    assert update.theme_summary(theme) == {"version": "abc", "colors": {
        "green": "#549e6a", "red": "#ff5345", "orange": "#a2734b"}}
    assert update.theme_summary(None) is None
    assert update.theme_summary({"colors": "nope"}) is None
