from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from core.providers.economic_ism_newswire import (
    ISMNewswireProvider,
    discover_release,
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
    assert batch.status == "degraded"
    assert "Services" in batch.issues[0]
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
    assert batch.status == "degraded"
    assert batch.events == []
    assert "2026-09" in batch.issues[0]


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
        "ISM_MFG", "2026-08", "index points")
    assert observation.value == 54.6
    assert observation.metadata.source_url.endswith(RELEASE_HREF)
    assert observation.metadata.source_revision == hashlib.sha256(RELEASE).hexdigest()
    assert observation.released_at == datetime(2026, 9, 1, 14, tzinfo=UTC)
