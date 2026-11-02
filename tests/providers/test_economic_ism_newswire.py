from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from core.providers.economic_ism_newswire import (
    ISMNewswireProvider,
    discover_release,
    expected_release,
    parse_release,
)

RELEASE_HREF = (
    "/news-releases/manufacturing-pmi-at-54-6-august-2026-"
    "ism-manufacturing-pmi-report-302865127.html"
)
LISTING = (
    "<html><title>News from Institute for Supply Management</title>"
    "<h1>News from Institute for Supply Management</h1>"
    f'<a href="{RELEASE_HREF}">Manufacturing PMI® at 54.6%; '
    "August 2026 ISM® Manufacturing PMI® Report</a></html>"
).encode()
RELEASE = (
    "<html><head><title>Manufacturing PMI® at 54.6%; August 2026 "
    'ISM® Manufacturing PMI® Report</title><meta name="author" '
    'content="Institute for Supply Management"></head><body>'
    '<h2 class="meta">News provided by</h2><strong>Institute for Supply Management</strong>'
    '<p class="mb-no">Sep 01, 2026, 10:00 ET</p>'
    "<table><tr><td>Manufacturing PMI®</td><td>54.6</td><td>55.6</td></tr>"
    "<tr><td>Prices</td><td>71.1</td><td>71.1</td></tr></table></body></html>"
).encode()


def test_public_listing_and_issuer_release_parse_exact_period_and_values():
    assert discover_release(LISTING, 8, 2026).endswith(RELEASE_HREF)
    published, pmi, prices = parse_release(RELEASE, 8, 2026)
    assert published == datetime(2026, 9, 1, 14, tzinfo=UTC)
    assert (pmi, prices) == (54.6, 71.1)
    assert discover_release(LISTING, 9, 2026) is None


@pytest.mark.parametrize(
    "body",
    [
        RELEASE.replace(b'Institute for Supply Management"', b'Another Issuer"'),
        RELEASE.replace(b"August 2026", b"July 2026"),
        RELEASE.replace(b"<td>71.1</td>", b"<td>missing</td>"),
        RELEASE.replace(b"<td>54.6</td>", b"<td>54.7</td>"),
    ],
)
def test_changed_issuer_period_or_table_fails_closed(body):
    with pytest.raises(ValueError):
        parse_release(body, 8, 2026)


async def test_provider_returns_two_source_release_actuals_without_invented_schedule():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/news/institute-for-supply-management/":
            return httpx.Response(200, content=LISTING)
        assert request.url.path == RELEASE_HREF
        return httpx.Response(200, content=RELEASE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await ISMNewswireProvider(
            client, clock=lambda: datetime(2026, 9, 1, 15, tzinfo=UTC)
        ).fetch()
    # Services is not due until Sep 3, so its absence is not an issue.
    assert batch.status == "healthy"
    assert batch.issues == []
    assert [(item.event_code, item.actual) for item in batch.events] == [
        ("ISM_MFG", 54.6),
        ("ISM_MFG_PRICES", 71.1),
    ]
    assert all(item.actual_basis == "source_release" for item in batch.events)
    assert all(item.scheduled_at is None for item in batch.events)
    assert all(item.period == "2026-08" for item in batch.events)


async def test_unlisted_current_report_does_not_reuse_old_actual():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=LISTING))
    ) as client:
        batch = await ISMNewswireProvider(
            client, clock=lambda: datetime(2026, 10, 1, 13, tzinfo=UTC)
        ).fetch()
    # 09:00 ET on Oct 1: neither September report is out yet.
    assert batch.status == "healthy"
    assert batch.events == []
    assert batch.issues == []


