from datetime import UTC, datetime, timedelta

import pytest

from core.domain.economics import EconomicEvent, MacroObservation, MacroProvenance
from core.services.economics import EconomicService

SLOT = datetime(2026, 9, 29, 14, tzinfo=UTC)
NOW = SLOT + timedelta(days=2)
BLS_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"


def schedule(provider: str = "fred_release_calendar") -> EconomicEvent:
    return EconomicEvent(
        id=f"{provider}:jolts-2026-09-29",
        event_code="JOLTS",
        name="Job Openings and Labor Turnover Survey",
        category="employment",
        importance="high",
        scheduled_at=SLOT,
        unit="release" if provider == "fred_release_calendar" else "value",
        frequency="release",
        status="due",
        consensus=7230000 if provider == "consensus" else None,
        provider=provider,
        source_event_id="192:2026-09-29",
        source_url="https://fred.stlouisfed.org/releases/calendar?rid=192&y=2026",
        received_at=SLOT - timedelta(days=1),
        mode="live",
    )


def observation(period: str = "2026-08", received_at: datetime = NOW) -> MacroObservation:
    return MacroObservation(
        id="JOLTS",
        period=period,
        value=7079,
        unit="thousand jobs",
        source_series_id="JTS000000000000000JOL",
        provider="bls",
        received_at=received_at,
        metadata=MacroProvenance(source_url=BLS_URL, source_revision='[{"code":"P"}]'),
    )


def test_verified_current_bls_vintage_appears_on_matching_jolts_slot():
    service = EconomicService(mode="live", clock=lambda: NOW)
    service._events = [schedule(), schedule("consensus")]
    service._observations = [observation()]

    rows = service.calendar_events(start=SLOT, end=SLOT + timedelta(days=1))
    official = next(row for row in rows if row.provider == "fred_release_calendar")
    consensus = next(row for row in rows if row.provider == "consensus")
    assert official.actual == 7079
    assert official.actual_basis == "current_vintage"
    assert official.period == "2026-08"
    assert official.unit == "thousand jobs"
    assert official.released_at is None
    assert official.raw_surprise is None
    assert official.source_url == BLS_URL
    assert any("original release value" in note for note in official.notes)
    assert consensus.actual is None
    assert consensus.consensus == 7230000
    assert consensus.raw_surprise is None


def test_calendar_join_rejects_wrong_period_or_prepublication_observation():
    service = EconomicService(mode="live", clock=lambda: NOW)
    service._events = [schedule()]
    service._observations = [
        observation(period="2026-07"),
        observation(received_at=SLOT - timedelta(seconds=1)),
    ]
    result = service.calendar_events(start=SLOT, end=SLOT + timedelta(days=1))
    assert len(result) == 1
    assert result[0].actual is None
    assert result[0].status == "due"


@pytest.mark.parametrize("code,day", [("ISM_MFG", 1), ("ISM_SERVICES", 5)])
def test_ism_issuer_release_joins_only_exact_calendar_slot_and_period(code, day):
    slot = datetime(2026, 10, day, 14, tzinfo=UTC)
    now = slot + timedelta(hours=1)
    scheduled = EconomicEvent(
        id="consensus:ism-september",
        event_code=code,
        name="ISM Manufacturing PMI",
        category="growth",
        importance="high",
        scheduled_at=slot,
        unit="value",
        frequency="release",
        status="due",
        consensus=54.8,
        previous=54.6,
        provider="consensus",
        source_event_id="ism-2026-10-01",
        source_url="https://nfs.faireconomy.media/ff_calendar_thisweek.json",
        received_at=slot - timedelta(days=1),
        mode="live",
    )
    issued = EconomicEvent(
        id=f"ism_newswire:2026-09:{code}",
        event_code=code,
        name="ISM Manufacturing PMI",
        category="growth",
        importance="high",
        period="2026-09",
        unit="index points",
        frequency="monthly",
        status="released",
        actual=55.1,
        actual_basis="source_release",
        provider="ism_newswire",
        source_event_id=f"2026-09:{code}",
        source_url="https://www.prnewswire.com/news-releases/ism-september.html",
        released_at=slot,
        received_at=slot + timedelta(minutes=3),
        mode="live",
    )
    service = EconomicService(mode="live", clock=lambda: now)
    service._events = [scheduled, issued]
    linked = service.calendar_event_state(scheduled)
    assert linked.actual == 55.1
    assert linked.actual_basis == "source_release"
    assert linked.period == "2026-09"
    assert linked.raw_surprise == 55.1 - 54.8
    assert linked.released_at == slot
    assert scheduled.actual is None
    cancelled = scheduled.model_copy(update={"status": "cancelled", "period": "2026-09"})
    cancelled_state = service.calendar_event_state(cancelled)
    assert cancelled_state.status == "cancelled" and cancelled_state.actual is None
    conflicting = scheduled.model_copy(update={"period": "2026-08"})
    conflicting_state = service.calendar_event_state(conflicting)
    assert conflicting_state.period == "2026-08" and conflicting_state.actual is None
    explicit = scheduled.model_copy(update={"period": "2026-09"})
    assert service.calendar_event_state(explicit).actual == 55.1
    shifted = scheduled.model_copy(update={"scheduled_at": slot + timedelta(minutes=1)})
    assert service.calendar_event_state(shifted).actual is None
    for change in [
        {"received_at": now + timedelta(seconds=1)},
        {"unit": "percent"},
        {"actual_basis": "current_vintage"},
        {"country": "CA"},
        {"mode": "demo"},
    ]:
        service._events = [scheduled, issued.model_copy(update=change)]
        assert service.calendar_event_state(scheduled).actual is None, change
        projected = service.calendar_events(start=slot, end=now)
        assert next(row for row in projected if row.provider == "consensus").actual is None
    future_schedule = scheduled.model_copy(update={"received_at": now + timedelta(seconds=1)})
    service._events = [future_schedule, issued]
    assert service.calendar_event_state(future_schedule).actual is None
    service._events = [scheduled, issued.model_copy(update={"period": "2026-08"})]
    assert service.calendar_event_state(scheduled).actual is None


