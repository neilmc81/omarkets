"""Entrypoint wiring tests: economic service construction from settings."""

from __future__ import annotations

import pytest

from core.config.settings import Settings
from core.domain.economics import EconomicsMode
from core.main import build_economic_service
from core.services.economics import EconomicService
from core.storage.economics import SQLiteEconomicRepository


def test_news_calendar_bridge_bounds_recent_window_before_calendar_limit(tmp_path):
    import asyncio
    from datetime import UTC, datetime, timedelta
    from unittest.mock import Mock, patch

    from core.main import build_news_service

    now = datetime(2026, 10, 1, 13, tzinfo=UTC)
    economics = Mock(mode=EconomicsMode.LIVE)
    economics.calendar_events.return_value = []
    settings = Settings(_env_file=None, data_dir=tmp_path, news_enabled=True)
    with patch("core.providers.news.base.utc_now", return_value=now):
        news = build_news_service(settings, economic_service=economics)
        source = next(item for item in news.sources if item.name == "economic_calendar")
        assert asyncio.run(source.fetch()) == []
    economics.calendar_events.assert_called_once_with(
        start=now - timedelta(days=1), end=now + timedelta(seconds=1),
    )
    economics.observed_releases.assert_not_called()
    assert news.polling.interval("energy_press") == 900  # catalog interval applied
    assert news.polling.interval("cnbc_top") == settings.news_poll_seconds


def test_census_key_reaches_the_census_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("OMARKETS_ECONOMIC_MODE", "live")
    settings = Settings(_env_file=None, data_dir=tmp_path, census_api_key="test-census-key")
    service = build_economic_service(settings)
    census = next(p for p in service.providers if p.name == "census_retail")
    assert census._api_key == "test-census-key"


def test_bea_key_reaches_the_bea_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("OMARKETS_ECONOMIC_MODE", "live")
    settings = Settings(_env_file=None, data_dir=tmp_path, bea_api_key="test-bea-key")
    service = build_economic_service(settings)
    bea = next(p for p in service.providers if p.name == "bea_data")
    assert bea._api_key == "test-bea-key"


def test_disabled_mode_builds_bare_service(tmp_path, monkeypatch):
    monkeypatch.setenv("OMARKETS_ECONOMIC_MODE", "disabled")
    settings = Settings(_env_file=None, data_dir=tmp_path)
    service = build_economic_service(settings)
    assert isinstance(service, EconomicService)
    assert service.mode == EconomicsMode.DISABLED
    assert service.providers == ()
    assert service.repository is None


def test_demo_mode_builds_demo_provider_and_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("OMARKETS_ECONOMIC_MODE", "demo")
    settings = Settings(_env_file=None, data_dir=tmp_path)
    service = build_economic_service(settings)
    assert service.mode == EconomicsMode.DEMO
    assert [provider.name for provider in service.providers] == ["demo"]
    assert [item.id for item in service.registry.definitions] == ["CPI_MOM"]
    assert service.registry.definitions[0].provider == "demo"
    assert isinstance(service.repository, SQLiteEconomicRepository)
    assert service.repository.mode == EconomicsMode.DEMO


def test_live_mode_builds_full_provider_set(tmp_path, monkeypatch):
    monkeypatch.setenv("OMARKETS_ECONOMIC_MODE", "live")
    settings = Settings(_env_file=None, data_dir=tmp_path)
    service = build_economic_service(settings)
    names = [provider.name for provider in service.providers]
    assert names == [
        "bls",
        "nyfed",
        "bea_calendar",
        "bea_releases",
        "bea_data",
        "fed_calendar",
        "fed_releases",
        "fred_release_calendar",
        "fred",
        "consensus",
        "dol_claims",
        "ism_newswire",
        "adp_releases",
        "nar_releases",
        "census_retail",
        "census_releases",
        "fed_g17",
        "conference_board",
    ]
    assert len(service.registry.definitions) > 1


def test_build_app_wires_only_the_desk_era_services(tmp_path):
    from core.main import build_app

    settings = Settings(
        _env_file=None, data_dir=tmp_path, market_data_provider="fake",
        market_data_mode="demo", economic_mode="disabled", news_enabled=False,
    )
    app = build_app(settings)
    assert app.state.desk is not None
    for retired in ("decisions", "analysts", "briefings", "historical", "research",
                    "technical", "notifications"):
        assert not hasattr(app.state, retired), retired


# --- desk_instruments wiring (Phase 2 slice 1) ---


def _desk_settings(tmp_path, **kwargs):
    return Settings(_env_file=None, data_dir=tmp_path, market_data_provider="fake",
                    market_data_mode="demo", economic_mode="disabled", news_enabled=False,
                    **kwargs)


def test_default_desk_writes_for_nq_es_ym_and_gold_on_desk_v9(tmp_path):
    from core.main import build_app

    app = build_app(_desk_settings(tmp_path))
    assert [i.symbol for i in app.state.desk.instruments] == ["NQ", "ES", "YM", "GOLD"]
    assert app.state.desk.prompt_version == "desk:v9"


