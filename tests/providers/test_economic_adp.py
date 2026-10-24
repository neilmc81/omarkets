from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from core.providers.economic_adp import ADPReleasesProvider, discover_releases, parse_release
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
