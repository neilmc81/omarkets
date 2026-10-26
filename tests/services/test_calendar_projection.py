from datetime import UTC, datetime, timedelta

import pytest

from core.domain.economics import MacroRegistry
from core.domain.macro_registry import MACRO_DEFINITIONS
from core.services.calendar_projection import project_calendar
from core.services.economics import EconomicMessageType, EconomicService
from core.storage.economics import SQLiteEconomicRepository
from tests.services.test_economics import T0, batch, event, observation


def adp(minute=0, **kwargs):
    return event(
        id=f"adp-{minute}",
        source_event_id=f"adp-{minute}",
        provider="consensus",
        event_code="ADP",
        name="ADP Weekly Employment Change",
        scheduled_at=T0 + timedelta(minutes=minute),
        received_at=T0,
        previous=0,
        **kwargs,
    )


def test_minute_revisions_are_one_stable_occurrence_with_repeatable_aliases():
    rows = [adp(0), adp(1), adp(2)]
    projected = project_calendar(rows)
    assert len(projected) == 1
    assert projected[0].previous == 0
    assert projected[0].occurrence_aliases == ["adp-0", "adp-1", "adp-2"]
    assert len(projected[0].occurrence_sources) == 3
    assert project_calendar(projected) == projected
    assert project_calendar(rows[:1])[0].id == projected[0].id


def test_unrelated_titles_periods_dates_and_distant_slots_do_not_merge():
    monthly = adp().model_copy(update={"id": "monthly", "name": "ADP Employment Change"})
    tomorrow = adp().model_copy(update={"id": "tomorrow", "scheduled_at": T0 + timedelta(days=1)})
    other_period = adp().model_copy(update={"id": "period", "period": "2026-08"})
    assert len(project_calendar([adp(), monthly, tomorrow, other_period])) == 4
    assert len(project_calendar([adp(), adp(60)])) == 2


def test_bea_notice_reconciles_lifecycle_without_inventing_actual():
    schedule = event(
        provider="bea_calendar",
        id="schedule",
        source_event_id="s",
        event_code="BEA_RELEASE",
        name="U.S. International Trade, August 2026",
        period="2026-08",
        unit="release",
        status="due",
    )
    notice = event(
        provider="bea_releases",
        id="notice",
        source_event_id="n",
        event_code="TRADE",
        name="International Trade, August 2026",
        period=None,
        scheduled_at=None,
        released_at=T0 + timedelta(hours=5),
        status="released",
        unit="release",
    )
    projected = project_calendar([schedule, notice])
    assert len(projected) == 1
    assert projected[0].status == "released"
    assert projected[0].actual is None
    assert projected[0].released_at == notice.released_at
    assert projected[0].scheduled_at == schedule.scheduled_at
    assert {row.id for row in projected[0].occurrence_sources} == {"notice", "schedule"}
    different = notice.model_copy(update={"name": "International Trade, July 2026"})
    assert len(project_calendar([schedule, different])) == 2


def test_projection_retains_repository_revisions_and_does_not_write(tmp_path):
    repository = SQLiteEconomicRepository(tmp_path / "economics.sqlite", mode="live")
    repository.initialize()
    service = EconomicService(repository=repository, mode="live", clock=lambda: T0)
    service.accept_batch(batch(provider="consensus", observations=[], events=[adp()]))
    service.accept_batch(batch(provider="consensus", observations=[], events=[adp(1)]))
    before = repository.events(all_versions=True)
    assert len(service.calendar_events()) == 1
    assert len(service.calendar_events()) == 1
    assert repository.events(all_versions=True) == before
    repository.close()


def test_ism_macro_derived_record_and_issuer_notice_share_occurrence():
    issuer = event(
        id="issuer",
        provider="ism_newswire",
        event_code="ISM_SERVICES",
        name="ISM Services PMI",
        period="2026-08",
        scheduled_at=None,
        released_at=T0,
        unit="index points",
        actual=0,
        source_url="https://www.prnewswire.com/news-releases/verified.html",
        status="released",
        actual_basis="source_release",
    )
    derived = issuer.model_copy(
        update={"id": "observed:ISM_SERVICES:2026-08", "name": "ISM services"}
    )
    projected = project_calendar([issuer, derived])
    assert len(projected) == 1
    assert projected[0].actual == 0
    assert set(projected[0].occurrence_aliases) == {issuer.id, derived.id}
    assert project_calendar(projected) == projected


def test_numeric_revisions_keep_ism_occurrence_identity_and_conflicts_stay_separate():
    issuer = event(id="issuer", provider="ism_newswire", event_code="ISM_MFG",
                   period="2026-08", scheduled_at=None, released_at=T0,
                   unit="index points", actual=50, status="released")
    revised = issuer.model_copy(update={"actual": 51, "revision": 2})
    assert project_calendar([issuer])[0].id == project_calendar([revised])[0].id
    conflicting = revised.model_copy(update={"id": "observed:ISM_MFG:2026-08"})
    assert len(project_calendar([issuer, conflicting])) == 2