def test_nq_only_on_desk_v8_can_still_be_selected(tmp_path):
    from core.main import build_app

    app = build_app(_desk_settings(tmp_path, desk_instruments=["NQ"],
                                   desk_prompt_version="desk:v8"))
    assert [i.symbol for i in app.state.desk.instruments] == ["NQ"]
    assert app.state.desk.prompt_version == "desk:v8"


def test_enabling_es_under_desk_v8_is_refused_with_a_fix(tmp_path):
    from core.main import DeskConfigError, build_app

    settings = _desk_settings(tmp_path, desk_instruments=["NQ", "ES"],
                              desk_prompt_version="desk:v8")
    with pytest.raises(DeskConfigError) as refused:
        build_app(settings)
    message = str(refused.value)
    assert "ES" in message and "desk:v8 has no prompt for it" in message
    assert "OMARKETS_DESK_PROMPT_VERSION=desk:v9" in message
    assert "omarketsctl config" in message
    # Refused before any store was opened.
    assert not (tmp_path / "desk.sqlite3").exists()


def test_refusal_stops_the_server_with_the_message(tmp_path, monkeypatch):
    import core.main as main

    settings = _desk_settings(tmp_path, desk_prompt_version="desk:v8")
    monkeypatch.setattr(main, "Settings", lambda: settings)
    monkeypatch.setattr(main.uvicorn, "Config", lambda *a, **k: pytest.fail("server built"))
    with pytest.raises(SystemExit, match="omarkets: desk_instruments includes ES"):
        main.main()


def test_es_is_wired_through_under_desk_v9(tmp_path):
    from core.main import build_app

    app = build_app(_desk_settings(tmp_path, desk_instruments=["NQ", "ES"],
                                   desk_prompt_version="desk:v9"))
    assert [i.symbol for i in app.state.desk.instruments] == ["NQ", "ES"]


def test_the_default_list_under_desk_v8_names_es_ym_and_gold(tmp_path):
    from core.main import DeskConfigError, build_app

    with pytest.raises(DeskConfigError, match="includes ES, YM, GOLD, but desk:v8 has no "
                                              "prompt for them") as refused:
        build_app(_desk_settings(tmp_path, desk_prompt_version="desk:v8"))
    assert "OMARKETS_DESK_PROMPT_VERSION=desk:v9" in str(refused.value)
    assert not (tmp_path / "desk.sqlite3").exists()


def test_enabling_ym_under_desk_v8_is_refused_with_a_fix(tmp_path):
    from core.main import DeskConfigError, build_app

    with pytest.raises(DeskConfigError, match="includes YM, but desk:v8 has no prompt for "
                                              "it") as refused:
        build_app(_desk_settings(tmp_path, desk_instruments=["NQ", "YM"],
                                 desk_prompt_version="desk:v8"))
    message = str(refused.value)
    assert "set OMARKETS_DESK_PROMPT_VERSION=desk:v9 or remove YM" in message
    assert not (tmp_path / "desk.sqlite3").exists()


def test_ym_is_wired_through_under_desk_v9(tmp_path):
    from core.main import build_app

    app = build_app(_desk_settings(tmp_path, desk_instruments=["NQ", "YM"]))
    assert [i.symbol for i in app.state.desk.instruments] == ["NQ", "YM"]


def test_enabling_gold_under_desk_v8_is_refused_with_a_fix(tmp_path):
    from core.main import DeskConfigError, build_app

    with pytest.raises(DeskConfigError, match="includes GOLD, but desk:v8 has no prompt for "
                                              "it") as refused:
        build_app(_desk_settings(tmp_path, desk_instruments=["NQ", "GOLD"],
                                 desk_prompt_version="desk:v8"))
    assert "set OMARKETS_DESK_PROMPT_VERSION=desk:v9 or remove GOLD" in str(refused.value)
    assert not (tmp_path / "desk.sqlite3").exists()


def test_gold_is_wired_through_under_desk_v9(tmp_path):
    from core.main import build_app

    app = build_app(_desk_settings(tmp_path, desk_instruments=["NQ", "GOLD"]))
    assert [i.symbol for i in app.state.desk.instruments] == ["NQ", "GOLD"]


def test_an_instrument_without_a_v9_prompt_is_refused(tmp_path):
    from core.main import DeskConfigError, build_app

    with pytest.raises(DeskConfigError, match="includes BTC, but desk:v9 has no prompt") as refused:
        build_app(_desk_settings(tmp_path, desk_instruments=["NQ", "ES", "YM", "BTC"]))
    assert "remove BTC from OMARKETS_DESK_INSTRUMENTS" in str(refused.value)
    assert not (tmp_path / "desk.sqlite3").exists()


def test_a_disabled_desk_is_not_checked(tmp_path):
    from core.main import build_app

    app = build_app(_desk_settings(tmp_path, desk_enabled=False,
                                   desk_prompt_version="desk:v8"))
    assert app.state.desk is None