# ISM's published release calendars (ismworld.org "Release Dates for the ISM
# Manufacturing and Services PMI Reports"): release month -> (Mfg day, Services day).
ISM_CALENDAR = {
    2025: {
        1: (3, 7),
        2: (3, 5),
        3: (3, 5),
        4: (1, 3),
        5: (1, 5),
        6: (2, 4),
        7: (1, 3),
        8: (1, 5),
        9: (2, 4),
        10: (1, 3),
        11: (3, 5),
        12: (1, 3),
    },
    2026: {
        1: (5, 7),
        2: (2, 4),
        3: (2, 4),
        4: (1, 6),
        5: (1, 5),
        6: (1, 3),
        7: (1, 6),
        8: (3, 5),
        9: (1, 3),
        10: (1, 5),
        11: (2, 4),
        12: (1, 3),
    },
}


@pytest.mark.parametrize(
    ("release_year", "release_month"),
    [(year, month) for year, months in ISM_CALENDAR.items() for month in months],
)
def test_expected_release_matches_ism_published_calendar(release_year, release_month):
    report_year, report_month = (
        (release_year - 1, 12) if release_month == 1 else (release_year, release_month - 1)
    )
    manufacturing, services = ISM_CALENDAR[release_year][release_month]
    for family, day in (("manufacturing", manufacturing), ("services", services)):
        due = expected_release(report_month, report_year, family)
        local = due.astimezone(ZoneInfo("America/New_York"))
        assert (local.date(), local.hour, local.minute) == (
            date(release_year, release_month, day),
            10,
            0,
        )


def test_expected_release_skips_ism_january_holiday_and_market_holidays():
    # ISM holiday Fri Jan 2, 2026: December 2025 reports on Jan 5 and Jan 7,
    # not Jan 2 and Jan 6 as a plain NYSE count would give.
    assert expected_release(12, 2025, "manufacturing") == datetime(2026, 1, 5, 15, tzinfo=UTC)
    assert expected_release(12, 2025, "services") == datetime(2026, 1, 7, 15, tzinfo=UTC)
    # Same holiday in 2025 (Thu Jan 2): Jan 3 and Jan 7.
    assert expected_release(12, 2024, "manufacturing") == datetime(2025, 1, 3, 15, tzinfo=UTC)
    assert expected_release(12, 2024, "services") == datetime(2025, 1, 7, 15, tzinfo=UTC)
    # Labor Day Mon Sep 1, 2025 is skipped: Tue Sep 2 and Thu Sep 4.
    assert expected_release(8, 2025, "manufacturing") == datetime(2025, 9, 2, 14, tzinfo=UTC)
    assert expected_release(8, 2025, "services") == datetime(2025, 9, 4, 14, tzinfo=UTC)
    # Good Friday Apr 3, 2026 and the Jul 3, 2026 holiday push Services to Monday.
    assert expected_release(3, 2026, "services") == datetime(2026, 4, 6, 14, tzinfo=UTC)
    assert expected_release(6, 2026, "services") == datetime(2026, 7, 6, 14, tzinfo=UTC)
    # Oct 2026 reports: Mon Nov 2 (Manufacturing) and Wed Nov 4 (Services).
    assert expected_release(10, 2026, "manufacturing") == datetime(2026, 11, 2, 15, tzinfo=UTC)
    assert expected_release(10, 2026, "services") == datetime(2026, 11, 4, 15, tzinfo=UTC)


EMPTY_LISTING = b"<html><h1>News from Institute for Supply Management</h1></html>"


async def _empty_listing_batch(now):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=EMPTY_LISTING))
    ) as client:
        return await ISMNewswireProvider(client, clock=lambda: now).fetch()


