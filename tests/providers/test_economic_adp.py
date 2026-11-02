from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from core.providers.economic_adp import (
    ADPReleasesProvider,
    discover_releases,
    latest_due_release,
    monthly_release_day,
    parse_release,
    weekly_release_day,
)
from core.providers.economic_base import ReleasePending
from core.providers.economic_factory import build_economic_providers

FIXTURES = Path(__file__).parents[1] / "fixtures/economics"
NOW = datetime(2026, 10, 8, tzinfo=UTC)
WEEKLY = (FIXTURES / "adp-weekly-2026-10-06.html").read_bytes()
MONTHLY = (FIXTURES / "adp-monthly-2026-09-30.html").read_bytes()
WURL = "https://mediacenter.adp.com/2026-10-06-ADP-National-Employment-Report-Preliminary-Estimate-for-September-19,-2026"
MURL = "https://mediacenter.adp.com/2026-09-30-ADP-National-Employment-Report-Private-Sector-Employment-Increased-by-90,000-Jobs-in-September"


def test_verified_issuer_actuals_have_distinct_period_units_and_revisions():
    weekly = parse_release(WEEKLY, WURL, NOW)
    assert (weekly.actual, weekly.period, weekly.frequency) == (23750, "2026-09-19", "weekly")
    assert weekly.unit == "jobs/week (four-week average, seasonally adjusted)"
    assert weekly.previous is None and weekly.revised_previous == 22500
    assert weekly.released_at == datetime(2026, 10, 6, 12, 15, tzinfo=UTC)
    monthly = parse_release(MONTHLY, MURL, NOW)
    assert (monthly.actual, monthly.period, monthly.unit) == (90000, "2026-09", "jobs")
    assert (monthly.previous, monthly.revised_previous) == (38000, 36000)
    assert monthly.received_at == NOW and monthly.scheduled_at is None
    assert len(monthly.source_revision) == 64


@pytest.mark.parametrize(
    "body,url",
    [
        (WEEKLY.replace(b"23,750", b"unavailable"), WURL),
        (WEEKLY.replace(b"9/19/2026", b"9/12/2026"), WURL),
        (WEEKLY.replace(b'content="ADP Media Center"', b'content="Other issuer"'), WURL),
        (MONTHLY.replace(b"Change in U.S. Private Employment", b"Unavailable total"), MURL),
        (MONTHLY.replace(b"ITEMDATE:", b"NO-DATE:"), MURL),
    ],
)
def test_missing_changed_or_ambiguous_evidence_rejected(body, url):
    with pytest.raises(ValueError):
        parse_release(body, url, NOW)


def test_zero_is_numeric_and_content_revision_preserves_release_identity():
    original = parse_release(WEEKLY, WURL, NOW)
    zero = parse_release(WEEKLY.replace(b"23,750", b"0"), WURL, NOW)
    assert zero.actual == 0 and zero.id == original.id
    assert zero.source_revision != original.source_revision
    changed = parse_release(WEEKLY.replace(b"23,750", b"24,000"), WURL, NOW)
    assert changed.actual == 24000 and changed.id == original.id


def test_future_release_and_out_of_window_listing_are_not_reused():
    with pytest.raises(ValueError):
        parse_release(WEEKLY, WURL, datetime(2026, 10, 6, 12, 14, tzinfo=UTC))
    body = (FIXTURES / "adp-listing-2026-10-08.html").read_bytes()
    assert WURL in discover_releases(body, NOW)
    assert WURL not in discover_releases(body, datetime(2026, 10, 5, tzinfo=UTC))
    assert discover_releases(body, datetime(2027, 1, 1, tzinfo=UTC)) == []


async def test_provider_wiring_partial_failure_health_and_bounded_fetch():
    listing = (
        '<meta property="og:site_name" content="ADP Media Center">'
        f'<a href="{WURL}">ADP National Employment Report Preliminary Estimate</a>'
        f'<a href="{MURL}">ADP National Employment Report: monthly</a>'
    ).encode()
    requests = []

    def handler(request):
        requests.append(str(request.url))
        if "?l=" in str(request.url):
            return httpx.Response(200, content=listing)
        return httpx.Response(200, content=WEEKLY if str(request.url) == WURL else b"bad")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await ADPReleasesProvider(client, clock=lambda: NOW).fetch()
    assert batch.status == "degraded" and len(batch.events) == 1
    assert batch.events[0].actual == 23750 and batch.issues
    assert len(requests) == 4
    assert "adp_releases" in [p.name for p in build_economic_providers("live")]


