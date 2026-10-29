"""Entrypoint wiring tests: economic service construction from settings."""

from __future__ import annotations

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
