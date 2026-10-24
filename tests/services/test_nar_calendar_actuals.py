from datetime import timedelta

import pytest

from core.services.calendar_projection import project_calendar
from tests.providers.test_economic_nar import NOW, URL, parse_release, release


def pair():
    issuer = parse_release(release(), URL, NOW)
    schedule = issuer.model_copy(update=dict(id='consensus:1', provider='consensus',
        source_event_id='slot', scheduled_at=issuer.released_at-timedelta(minutes=1),
        released_at=None, period=None, unit='value', frequency='release',
        status=issuer.status.__class__.DUE, actual=None, actual_basis=None,
        consensus=3_950_000, previous=3_980_000))
    # Validate copies to mirror stored provider records.
    return issuer, type(issuer).model_validate(schedule.model_dump())


def test_unique_match_preserves_sources_schedule_consensus_and_revisions():
    issuer, schedule = pair()
    result = project_calendar([issuer, schedule], now=NOW)
    assert len(result) == 1
    item = result[0]
    assert item.actual == 3_990_000 and item.raw_surprise == 40_000
    assert item.scheduled_at == schedule.scheduled_at
    assert item.released_at == issuer.released_at
    assert item.consensus == 3_950_000 and item.previous == 3_980_000
    assert {e.id for e in item.occurrence_sources} == {issuer.id, schedule.id}
    assert project_calendar(result, now=NOW) == result


@pytest.mark.parametrize('changes', [dict(period='2026-08'), dict(unit='percent'),
    dict(scheduled_at=NOW-timedelta(days=1)), dict(actual=4_000_000), dict(status='cancelled')])
def test_conflicts_do_not_reconcile(changes):
    issuer, schedule = pair()
    schedule = type(schedule).model_validate({**schedule.model_dump(), **changes})
    assert len(project_calendar([issuer, schedule], now=NOW)) == 2


def test_ambiguity_and_future_observation_do_not_reconcile():
    issuer, schedule = pair()
    duplicate = schedule.model_copy(update={'id': 'consensus:2'})
    assert len(project_calendar([issuer, schedule, duplicate], now=NOW)) == 3
    cutoff = NOW - timedelta(hours=1)
    assert project_calendar([issuer, schedule], now=cutoff) == []
    known_schedule = schedule.model_copy(update={"received_at": cutoff})
    assert project_calendar([issuer, known_schedule], now=cutoff) == [known_schedule]
