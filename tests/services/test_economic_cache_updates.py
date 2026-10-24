"""Incremental economic caches must match canonical full repository reloads."""
from datetime import timedelta

import pytest

from core.domain.economics import EconomicsMode, MacroRegistry
from core.services.economics import EconomicService
from core.storage.economics import SQLiteEconomicRepository
from tests.services.test_economics import T0, batch, definition, event, observation


def assert_matches_reload(service):
    loaded = EconomicService(repository=service.repository, registry=service.registry,
                             mode=service.mode, clock=service.clock)
    loaded._seed()
    assert service._observations == loaded._observations
    assert service._events == loaded._events
    assert service._release_versions == loaded._release_versions
    assert service._series_checks == loaded._series_checks
    assert service._checks == loaded._checks
    assert service.observed_releases() == loaded.observed_releases()
    assert service.current_state() == loaded.current_state()


@pytest.fixture
def service(tmp_path):
    repository = SQLiteEconomicRepository(tmp_path / "economics.sqlite3", EconomicsMode.LIVE)
    repository.initialize()
    instance = EconomicService(repository=repository, mode=EconomicsMode.LIVE,
                               registry=MacroRegistry(definitions=(definition(),)),
                               clock=lambda: T0 + timedelta(days=1))
    yield instance
    repository.close()


def test_unseeded_prefilled_repository_is_loaded_before_first_delta(service):
    service.repository.store_batch(batch(observations=[observation(period="2026-07")]))
    assert not service._cache_seeded
    service.accept_batch(batch(observations=[observation(period="2026-08")]))
    assert service._cache_seeded
    assert len(service._observations) == 2
    assert_matches_reload(service)


@pytest.mark.parametrize("value", [None, 0, 3.5])
def test_revisions_and_missing_or_zero_preserve_history_and_latest_order(service, value):
    service.accept_batch(batch(observations=[
        observation(period="2026-07"), observation(period="2026-08"),
    ], events=[event(), event(id="other", source_event_id="other")]))
    service.accept_batch(batch(observations=[
        observation(period="2026-07", value=value),
        observation(period="2026-08", value=3.8),
        observation(period="2026-07", value=4.2),
    ], events=[event(actual=3), event(actual=4)]))
    assert_matches_reload(service)
    assert [item.period for item in service._observations] == ["2026-08", "2026-07"]
    assert service._observations[-1].revision == 3
    assert service._events[-1].revision == 3
    assert len(service._release_versions) == 5


def test_distinct_source_provider_unit_and_event_period_changes_use_storage_keys(service):
    batches = [
        batch(observations=[observation(), observation(source_series_id="other")],
              events=[event(), event(source_event_id="other")]),
        batch(observations=[observation(provider="fred", source_series_id="fred-source")],
              events=[event(provider="fred", source_event_id="fred-source")], provider="fred"),
        batch(observations=[observation(unit="different-unit")],
              events=[event(period="2026-09")]),
    ]
    for item in batches:
        service.accept_batch(item)
        assert_matches_reload(service)
    assert len(service._observations) == 3  # Changed unit revises the same storage identity.
    assert len(service._events) == 3  # Event period is payload, not storage identity.


def test_identical_empty_and_failed_checks_update_without_observation_decode(service, monkeypatch):
    first = batch()
    service.accept_batch(first)
    expected_versions = list(service._release_versions)
    observations = service.repository.observations
    events = service.repository.events

    def forbidden_reload(*args, **kwargs):
        raise AssertionError("batch updates must not decode complete economic history")

    monkeypatch.setattr(service.repository, "observations", forbidden_reload)
    monkeypatch.setattr(service.repository, "events", forbidden_reload)
    for item in [
        first,
        batch(checked_at=T0 + timedelta(minutes=5)),
        batch(observations=[], events=[], checked_series_ids=[], status="degraded",
              checked_at=T0 + timedelta(minutes=10), issues=["test missing series"]),
        batch(observations=[], events=[], status="unavailable", checked_series_ids=[],
              checked_at=T0 + timedelta(minutes=15)),
    ]:
        changes = service.accept_batch(item)
        assert changes.observations == []
        assert changes.events == []
        assert service._release_versions == expected_versions
    monkeypatch.setattr(service.repository, "observations", observations)
    monkeypatch.setattr(service.repository, "events", events)
    assert_matches_reload(service)
    check = service._series_checks[("bls", "CPI_YOY")]
    assert not check.successful
    assert check.last_success_at == T0 + timedelta(minutes=5)


def test_failed_persistence_does_not_apply_uncommitted_cache_delta(service, monkeypatch):
    service.accept_batch(batch())
    old_observations = list(service._observations)
    old_versions = list(service._release_versions)

    def fail(*args, **kwargs):
        raise RuntimeError("test persistence failure")

    monkeypatch.setattr(service.repository, "store_batch", fail)
    with pytest.raises(RuntimeError, match="test persistence failure"):
        service.accept_batch(batch(observations=[observation(value=0)]))
    assert service._observations == old_observations
    assert service._release_versions == old_versions
