from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from core.providers.economic_releases import FED_RSS_URL, FedReleasesProvider

PUBLISHED = datetime(2025, 9, 17, 18, tzinfo=UTC)
URL = "https://www.federalreserve.gov/newsevents/pressreleases/monetary20250917a.htm"
FIXTURE = Path(__file__).parents[1] / "fixtures/economics/fomc-2025-09-17.html"


def rss(link=URL):
    return f"""<rss><channel><item><title>Federal Reserve issues FOMC statement</title>
    <link>{link}</link><guid>statement-20250917</guid>
    <pubDate>Wed, 17 Sep 2025 18:00:00 GMT</pubDate></item></channel></rss>"""


async def test_official_saved_statement_provenance_and_cached_retrieval():
    calls = []
    now = PUBLISHED + timedelta(minutes=1)

    def transport(request):
        calls.append(str(request.url))
        return (
            httpx.Response(200, text=rss())
            if str(request.url) == FED_RSS_URL
            else (httpx.Response(200, content=FIXTURE.read_bytes()))
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = FedReleasesProvider(client, clock=lambda: now)
        batch = await provider.fetch()
        assert batch.status == "healthy"
        event = batch.events[0]
        statement = event.statement
        assert statement is not None
        assert statement.published_at == PUBLISHED and statement.received_at == now
        assert statement.source_url == URL
        assert "decided to lower the target range" in statement.text
        assert "Voting against this action was Stephen I. Miran" in statement.text
        assert "For media inquiries" not in statement.text
        assert "Implementation Note" not in statement.text
        assert "Board of Governors" not in statement.text
        assert event.actual == 4.25 and event.unit == "percent"  # 4 to 4-1/4 percent
        assert event.consensus is None
        now += timedelta(minutes=1)
        again = await provider.fetch()
        assert again.events[0].statement == statement
        assert calls.count(URL) == 1


@pytest.mark.parametrize(
    "link",
    [
        URL + "?redirect=other",
        URL.replace("a.htm", "a1.htm"),
        URL.replace("20250917", "20250918"),
    ],
)
async def test_unqualified_statement_url_not_fetched(link):
    calls = []

    def transport(request):
        calls.append(str(request.url))
        return httpx.Response(200, text=rss(link))

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        batch = await FedReleasesProvider(client, clock=lambda: PUBLISHED).fetch()
    assert calls == [FED_RSS_URL]
    assert batch.status == "degraded" and batch.events[0].statement is None


async def test_historical_notice_not_fetched():
    calls = []

    def transport(request):
        calls.append(str(request.url))
        return httpx.Response(200, text=rss())

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        batch = await FedReleasesProvider(
            client, clock=lambda: PUBLISHED + timedelta(days=10, seconds=1)
        ).fetch()
    assert calls == [FED_RSS_URL]
    assert batch.events[0].statement is None
    assert batch.events[0].actual is None


async def test_rate_read_once_from_a_recent_statement_after_a_restart():
    calls = []

    def transport(request):
        calls.append(str(request.url))
        if str(request.url) == FED_RSS_URL:
            return httpx.Response(200, text=rss())
        return httpx.Response(200, content=FIXTURE.read_bytes())

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = FedReleasesProvider(client, clock=lambda: PUBLISHED + timedelta(days=2))
        first, second = await provider.fetch(), await provider.fetch()
    assert calls.count(URL) == 1
    assert first.events[0].statement is None
    assert first.events[0].actual == second.events[0].actual == 4.25


@pytest.mark.parametrize(
    "status,body",
    [
        (302, b""),
        (200, b"<html><p>The Committee text outside article</p></html>"),
    ],
)
async def test_redirect_or_missing_article_keeps_notice(status, body):
    calls = []

    def transport(request):
        calls.append(str(request.url))
        if str(request.url) == FED_RSS_URL:
            return httpx.Response(200, text=rss())
        return httpx.Response(status, content=body, headers={"Location": "https://example.com"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        batch = await FedReleasesProvider(client, clock=lambda: PUBLISHED).fetch()
    assert calls == [FED_RSS_URL, URL]
    assert batch.status == "degraded"
    assert batch.events[0].statement is None
    assert batch.events[0].status == "released"


@pytest.mark.parametrize(
    "url",
    [
        URL.replace("www.federalreserve.gov", "example.com"),
        URL.replace("https:", "http:"),
        URL.replace("www.federalreserve.gov", "user@www.federalreserve.gov"),
        URL.replace("www.federalreserve.gov", "www.federalreserve.gov:443"),
        URL + "?foo=1",
        URL + "#text",
        URL.replace("20250917", "20250918"),
    ],
)
def test_imported_statement_rejects_nonofficial_or_wrong_date(url):
    import hashlib

    from pydantic import ValidationError

    from core.domain.economics import EconomicStatement

    text = "The Committee published a policy statement. " * 5
    with pytest.raises(ValidationError):
        EconomicStatement(
            text=text,
            source_url=url,
            published_at=PUBLISHED,
            received_at=PUBLISHED,
            content_sha256=hashlib.sha256(text.encode()).hexdigest(),
        )


async def test_expired_statement_cache_revalidates_corrections_and_preserves_unchanged():
    now = PUBLISHED + timedelta(minutes=1)
    body = FIXTURE.read_bytes()
    calls = []

    def transport(request):
        calls.append(str(request.url))
        if str(request.url) == FED_RSS_URL:
            return httpx.Response(200, text=rss())
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = FedReleasesProvider(client, clock=lambda: now)
        initial = (await provider.fetch()).events[0].statement
        now += timedelta(minutes=5)
        unchanged = (await provider.fetch()).events[0].statement
        assert unchanged == initial
        assert unchanged.received_at == PUBLISHED + timedelta(minutes=1)
        now += timedelta(minutes=1)
        assert (await provider.fetch()).events[0].statement == initial
        assert calls.count(URL) == 2
        body = body.replace(b"Job gains have slowed", b"Job gains have moderated")
        now += timedelta(minutes=4)
        corrected = (await provider.fetch()).events[0].statement
        assert corrected is not None and initial is not None
        assert corrected.content_sha256 != initial.content_sha256
        assert corrected.received_at == now
        assert "Job gains have moderated" in corrected.text
        assert corrected.published_at == initial.published_at
        assert calls.count(URL) == 3