@pytest.mark.parametrize(
    ("now", "issues"),
    [
        # Fri Jan 2, 2026 11:00 ET is an ISM holiday: nothing is due yet.
        (datetime(2026, 1, 2, 16, tzinfo=UTC), []),
        # Mon Jan 5 10:59 ET: Manufacturing is due but inside the grace hour.
        (datetime(2026, 1, 5, 15, 59, tzinfo=UTC), []),
        # Mon Jan 5 11:00 ET: Manufacturing is overdue.
        (datetime(2026, 1, 5, 16, tzinfo=UTC), ["Manufacturing"]),
        # Tue Jan 6 11:00 ET: Services (the plain-NYSE 3rd day) is not due until Jan 7.
        (datetime(2026, 1, 6, 16, tzinfo=UTC), ["Manufacturing"]),
        # Wed Jan 7 10:59 ET: Services is still inside its grace hour.
        (datetime(2026, 1, 7, 15, 59, tzinfo=UTC), ["Manufacturing"]),
        # Wed Jan 7 11:00 ET: both overdue.
        (datetime(2026, 1, 7, 16, tzinfo=UTC), ["Manufacturing", "Services"]),
    ],
)
async def test_december_reports_stay_pending_through_ism_january_holiday(now, issues):
    batch = await _empty_listing_batch(now)
    assert batch.events == []
    assert batch.status == ("degraded" if issues else "healthy")
    assert len(batch.issues) == len(issues)
    for family, issue in zip(issues, batch.issues, strict=True):
        assert issue.startswith(f"ISM {family} 2025-12 report not listed after its expected")


@pytest.mark.parametrize(
    ("now", "issues"),
    [
        # Nov 2 07:16 ET: the live case. Both October reports are still to come.
        (datetime(2026, 11, 2, 12, 16, 10, tzinfo=UTC), []),
        # 10:30 ET: Manufacturing is due but inside the listing grace.
        (datetime(2026, 11, 2, 15, 30, tzinfo=UTC), []),
        # 11:30 ET: Manufacturing is overdue; Services is not due until Nov 4.
        (datetime(2026, 11, 2, 16, 30, tzinfo=UTC), ["Manufacturing"]),
        # Nov 5: both overdue.
        (datetime(2026, 11, 5, 15, tzinfo=UTC), ["Manufacturing", "Services"]),
    ],
)
async def test_not_yet_listed_is_pending_until_the_release_is_overdue(now, issues):
    batch = await _empty_listing_batch(now)
    assert batch.events == []
    assert batch.status == ("degraded" if issues else "healthy")
    assert len(batch.issues) == len(issues)
    for family, issue in zip(issues, batch.issues, strict=True):
        assert issue.startswith(f"ISM {family} 2026-10 report not listed after its expected")


SERVICES_HREF = (
    "/news-releases/services-pmi-at-54-6-august-2026-ism-services-pmi-report-123456.html"
)
SERVICES_LISTING = LISTING.replace(RELEASE_HREF.encode(), SERVICES_HREF.encode()).replace(
    b"Manufacturing", b"Services"
)
SERVICES_RELEASE = (
    RELEASE.replace(b"Manufacturing", b"Services")
    .replace(b"Sep 01, 2026", b"Sep 03, 2026")
    .replace(
        b"<table>",
        b"<table><tr><th>Index</th><th>Services PMI</th>"
        b"<th>Manufacturing PMI</th></tr><tr><th>Series Index Aug</th>"
        b"<th>Series Index Jul</th></tr>",
    )
)
FIXTURES = Path(__file__).parents[1] / "fixtures/economics"


def test_saved_official_september_services_release_and_listing():
    listing = (FIXTURES / "ism-listing-2026-10-05.html").read_bytes()
    release = (FIXTURES / "ism-services-2026-09.html").read_bytes()
    url = discover_release(listing, 9, 2026, "services")
    assert url.endswith("services-pmi-report-302898421.html")
    assert parse_release(release, 9, 2026, "services") == (
        datetime(2026, 10, 5, 14, tzinfo=UTC),
        54.9,
        74.0,
    )
    assert discover_release(listing, 8, 2026, "services") != url


