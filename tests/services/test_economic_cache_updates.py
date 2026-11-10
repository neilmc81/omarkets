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


def test_calendar_projection_is_cached_until_source_data_changes(service, monkeypatch):
    """Repeated calendar reads and ticks must not re-project unchanged data on the loop."""
    import core.services.economics as module

    calls = []
    real = module.project_calendar

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(module, "project_calendar", counting)
    service.accept_batch(batch(events=[event()]))  # its tick projects once
    first = service.calendar_events()
    for _ in range(5):
        assert service.calendar_events() == first
        service.tick()
    assert len(calls) == 1

    service.accept_batch(batch(events=[event(id="other", source_event_id="other")]))
    assert len(service.calendar_events()) == len(first) + 1
    assert len(calls) == 2

    service.accept_batch(batch(observations=[observation(period="2026-09")]))
    service.calendar_events()
    assert len(calls) == 3


def assert_cache_tracks_fresh(service, clock, moments):
    """Cached reads must equal a fresh projection as the clock moves both ways."""
    for moment in [*moments, *reversed(moments)]:
        clock[0] = moment
        assert service._project_calendar() == service._build_calendar(moment), moment


def test_cache_expires_when_a_schedule_slot_arrives_after_its_issuer_release():
    from tests.providers.test_economic_adp import MONTHLY, MURL, NOW, parse_release

    issuer = parse_release(MONTHLY, MURL, NOW)
    received = issuer.released_at + timedelta(seconds=30)
    issuer = issuer.model_copy(update={"received_at": received})
    slot = issuer.released_at + timedelta(minutes=1)
    schedule = event(
        id="schedule", source_event_id="schedule", provider="consensus",
        event_code="ADP", name=issuer.name, scheduled_at=slot,
        received_at=received, unit="value", consensus=20000,
        previous=22500, status="due",
    )
    clock = [received]
    service = EconomicService(mode="live", clock=lambda: clock[0])
    service._events = [schedule, issuer]
    assert len(service._project_calendar()) == 2
    assert_cache_tracks_fresh(service, clock, [received, slot - timedelta(seconds=1), slot,
                                               slot + timedelta(hours=1)])
    clock[0] = slot
    assert len(service._project_calendar()) == 1


def test_cache_expires_when_retained_ism_history_is_received():
    from datetime import UTC, datetime

    from core.domain.macro_registry import MACRO_DEFINITIONS

    definition = MacroRegistry(definitions=MACRO_DEFINITIONS).get("ISM_MFG")
    july_slot = datetime(2026, 8, 3, 14, tzinfo=UTC)
    late = datetime(2026, 10, 5, tzinfo=UTC)
    now = datetime(2026, 10, 2, tzinfo=UTC)

    def observed(period, slot, received_at, value):
        return observation(
            id="ISM_MFG", provider="ism_newswire",
            source_series_id=definition.source_series_id, period=period,
            released_at=slot, received_at=received_at, value=value, unit="index points",
        )

    clock = [now]
    service = EconomicService(mode="live", clock=lambda: clock[0],
                              registry=MacroRegistry(definitions=MACRO_DEFINITIONS))
    service._release_versions = [
        observed("2026-07", july_slot, late, 49),
        observed("2026-09", datetime(2026, 10, 1, 14, tzinfo=UTC), now, 52),
    ]
    service._events = [event(
        id="issuer:ISM_MFG:2026-07", source_event_id="ISM_MFG:2026-07",
        provider="ism_newswire", event_code="ISM_MFG", period="2026-07",
        name=definition.name, scheduled_at=None, released_at=july_slot,
        received_at=july_slot + timedelta(minutes=1), unit="index points", actual=49,
        actual_basis="source_release", status="released",
    )]
    service._refresh_releases()

    def aliases():
        return {alias for row in service._project_calendar() for alias in row.occurrence_aliases}

    assert "observed:ISM_MFG:2026-07" not in aliases()
    assert_cache_tracks_fresh(service, clock, [now, late - timedelta(seconds=1), late])
    clock[0] = late
    assert "observed:ISM_MFG:2026-07" in aliases()


def test_cache_from_a_superseded_source_version_is_not_stored(service):
    service.accept_batch(batch(events=[event()]))
    real = service._calendar_inputs

    def racing():
        inputs = real()
        service._calendar_version += 1  # a batch lands mid-projection
        return inputs

    service._calendar_cache = None
    service._calendar_inputs = racing
    service._project_calendar()
    assert service._calendar_cache is None


def test_callers_cannot_mutate_the_cached_projection(service):
    service.accept_batch(batch(events=[event(notes=["original"])]))
    first = service._project_calendar()
    first[0].notes.append("poison")
    first[0].occurrence_aliases.append("poison")
    first.clear()
    again = service._project_calendar()
    assert again[0].notes == ["original"]
    assert "poison" not in again[0].occurrence_aliases
