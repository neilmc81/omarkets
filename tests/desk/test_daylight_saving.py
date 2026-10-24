"""The desk schedule follows New York clock time across daylight-saving changes.

US clocks fall back on Nov 1 2026 (EDT, UTC-4 -> EST, UTC-5) and spring forward
on Mar 14 2027. Pre-market must stay at 09:00 New York time, so its UTC time
(and the user's local time) moves by an hour.
"""

from datetime import UTC, date, datetime, timedelta

import pytest

from core.desk.models import NoteKind
from core.desk.service import RELEASE_DELAY
from core.providers.earnings_nasdaq import EarningsDate
from tests.desk.test_service import cpi_rows, kinds, make


def utc(*args):
    return datetime(*args, tzinfo=UTC)


@pytest.mark.parametrize(("day", "nine_et_utc_hour"), [
    (date(2026, 10, 30), 13),  # Friday, EDT
    (date(2026, 11, 2), 14),   # Monday after fall-back, EST
    (date(2027, 3, 12), 14),   # Friday before spring-forward, EST
    (date(2027, 3, 15), 13),   # Monday after spring-forward, EDT
])
async def test_premarket_stays_at_nine_new_york_time(day, nine_et_utc_hour):
    service, _, _ = make(utc(day.year, day.month, day.day, 0))
    nine = utc(day.year, day.month, day.day, nine_et_utc_hour)
    assert NoteKind.PREMARKET not in await kinds(service, nine - timedelta(minutes=5))
    assert NoteKind.PREMARKET in await kinds(service, nine + timedelta(minutes=5))


async def test_primer_opens_at_five_pm_new_york_on_the_fall_back_sunday():
    service, _, _ = make(utc(2026, 11, 1, 0))
    # Sunday Nov 1 2026 17:00 EST is 22:00 UTC (it would be 21:00 UTC in EDT).
    assert NoteKind.PRIMER not in await kinds(service, utc(2026, 11, 1, 21, 30))
    assert NoteKind.PRIMER in await kinds(service, utc(2026, 11, 1, 22, 5))


def test_session_text_counts_down_to_the_est_open():
    service, _, _ = make(utc(2026, 11, 2, 0))
    # 13:30 UTC on Nov 2 is 08:30 EST: one hour before the 09:30 open.
    assert service.session_text(utc(2026, 11, 2, 13, 30)).endswith("US open in 1h 00m")


def test_earnings_report_times_follow_new_york_clock():
    oct_ = EarningsDate(symbol="META", company="Meta", day=date(2026, 10, 28),
                        timing="after_close")
    nov = EarningsDate(symbol="NVDA", company="Nvidia", day=date(2026, 11, 18),
                       timing="after_close")
    assert oct_.expected_at == utc(2026, 10, 28, 20, 5)  # 16:05 EDT
    assert nov.expected_at == utc(2026, 11, 18, 21, 5)  # 16:05 EST


async def test_release_notes_wait_for_the_full_delayed_reaction_in_winter():
    # 08:30 EST release on Friday Nov 6 2026 is 13:30 UTC.
    release = utc(2026, 11, 6, 13, 30)
    rows = cpi_rows()
    for row in rows:
        row.scheduled_at = release
    service, _, _ = make(release, rows=rows)
    assert RELEASE_DELAY == timedelta(minutes=26)
    assert NoteKind.RELEASE not in await kinds(service, release + timedelta(minutes=25))
    assert NoteKind.RELEASE in await kinds(service, release + timedelta(minutes=27))