@pytest.mark.parametrize(
    "body",
    [
        SERVICES_RELEASE.replace(b'Institute for Supply Management"', b'Wrong issuer"'),
        SERVICES_RELEASE.replace(b"August 2026", b"July 2026"),
        SERVICES_RELEASE.replace(b"Series Index Aug", b"Series Index Jul"),
        SERVICES_RELEASE.replace(b"<td>54.6</td>", b"<td>54.7</td>"),
        SERVICES_RELEASE.replace(b"</table>", b"<tr><td>Prices</td><td>71.1</td></tr></table>"),
        SERVICES_RELEASE.replace(b"10:00 ET", b"09:00 ET"),
        SERVICES_RELEASE.replace(b"Sep 03, 2026", b"Aug 03, 2026"),
        SERVICES_RELEASE.replace(
            b"<th>Services PMI</th><th>Manufacturing PMI</th>",
            b"<th>Manufacturing PMI</th><th>Services PMI</th>",
        ),
    ],
)
def test_services_rejects_wrong_issuer_period_column_ambiguity_or_publication(body):
    with pytest.raises(ValueError):
        parse_release(body, 8, 2026, "services")


async def test_provider_services_failure_preserves_manufacturing_identity_and_actuals():
    combined = LISTING.replace(b"</html>", SERVICES_LISTING + b"</html>")

    def handler(request):
        if request.url.path == "/news/institute-for-supply-management/":
            return httpx.Response(200, content=combined)
        if request.url.path == RELEASE_HREF:
            return httpx.Response(200, content=RELEASE)
        return httpx.Response(200, content=SERVICES_RELEASE.replace(b"August 2026", b"July 2026"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await ISMNewswireProvider(
            client, clock=lambda: datetime(2026, 9, 3, 15, tzinfo=UTC)
        ).fetch()
    assert batch.status == "degraded"
    assert [(e.event_code, e.actual, e.source_event_id) for e in batch.events] == [
        ("ISM_MFG", 54.6, "2026-08:ISM_MFG"),
        ("ISM_MFG_PRICES", 71.1, "2026-08:ISM_MFG_PRICES"),
    ]
    assert "Services" in batch.issues[0]


async def test_provider_collects_both_families_and_isolates_http_rejection():
    listing = LISTING.replace(b"</html>", SERVICES_LISTING + b"</html>")
    fail_mfg = [False]

    def handler(request):
        if request.url.path == "/news/institute-for-supply-management/":
            return httpx.Response(200, content=listing)
        if request.url.path == RELEASE_HREF:
            return httpx.Response(403 if fail_mfg[0] else 200, content=RELEASE)
        assert request.url.path == SERVICES_HREF
        return httpx.Response(200, content=SERVICES_RELEASE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = ISMNewswireProvider(client, clock=lambda: datetime(2026, 9, 3, 15, tzinfo=UTC))
        batch = await provider.fetch()
        assert batch.status == "healthy"
        assert len(batch.events) == 3
        assert [(e.event_code, e.actual) for e in batch.events[-1:]] == [
            ("ISM_SERVICES", 54.6),
        ]
        assert all(e.scheduled_at is None and e.consensus is None for e in batch.events)
        fail_mfg[0] = True
        partial = await provider.fetch()
        assert partial.status == "degraded"
        assert [e.event_code for e in partial.events] == ["ISM_SERVICES"]


async def test_verified_ism_values_emit_macro_period_units_and_content_provenance():
    import hashlib

    def handler(request):
        body = LISTING if request.url.path == "/news/institute-for-supply-management/" else RELEASE
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await ISMNewswireProvider(
            client, clock=lambda: datetime(2026, 9, 1, 15, tzinfo=UTC)
        ).fetch()
    assert batch.checked_series_ids == ["ISM_MFG"]
    observation = batch.observations[0]
    assert (observation.id, observation.period, observation.unit) == (
        "ISM_MFG",
        "2026-08",
        "index points",
    )
    assert observation.value == 54.6
    assert observation.metadata.source_url.endswith(RELEASE_HREF)
    assert observation.metadata.source_revision == hashlib.sha256(RELEASE).hexdigest()
    assert observation.released_at == datetime(2026, 9, 1, 14, tzinfo=UTC)
