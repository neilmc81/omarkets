"""Tests for the typed configuration layer."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.config.settings import Settings


def test_defaults_are_sane(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith(("OMARKETS_", "MARKETOS_")):
            monkeypatch.delenv(key, raising=False)
    settings = Settings(_env_file=None)
    assert settings.api_host == "127.0.0.1"
    assert settings.api_port == 8765
    assert settings.market_data_mode == "live"
    assert settings.data_provider == ""
    assert settings.provider_update_interval > 0
    assert settings.resolved_duckdb_path == settings.data_dir / "omarkets-live.duckdb"


def test_env_vars_override_defaults(monkeypatch):
    monkeypatch.setenv("OMARKETS_API_PORT", "9999")
    monkeypatch.setenv("OMARKETS_PROVIDER_SEED", "123")
    monkeypatch.setenv("OMARKETS_STALE_DEFAULT_SECONDS", "12.5")
    settings = Settings(_env_file=None)
    assert settings.api_port == 9999
    assert settings.provider_seed == 123
    assert settings.stale_default_seconds == 12.5


def test_duckdb_path_precedence(tmp_path):
    explicit = tmp_path / "explicit.duckdb"
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        duckdb_path=explicit,
    )
    assert settings.resolved_duckdb_path == explicit


def test_default_db_path_follows_data_dir_and_mode(tmp_path):
    live = Settings(_env_file=None, data_dir=tmp_path / "data")
    assert live.resolved_duckdb_path == tmp_path / "data" / "omarkets-live.duckdb"
    demo = Settings(_env_file=None, data_dir=tmp_path / "data", market_data_mode="demo")
    assert demo.resolved_duckdb_path == tmp_path / "data" / "omarkets-demo.duckdb"
    # Live and demo must never share a database.
    assert live.resolved_duckdb_path != demo.resolved_duckdb_path


def test_legacy_duckdb_path_is_the_shared_pre_namespacing_file(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path / "data")
    assert settings.legacy_duckdb_path == tmp_path / "data" / "marketos.duckdb"


def test_invalid_port_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, api_port=70000)


def test_invalid_update_interval_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, provider_update_interval=0)


def test_stale_overrides_parsed(monkeypatch):
    monkeypatch.setenv("OMARKETS_STALE_OVERRIDES", '{"equity_index_future": 1.5, "crypto": 9}')
    settings = Settings(_env_file=None)
    assert settings.stale_overrides == {"equity_index_future": 1.5, "crypto": 9.0}


def test_economic_defaults(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith(("OMARKETS_", "MARKETOS_")):
            monkeypatch.delenv(key, raising=False)
    settings = Settings(_env_file=None)
    assert settings.economic_mode == "live"
    assert settings.economic_poll_seconds == 21600
    assert settings.economic_transition_seconds == 30
    assert settings.fred_api_key is None
    assert settings.resolved_economic_db_path == (settings.data_dir / "economics-live.sqlite3")


def test_economic_mode_env_and_demo_db(monkeypatch):
    monkeypatch.setenv("OMARKETS_ECONOMIC_MODE", "demo")
    settings = Settings(_env_file=None, data_dir=__import__("pathlib").Path("./data"))
    assert settings.economic_mode == "demo"
    assert settings.resolved_economic_db_path.name == "economics-demo.sqlite3"


def test_economic_disabled_db_path(monkeypatch):
    monkeypatch.setenv("OMARKETS_ECONOMIC_MODE", "disabled")
    settings = Settings(_env_file=None)
    assert settings.resolved_economic_db_path.name == "economics-disabled.sqlite3"


def test_economic_poll_overrides(monkeypatch):
    monkeypatch.setenv("OMARKETS_ECONOMIC_POLL_OVERRIDES", '{"bls": 90000, "fed_releases": 1800}')
    settings = Settings(_env_file=None)
    assert settings.economic_poll_overrides == {"bls": 90000.0, "fed_releases": 1800.0}


def test_economic_poll_override_below_minimum_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, economic_poll_overrides={"bls": 10})


def test_economic_poll_seconds_below_minimum_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, economic_poll_seconds=5)


def test_api_host_must_be_loopback():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, api_host="0.0.0.0")


def test_fred_api_key_from_env(monkeypatch):
    monkeypatch.setenv("OMARKETS_FRED_API_KEY", "test-key")
    settings = Settings(_env_file=None)
    assert settings.fred_api_key.get_secret_value() == "test-key"


def test_news_defaults(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith(("OMARKETS_", "MARKETOS_")):
            monkeypatch.delenv(key, raising=False)
    settings = Settings(_env_file=None)
    assert settings.news_enabled is True
    assert settings.news_poll_seconds == 300
    assert settings.news_poll_overrides == {}
    assert settings.news_enabled_sources == []
    assert settings.news_disabled_sources == []
    assert settings.news_dedupe_threshold == 0.6
    assert settings.resolved_news_db_path == settings.data_dir / "events.sqlite3"


def test_news_env_overrides(monkeypatch):
    monkeypatch.setenv("OMARKETS_NEWS_ENABLED", "false")
    monkeypatch.setenv("OMARKETS_NEWS_POLL_OVERRIDES", '{"cnbc_top": 60}')
    monkeypatch.setenv("OMARKETS_NEWS_DISABLED_SOURCES", '["bbc_world"]')
    settings = Settings(_env_file=None)
    assert settings.news_enabled is False
    assert settings.news_poll_overrides == {"cnbc_top": 60.0}
    assert settings.news_disabled_sources == ["bbc_world"]


def test_news_poll_override_below_minimum_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, news_poll_overrides={"cnbc_top": 10})


def test_news_poll_seconds_below_minimum_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, news_poll_seconds=5)


def test_news_dedupe_threshold_bounds():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, news_dedupe_threshold=1.5)


def test_openrouter_key_aliases_are_secret(monkeypatch):
    monkeypatch.delenv("OMARKETS_OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    settings = Settings(_env_file=None)
    assert settings.openrouter_api_key.get_secret_value() == "test-openrouter-key"
    assert "test-openrouter-key" not in repr(settings)
    monkeypatch.delenv("OPENROUTER_API_KEY")
    monkeypatch.setenv("OMARKETS_OPENROUTER_API_KEY", "test-prefixed-key")
    assert Settings(_env_file=None).openrouter_api_key.get_secret_value() == "test-prefixed-key"


def test_retired_feature_flags_are_ignored(monkeypatch):
    # Old drop-ins may still set these; they must not break startup.
    for name in ("DECISION", "ANALYST", "WORKFLOW"):
        monkeypatch.setenv(f"OMARKETS_{name}_ENABLED", "false")
    settings = Settings(_env_file=None)
    assert not hasattr(settings, "decision_enabled")
    assert settings.desk_enabled


def test_weekly_budget_settings_validate_timezone_and_cost():
    from pydantic import ValidationError

    settings = Settings(_env_file=None, ai_weekly_budget_usd=10,
                        ai_budget_timezone='Europe/London',
                        ai_openrouter_daily_request_limit=0,
                        ai_openrouter_background_request_limit=0)
    assert settings.ai_weekly_budget_usd == 10
    assert settings.ai_openrouter_daily_request_limit == 0
    for kwargs in ({'ai_budget_timezone': 'unknown/invalid'},
                   {'ai_weekly_budget_usd': float('nan')},
                   {'ai_weekly_budget_usd': -1},
                   {'ai_openrouter_daily_request_limit': -1}):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **kwargs)


# --- MARKETOS_ -> OMARKETS_ rename compatibility ---


@pytest.fixture
def clean_env(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith(("OMARKETS_", "MARKETOS_")) or key == "OPENROUTER_API_KEY":
            monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_old_env_name_still_works(clean_env):
    clean_env.setenv("MARKETOS_API_PORT", "9001")
    clean_env.setenv("MARKETOS_DESK_MODELS", '{"move": "x/y"}')
    settings = Settings(_env_file=None)
    assert settings.api_port == 9001
    assert settings.desk_models == {"move": "x/y"}


def test_new_env_name_works(clean_env):
    clean_env.setenv("OMARKETS_API_PORT", "9002")
    assert Settings(_env_file=None).api_port == 9002


def test_new_env_name_beats_old(clean_env):
    clean_env.setenv("MARKETOS_API_PORT", "9001")
    clean_env.setenv("OMARKETS_API_PORT", "9002")
    assert Settings(_env_file=None).api_port == 9002


def test_dotenv_old_and_new_names(clean_env, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("MARKETOS_API_PORT=9001\nMARKETOS_LOG_LEVEL=DEBUG\nOMARKETS_API_PORT=9002\n")
    settings = Settings(_env_file=env_file)
    assert settings.api_port == 9002
    assert settings.log_level == "DEBUG"


def test_new_name_in_dotenv_beats_old_name_in_environment(clean_env, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("OMARKETS_API_PORT=9002\n")
    clean_env.setenv("MARKETOS_API_PORT", "9001")
    assert Settings(_env_file=env_file).api_port == 9002


def test_env_file_none_skips_legacy_dotenv(clean_env, tmp_path):
    (tmp_path / ".env").write_text("MARKETOS_API_PORT=9001\n")
    clean_env.chdir(tmp_path)
    assert Settings(_env_file=None).api_port == 8765


def test_openrouter_key_accepts_all_names(clean_env):
    clean_env.setenv("MARKETOS_OPENROUTER_API_KEY", "old")
    assert Settings(_env_file=None).openrouter_api_key.get_secret_value() == "old"
    clean_env.setenv("OMARKETS_OPENROUTER_API_KEY", "new")
    assert Settings(_env_file=None).openrouter_api_key.get_secret_value() == "new"
    clean_env.setenv("OPENROUTER_API_KEY", "plain")
    assert Settings(_env_file=None).openrouter_api_key.get_secret_value() == "plain"


def test_existing_marketos_database_is_kept(clean_env, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "marketos-live.duckdb").touch()
    assert Settings(_env_file=None, data_dir=data).resolved_duckdb_path == (
        data / "marketos-live.duckdb"
    )
    (data / "omarkets-live.duckdb").touch()
    assert Settings(_env_file=None, data_dir=data).resolved_duckdb_path == (
        data / "omarkets-live.duckdb"
    )


def test_new_install_uses_omarkets_database(clean_env, tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path)
    assert settings.resolved_duckdb_path == tmp_path / "omarkets-live.duckdb"
