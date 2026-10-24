from datetime import timedelta

from core.services.calendar_projection import project_calendar
from tests.providers.test_economic_adp import MONTHLY, MURL, NOW, WEEKLY, WURL, parse_release
from tests.services.test_economics import event


def schedule(issuer, **updates):
    return event(
        id="schedule",
        source_event_id="schedule",
        provider="consensus",
        event_code="ADP",
        name=issuer.name,
        scheduled_at=issuer.released_at,
        received_at=NOW,
        unit="value",
        consensus=20000,
        previous=22500,
        status="due",
    ).model_copy(update=updates)


def test_weekly_exact_occurrence_retains_forecast_source_aliases_and_is_repeatable():
    issuer = parse_release(WEEKLY, WURL, NOW)
    source = schedule(issuer)
    rows = project_calendar([source, issuer], now=NOW)
    assert len(rows) == 1
    result = rows[0]
    assert (result.actual, result.raw_surprise, result.consensus) == (23750, 3750, 20000)
    assert result.period == "2026-09-19" and result.frequency == "weekly"
    assert source.id in result.occurrence_aliases and issuer.id in result.occurrence_aliases
    assert {r.provider for r in result.occurrence_sources} == {"consensus", "adp_releases"}
    assert project_calendar(rows, now=NOW) == rows


def test_monthly_legacy_payroll_classification_and_minute_alias_reconcile():
    issuer = parse_release(MONTHLY, MURL, NOW)
    old = schedule(issuer, event_code="PAYROLLS")
    new = schedule(
        issuer,
        id="corrected",
        source_event_id="corrected",
        scheduled_at=issuer.released_at + timedelta(minutes=1),
    )
    rows = project_calendar([old, new, issuer], now=NOW)
    assert len(rows) == 1 and rows[0].event_code == "ADP"
    assert rows[0].actual == 90000 and rows[0].revised_previous == 36000
    assert next(r for r in rows[0].occurrence_sources if r.id == old.id).event_code == "PAYROLLS"


def test_no_cross_frequency_day_period_or_future_slot_leakage():
    issuer = parse_release(WEEKLY, WURL, NOW)
    for updates in [
        {"name": "ADP Non-Farm Employment Change"},
        {"scheduled_at": issuer.released_at - timedelta(days=7)},
        {"period": "2026-09-12"},
        {"scheduled_at": issuer.released_at + timedelta(minutes=6)},
        {"unit": "percent"},
    ]:
        rows = project_calendar([schedule(issuer, **updates), issuer], now=NOW)
        assert len(rows) == 2
        assert next(r for r in rows if r.provider == "consensus").actual is None
    rows = project_calendar(
        [schedule(issuer), issuer], now=issuer.released_at - timedelta(seconds=1)
    )
    assert rows == []  # Neither source had been received at this replay instant.


def test_future_received_schedule_revision_cannot_supply_forecast_or_alias():
    issuer = parse_release(WEEKLY, WURL, NOW)
    original = schedule(issuer)
    future = schedule(issuer, id="future", source_event_id="future", consensus=99999,
                      received_at=NOW + timedelta(days=1))
    rows = project_calendar([original, future, issuer], now=NOW)
    assert len(rows) == 1 and rows[0].consensus == 20000
    assert "future" not in rows[0].occurrence_aliases
    assert all(row.received_at <= NOW for row in rows[0].occurrence_sources)
    assert project_calendar([future, issuer], now=NOW) == [issuer]


def test_ambiguous_issuer_or_multiple_distinct_schedules_fail_closed():
    issuer = parse_release(WEEKLY, WURL, NOW)
    other = issuer.model_copy(update={"id": "other-issuer"})
    assert len(project_calendar([schedule(issuer), issuer, other], now=NOW)) == 3
    other_slot = schedule(issuer, id="other-slot", name="ADP Weekly Employment Estimate")
    rows = project_calendar([schedule(issuer), other_slot, issuer], now=NOW)
    assert len(rows) == 3 and all(r.actual is None for r in rows if r.provider == "consensus")


def test_repository_keeps_issuer_revisions_and_restarts_with_same_occurrence(tmp_path):
    from core.providers.economic_base import EconomicBatch
    from core.services.economics import EconomicService
    from core.storage.economics import SQLiteEconomicRepository

    repository = SQLiteEconomicRepository(tmp_path / "adp.sqlite", mode="live")
    repository.initialize()
    service = EconomicService(repository=repository, mode="live", clock=lambda: NOW)
    issuer = parse_release(WEEKLY, WURL, NOW)
    try:
        service.accept_batch(
            EconomicBatch(
                provider="consensus", checked_at=NOW, events=[schedule(issuer)], status="healthy"
            )
        )
        service.accept_batch(
            EconomicBatch(
                provider="adp_releases", checked_at=NOW, events=[issuer], status="healthy"
            )
        )
        first = service.calendar_occurrence("schedule")
        revised = parse_release(WEEKLY.replace(b"23,750", b"24,000"), WURL, NOW)
        service.accept_batch(
            EconomicBatch(
                provider="adp_releases", checked_at=NOW, events=[revised], status="healthy"
            )
        )
        current = service.calendar_occurrence("schedule")
        assert current.id == first.id and current.actual == 24000
        assert current.revision > first.revision
        versions = repository.events(all_versions=True)
        assert sorted(row.actual for row in versions if row.provider == "adp_releases") == [
            23750,
            24000,
        ]
        restarted = EconomicService(repository=repository, mode="live", clock=lambda: NOW)
        restarted._seed()
        assert restarted.calendar_occurrence("schedule") == current
        assert repository.events(all_versions=True) == versions
        assert any(check.name == "adp_releases" for check in service.calendar_provider_health())
    finally:
        repository.close()