JOBS_SLOT = datetime(2026, 10, 2, 12, 30, tzinfo=UTC)


def jobs_schedule(code="PAYROLLS", provider="consensus", **updates):
    template = schedule().model_dump()
    template.update(
        id=f"{provider}:{code}", provider=provider, event_code=code,
        scheduled_at=JOBS_SLOT, period=None, name=code,
        unit="value" if code == "PAYROLLS" else "percent",
        consensus=150000 if code == "PAYROLLS" else 4.2 if code == "UNEMPLOYMENT" else .3,
        previous=120000 if code == "PAYROLLS" else 4.3 if code == "UNEMPLOYMENT" else .2,
    )
    template.update(updates)
    return EconomicEvent.model_validate(template)


def jobs_observation(code="PAYROLLS", **updates):
    series, identifier, value, unit = {
        "PAYROLLS": ("PAYROLLS", "CES0000000001", 160, "thousand jobs"),
        "UNEMPLOYMENT": ("UNEMPLOYMENT", "LNS14000000", 4.1, "percent"),
        "AHE": ("AHE_MOM", "CES0500000003", .2, "percent"),
    }[code]
    template = observation().model_dump()
    template.update(id=series, source_series_id=identifier, value=value, unit=unit,
                    period="2026-09", received_at=JOBS_SLOT + timedelta(minutes=5))
    template.update(updates)
    return MacroObservation.model_validate(template)


def jobs_service(code="PAYROLLS"):
    service = EconomicService(mode="live", clock=lambda: JOBS_SLOT + timedelta(hours=1))
    official = jobs_schedule(provider="bls_calendar", unit="release", period="2026-09",
                             consensus=None, previous=None)
    scheduled = jobs_schedule(code)
    service._events = [official, scheduled]
    service._observations = [jobs_observation(code)]
    return service, scheduled


@pytest.mark.parametrize("code,actual,unit", [
    ("PAYROLLS", 160000, "jobs"), ("UNEMPLOYMENT", 4.1, "percent"), ("AHE", .2, "percent"),
])
def test_jobs_consensus_joins_verified_current_vintage_with_compatible_units(code, actual, unit):
    service, scheduled = jobs_service(code)
    original = scheduled.model_dump_json()
    linked = service.calendar_event_state(scheduled)
    assert linked.actual == actual
    assert linked.unit == unit
    assert linked.actual_basis == "current_vintage"
    assert linked.period == "2026-09"
    assert linked.released_at is None
    assert linked.consensus == scheduled.consensus
    assert linked.previous == scheduled.previous
    assert linked.raw_surprise == actual - scheduled.consensus
    assert scheduled.model_dump_json() == original
    assert linked.revision == scheduled.revision
    assert service.calendar_events(start=JOBS_SLOT, end=JOBS_SLOT + timedelta(hours=1))


@pytest.mark.parametrize("mutation", ["no_official", "slot", "period", "unit", "series", "source"])
def test_jobs_join_fails_closed_on_unverified_slot_period_series_or_units(mutation):
    service, scheduled = jobs_service("AHE")
    if mutation == "no_official":
        service._events = [scheduled]
    elif mutation == "slot":
        scheduled = scheduled.model_copy(update={"scheduled_at": JOBS_SLOT + timedelta(minutes=1)})
    elif mutation == "period":
        scheduled = scheduled.model_copy(update={"period": "2026-08"})
    elif mutation == "unit":
        service._observations = [jobs_observation("AHE", unit="USD")]
    elif mutation == "series":
        service._observations = [jobs_observation("AHE", id="AHE_YOY")]
    else:
        service._observations = [jobs_observation("AHE", metadata=MacroProvenance(
            source_url="https://example.com/not-bls"))]
    assert service.calendar_event_state(scheduled).actual is None


def test_current_actual_is_not_overwritten_and_conflicting_same_revision_is_rejected():
    service, scheduled = jobs_service()
    released = scheduled.model_copy(update={"actual": 180000})
    assert service.calendar_event_state(released).actual == 180000
    service._observations.append(jobs_observation(value=170))
    assert service.calendar_event_state(scheduled).actual is None
    service._observations[-1] = jobs_observation(value=170, revision=2)
    assert service.calendar_event_state(scheduled).actual == 170000
    assert service._observations[0].value == 160


def test_jobs_requires_reference_period_on_matching_official_schedule_if_supplied():
    service, scheduled = jobs_service("UNEMPLOYMENT")
    service._events[0] = service._events[0].model_copy(update={"period": "2026-08"})
    assert service.calendar_event_state(scheduled).actual is None