def test_bea_stable_schedule_uid_survives_date_revision_without_merging_other_dates():
    schedule = event(provider="bea_calendar", source_event_id="stable-bea-uid",
                     name="International Trade, August 2026", period="2026-08")
    moved = schedule.model_copy(update={"scheduled_at": T0 + timedelta(days=1), "revision": 2})
    assert project_calendar([schedule])[0].id == project_calendar([moved])[0].id


@pytest.mark.parametrize("code", ["ISM_MFG", "ISM_SERVICES"])
def test_ism_period_rollover_preserves_members_revision_alias_and_notifications(tmp_path, code):
    repository = SQLiteEconomicRepository(tmp_path / "economics.sqlite", mode="live")
    repository.initialize()
    now = datetime(2026, 10, 7, 14, tzinfo=UTC)
    service = EconomicService(repository=repository, mode="live", clock=lambda: now,
                              registry=MacroRegistry(definitions=MACRO_DEFINITIONS))
    definition = service.registry.get(code)

    def report(period, slot, actual):
        url = f"https://www.prnewswire.com/news-releases/{code}-{period}.html"
        issued = event(
            id=f"issuer:{code}:{period}", source_event_id=f"{code}:{period}",
            provider="ism_newswire", event_code=code, period=period,
            name=definition.name, scheduled_at=None, released_at=slot,
            received_at=slot + timedelta(minutes=1), unit="index points",
            actual=actual, actual_basis="source_release", status="released", source_url=url,
        )
        observed = observation(
            id=code, provider="ism_newswire", source_series_id=definition.source_series_id,
            period=period, released_at=slot, received_at=issued.received_at,
            value=actual, unit="index points", metadata={"source_url": url},
        )
        return batch(provider="ism_newswire", observations=[observed], events=[issued])

    try:
        august = report("2026-08", datetime(2026, 9, 1, 14, tzinfo=UTC), 51)
        service.accept_batch(august)
        alias = f"observed:{code}:2026-08"
        before = service.calendar_occurrence(alias)
        assert before is not None and before.revision == 2
        assert len(before.occurrence_sources) == 2
        mailbox = service.subscribe()
        service.accept_batch(report("2026-09", datetime(2026, 10, 1, 14, tzinfo=UTC), 52))
        after = service.calendar_occurrence(alias)
        assert after == before
        assert service.calendar_occurrence(before.id) == before
        assert next(row for row in service.calendar_events() if row.id == before.id) == before
        assert [row.period for row in service.observed_releases() if row.event_code == code] == [
            "2026-09",
        ]
        released = mailbox.pending[EconomicMessageType.RELEASED].data.ids
        assert before.id not in released
        september = service.calendar_occurrence(f"observed:{code}:2026-09")
        assert released == [september.id]
        mailbox.pending.clear()
        service.tick()
        assert not mailbox.pending
        stored_events = repository.events(all_versions=True)
        stored_observations = repository.observations(all_versions=True)
        restarted = EconomicService(repository=repository, mode="live", clock=lambda: now,
                                    registry=service.registry)
        restarted._seed()
        assert restarted.calendar_occurrence(alias) == before
        assert restarted.calendar_events() == service.calendar_events()
        assert repository.events(all_versions=True) == stored_events
        assert repository.observations(all_versions=True) == stored_observations
    finally:
        repository.close()


def test_old_ism_issuer_without_retained_observation_does_not_invent_member():
    service = EconomicService(mode="live", clock=lambda: T0)
    issuer = event(id="issuer", provider="ism_newswire", event_code="ISM_MFG",
                   period="2026-08", scheduled_at=None, released_at=T0,
                   unit="index points", actual=51, status="released")
    service._events = [issuer]
    projected = service.calendar_events()[0]
    assert projected.revision == 1
    assert projected.occurrence_aliases == ["issuer"]
    assert len(projected.occurrence_sources) == 1
    assert service.calendar_occurrence("observed:ISM_MFG:2026-08") is None


def test_adp_issuer_revision_keeps_its_own_first_reported_previous():
    schedule = event(id="adp-cal", source_event_id="adp-cal", provider="consensus",
                     event_code="ADP", name="ADP Non-Farm Employment Change",
                     scheduled_at=T0, received_at=T0, unit="jobs", previous=36_000)
    issuer = event(id="adp-issuer", source_event_id="adp-issuer", provider="adp_releases",
                   event_code="ADP", name="ADP Non-Farm Employment Change", scheduled_at=None,
                   released_at=T0, received_at=T0, unit="jobs", frequency="monthly",
                   status="released", actual=42_000, actual_basis="source_release",
                   previous=38_000, revised_previous=36_000)
    [row] = [r for r in project_calendar([schedule, issuer]) if r.actual is not None]
    assert (row.previous, row.revised_previous) == (38_000, 36_000)
