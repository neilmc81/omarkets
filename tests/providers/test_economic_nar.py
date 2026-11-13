from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from core.providers.economic_base import ReleasePending
from core.providers.economic_nar import NARReleasesProvider, parse_release, release_url

NOW = datetime(2026, 10, 13, 15, tzinfo=UTC)
URL = 'https://www.nar.realtor/newsroom/existing-home-sales-test'


def release(**changes):
    data = dict(url=URL, stamp='2026-10-13T09:01:00-05:00',
                description='Existing-home sales increased to a seasonally adjusted '
                'annual rate of 3.99 million in September 2026.',
                text='WASHINGTON (October 13, 2026) – Existing-home sales increased '
                'in September 2026. National Snapshot Total Existing-Home Sales for September 2026 '
                'Existing-home sales decreased year-over-year to a seasonally adjusted annual '
                'rate of 3.99 million. Inventory in September '
                'Regional annual rate of 9.99 million.')
    data.update(changes)
    return (f'<meta property="og:url" content="{data["url"]}">'
            f'<meta property="article:published_time" content="{data["stamp"]}">'
            f'<meta property="og:description" content="{data["description"]}">'
            f'<p>{data["text"]}</p>').encode()


def test_national_rate_and_source_publication():
    event = parse_release(release(), URL, NOW)
    assert event.actual == 3_990_000
    assert event.period == '2026-09'
    assert event.released_at == datetime(2026, 10, 13, 14, 1, tzinfo=UTC)
    assert event.scheduled_at is None
    assert event.consensus is None
    assert event.actual_basis == 'source_release'


@pytest.mark.parametrize('changes', [dict(url='https://evil.example/'),
    dict(stamp='2026-10-14T14:00:00Z'), dict(stamp='2026-10-13T14:00:00'),
    dict(description='Regional seasonally adjusted annual rate of 9.99 million'),
    dict(text='No national period available')])
def test_rejects_unverified_release(changes):
    with pytest.raises(ValueError):
        parse_release(release(**changes), URL, NOW)


def test_listing_rejects_external_and_ambiguous_links():
    good = f'<a href="{URL}">Read the full news release</a>'.encode()
    assert release_url(good) == URL
    for bad in [good + good.replace(b'-test', b'-other'),
                good.replace(b'www.nar.realtor', b'evil.example')]:
        with pytest.raises(ValueError):
            release_url(bad)


@pytest.mark.parametrize('changes', [
    dict(description='Existing-home sales in the Northeast reached a seasonally adjusted '
         'annual rate of 9.99 million in September 2026.'),
    dict(description='Existing-home sales reached a seasonally adjusted annual rate of '
         '4.99 million in September 2026.'),
    dict(description='Existing-home sales reached a seasonally adjusted annual rate of '
         '3.99 million in August 2026.'),
])
def test_rejects_region_rate_and_metadata_disagreement(changes):
    with pytest.raises(ValueError):
        parse_release(release(**changes), URL, NOW)


@pytest.mark.parametrize('period', ['September 2027', 'October 2026'])
def test_rejects_future_or_same_month_reference_period(period):
    body = release().replace(b'September 2026', period.encode())
    with pytest.raises(ValueError):
        parse_release(body, URL, NOW)


def test_unrelated_later_period_cannot_supply_national_period():
    body = release().replace(b'Total Existing-Home Sales for September 2026',
                             b'Total Existing-Home Sales for period unknown')
    with pytest.raises(ValueError):
        parse_release(body, URL, NOW)


def test_regional_or_subtype_section_cannot_supply_national_rate():
    body = release().replace(b'Existing-home sales decreased year-over-year',
                             b'Existing-home sales in the Northeast decreased year-over-year')
    with pytest.raises(ValueError):
        parse_release(body, URL, NOW)


def test_regional_summary_after_national_metadata_does_not_replace_actual():
    event = parse_release(release(description='Existing-home sales increased to a '
        'seasonally adjusted annual rate of 3.99 million in September 2026. '
        'Sales gains in the South and West were offset by regional declines.'), URL, NOW)
    assert event.actual == 3_990_000


def test_subtype_rate_in_national_section_is_rejected():
    body = release().replace(b'Existing-home sales decreased year-over-year',
                             b'Existing-home sales for single-family homes '
                             b'decreased year-over-year')
    with pytest.raises(ValueError):
        parse_release(body, URL, NOW)


async def test_release_stamped_later_today_is_pending_not_feed_down():
    early = datetime(2026, 10, 13, 13, 59, tzinfo=UTC)  # release stamped 14:01 UTC
    with pytest.raises(ReleasePending):
        parse_release(release(), URL, early)
    listing = f'<a href="{URL}">Read the full news release</a>'.encode()

    def handler(request):
        return httpx.Response(200, content=release() if str(request.url) == URL else listing)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await NARReleasesProvider(client, clock=lambda: early).fetch()
    assert batch.status == "healthy" and batch.events == [] and batch.issues == []


FIXTURES = Path(__file__).parents[1] / 'fixtures' / 'economics'
OCT_URL = 'https://www.nar.realtor/newsroom/existing-home-sales-decreased-5-5-percent-october-nar-report'
OCT_NOW = datetime(2026, 11, 12, 16, tzinfo=UTC)


def test_october_2026_listing_and_release_fixtures_parse():
    listing = (FIXTURES / 'nar-listing-2026-11-12.html').read_bytes()
    assert release_url(listing) == OCT_URL
    event = parse_release((FIXTURES / 'nar-release-2026-11-12.html').read_bytes(), OCT_URL, OCT_NOW)
    assert event.event_code == 'EXISTING_HOME_SALES'
    assert event.period == '2026-10'
    assert event.actual == 3_750_000


def test_october_2026_regional_or_subtype_rate_cannot_be_national_actual():
    page = (FIXTURES / 'nar-release-2026-11-12.html').read_bytes()
    national = b'year-over-year to a seasonally adjusted annual rate of 3.75 million'
    assert national in page
    for lead in (b'Existing-home sales in the South decreased 8.8%',
                 b'Existing-home sales for single-family homes decreased 8.8%'):
        body = page.replace(b'Existing-home sales decreased 8.8% year-over-year to a seasonally '
                            b'adjusted annual rate of 3.75 million',
                            lead + b' year-over-year to a seasonally adjusted annual rate '
                            b'of 3.75 million')
        assert body != page
        with pytest.raises(ValueError):
            parse_release(body, OCT_URL, OCT_NOW)


async def test_unreadable_release_page_is_reported_as_parse_failure():
    listing = f'<a href="{URL}">Read the full news release</a>'.encode()

    def handler(request):
        return httpx.Response(200, content=b'<html>new layout</html>' if str(request.url) == URL
                              else listing)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await NARReleasesProvider(client, clock=lambda: NOW).fetch()
    assert batch.status == 'unavailable'
    assert 'could not be read' in batch.issues[0]
    assert 'suppressed' not in batch.issues[0]