def test_expected_release_schedule_matches_issuer_dates():
    # Monthly: Wednesday of BLS jobs-report week, including New Year moves.
    assert monthly_release_day(2025, 12) == date(2026, 1, 7)
    assert monthly_release_day(2026, 6) == date(2026, 7, 1)  # jobs report Thu Jul 2
    assert monthly_release_day(2026, 9) == date(2026, 9, 30)
    assert monthly_release_day(2026, 10) == date(2026, 11, 4)
    assert monthly_release_day(2026, 11) == date(2026, 12, 2)
    assert monthly_release_day(2026, 12) == date(2027, 1, 6)
    # Weekly: Tuesdays, Wednesday after a Monday holiday, none in monthly weeks.
    assert weekly_release_day(date(2026, 9, 7)) == date(2026, 9, 9)  # Labor Day
    assert weekly_release_day(date(2026, 10, 12)) == date(2026, 10, 13)  # Columbus Day
    assert weekly_release_day(date(2026, 9, 28)) is None
    assert weekly_release_day(date(2026, 11, 2)) is None
    # Mon Nov 2 to Wed Nov 4 09:14 ET: last due monthly is still Sep 30.
    assert latest_due_release("monthly", datetime(2026, 11, 4, 14, 14, tzinfo=UTC)) == datetime(
        2026, 9, 30, 12, 15, tzinfo=UTC
    )
    assert latest_due_release("monthly", datetime(2026, 11, 4, 14, 15, tzinfo=UTC)) == datetime(
        2026, 11, 4, 13, 15, tzinfo=UTC
    )
    assert latest_due_release("weekly", datetime(2026, 11, 3, 18, tzinfo=UTC)) == datetime(
        2026, 10, 27, 12, 15, tzinfo=UTC
    )


async def _collect(now, pages):
    listing = ('<meta property="og:site_name" content="ADP Media Center">' + "".join(
        f'<a href="{url}">ADP National Employment Report</a>' for url in pages
    )).encode()

    def handler(request):
        url = str(request.url)
        return httpx.Response(200, content=listing if "?l=" in url else pages[url])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await ADPReleasesProvider(client, clock=lambda: now).fetch()


def _family_issues(batch, family):
    return [issue for issue in batch.issues if f"ADP {family} " in issue]


async def test_monthly_missing_before_expected_release_is_pending_then_overdue():
    pages = {WURL: WEEKLY, MURL: MONTHLY}
    # Wed Nov 4, 08:00 ET: October's report is due 08:15 ET; 35 days since Sep 30.
    early = await _collect(datetime(2026, 11, 4, 13, tzinfo=UTC), pages)
    assert _family_issues(early, "monthly") == []
    # Still pending inside the grace hour.
    grace = await _collect(datetime(2026, 11, 4, 14, 14, tzinfo=UTC), pages)
    assert _family_issues(grace, "monthly") == []
    late = await _collect(datetime(2026, 11, 4, 14, 16, tzinfo=UTC), pages)
    assert _family_issues(late, "monthly") == [
        "ADP monthly report not found after its expected release (Wed Nov 04, 08:15 ET)"
    ]


async def test_weekly_missing_before_tuesday_release_is_pending_then_overdue():
    pages = {WURL: WEEKLY, MURL: MONTHLY}
    before = await _collect(datetime(2026, 10, 13, 12, tzinfo=UTC), pages)  # 08:00 ET
    assert before.status == "healthy" and before.issues == []
    after = await _collect(datetime(2026, 10, 13, 13, 16, tzinfo=UTC), pages)  # 09:16 ET
    assert after.status == "degraded"
    assert after.issues == [
        "ADP weekly report not found after its expected release (Tue Oct 13, 08:15 ET)"
    ]


async def test_future_dated_release_page_is_pending_not_rejected():
    # Tue Oct 6 08:14 ET: the weekly page is listed a minute before 08:15 ET.
    batch = await _collect(datetime(2026, 10, 6, 12, 14, tzinfo=UTC), {WURL: WEEKLY, MURL: MONTHLY})
    assert not any(WURL in issue for issue in batch.issues)
    assert [event.frequency for event in batch.events] == ["monthly"]
    with pytest.raises(ReleasePending):
        parse_release(WEEKLY, WURL, datetime(2026, 10, 6, 12, 14, tzinfo=UTC))
