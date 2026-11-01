"""Tests for rebuilding the running service's settings and summarising note costs."""

from __future__ import annotations

import json
import os
import sqlite3

from core.config import effective
from core.config.settings import DEEPSEEK, OPUS


def _rows(environ, env_file):
    settings, rows = effective.effective_settings(environ, env_file)
    return settings, {row.name: row for row in rows}


def test_service_env_wins_and_sources_are_named(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("MARKETOS_AI_WEEKLY_BUDGET_USD=10\nMARKETOS_ECONOMIC_MODE=live\n")
    settings, rows = _rows({"MARKETOS_AI_WEEKLY_BUDGET_USD": "2.50"}, env_file)
    assert settings.ai_weekly_budget_usd == 2.5
    assert rows["ai_weekly_budget_usd"].source == "service env (MARKETOS_AI_WEEKLY_BUDGET_USD)"
    assert rows["economic_mode"].source == ".env (MARKETOS_ECONOMIC_MODE)"
    assert rows["desk_move_threshold_pct"].source == "default"


def test_new_prefix_beats_legacy_prefix(tmp_path):
    environ = {"OMARKETS_AI_WEEKLY_BUDGET_USD": "3", "MARKETOS_AI_WEEKLY_BUDGET_USD": "9"}
    settings, rows = _rows(environ, None)
    assert settings.ai_weekly_budget_usd == 3
    assert rows["ai_weekly_budget_usd"].source == "service env (OMARKETS_AI_WEEKLY_BUDGET_USD)"


def test_secret_values_are_never_returned(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("MARKETOS_FRED_API_KEY=fred-secret-value\n")
    _, rows = _rows({"OPENROUTER_API_KEY": "sk-or-secret-value"}, env_file)
    assert rows["openrouter_api_key"].value == "set"
    assert rows["openrouter_api_key"].source == "service env (OPENROUTER_API_KEY)"
    assert rows["fred_api_key"].value == "set"
    assert rows["bls_api_key"].value == "not set"
    assert not any("secret-value" in row.value for row in rows.values())


def test_caller_environment_is_restored():
    before = dict(os.environ)
    effective.effective_settings({"OMARKETS_API_PORT": "1"}, None)
    assert dict(os.environ) == before


def test_desk_routing_reports_defaults_and_overrides():
    settings, _ = _rows({"OMARKETS_DESK_MODELS": '{"move": "x/model"}'}, None)
    routing = effective.desk_routing(settings)
    assert routing["premarket"] == OPUS
    assert routing["release"] == DEEPSEEK
    assert routing["move"] == "x/model"
    assert routing["(fallback)"] == OPUS


def test_desk_instruments_show_as_the_json_to_set_them_with():
    _, rows = _rows({}, None)
    assert rows["desk_instruments"].value == '["NQ", "ES", "YM", "GOLD"]'
    assert rows["desk_instruments"].source == "default"
    assert rows["desk_prompt_version"].value == "desk:v9"
    assert rows["desk_prompt_version"].source == "default"
    _, rows = _rows({"OMARKETS_DESK_INSTRUMENTS": '["NQ"]'}, None)
    assert rows["desk_instruments"].value == '["NQ"]'


def test_note_costs_group_by_model_and_prompt_version(tmp_path):
    db_path = tmp_path / "desk.sqlite3"
    db = sqlite3.connect(db_path)
    db.execute(
        "CREATE TABLE desk_notes (id TEXT PRIMARY KEY, kind TEXT, status TEXT,"
        " created_at TEXT, attempts INTEGER, payload TEXT)"
    )
    notes = [
        ("a", "2026-10-20T00:00:00Z", "opus · desk:v4", 0.05),
        ("b", "2026-10-21T00:00:00Z", "deepseek · desk:v4", 0.001),
        ("c", "2026-10-22T00:00:00Z", "opus · desk:v4", 0.07),
        ("d", "2026-10-23T00:00:00Z", None, None),
    ]
    for note_id, at, model, cost in notes:
        payload = json.dumps({"model": model, "cost_usd": cost})
        db.execute(
            "INSERT INTO desk_notes VALUES (?, 'move', 'ok', ?, 1, ?)", (note_id, at, payload)
        )
    db.commit()
    db.close()

    rows = {row.setup: row for row in effective.note_costs(db_path)}
    assert rows["opus · desk:v4"].notes == 2
    assert abs(rows["opus · desk:v4"].total_usd - 0.12) < 1e-9
    assert abs(rows["opus · desk:v4"].mean_usd - 0.06) < 1e-9
    assert rows["opus · desk:v4"].last_at.startswith("2026-10-22")
    assert rows["unknown"].total_usd == 0.0


def test_note_costs_missing_db_is_empty(tmp_path):
    assert effective.note_costs(tmp_path / "absent.sqlite3") == []
