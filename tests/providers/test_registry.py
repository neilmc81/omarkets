import pytest

from core.domain.instruments import INSTRUMENTS
from core.providers.registry import DEFAULT_ROUTES, ProviderRegistry


def test_default_routes_cover_expected_instruments():
    registry = ProviderRegistry(DEFAULT_ROUTES)
    assert registry.provider_for("NQ") == "cnbc"
    assert registry.provider_for("ES") == "cnbc"
    assert registry.provider_for("BTC") == "binance"
    assert len(registry.routes()) == len(DEFAULT_ROUTES)


def test_routes_only_reference_known_instruments():
    registry = ProviderRegistry(DEFAULT_ROUTES)
    for route in registry.routes():
        assert route.symbol in INSTRUMENTS


def test_symbols_for_provider_is_deterministic():
    registry = ProviderRegistry(DEFAULT_ROUTES)
    assert registry.symbols_for("binance") == ("BTC",)
    assert "NQ" in registry.symbols_for("cnbc")
    assert registry.symbols_for("nobody") == ()


def test_unknown_symbol_rejected():
    with pytest.raises(ValueError):
        ProviderRegistry({"NOTREAL": "cnbc"})


def test_provider_for_normalizes_case_and_whitespace():
    registry = ProviderRegistry(DEFAULT_ROUTES)
    assert registry.provider_for(" nq ") == "cnbc"
    assert registry.provider_for("nope") is None


def test_with_routes_overrides_without_mutating():
    registry = ProviderRegistry(DEFAULT_ROUTES)
    updated = registry.with_routes({"NQ": "other"})
    assert updated.provider_for("NQ") == "other"
    assert registry.provider_for("NQ") == "cnbc"
