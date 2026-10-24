from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from defusedxml.ElementTree import ParseError, fromstring

from core.domain.news import SourceMetadata, SourceStatus, SourceType
from core.providers.news.base import HttpNewsSource, NewsSource, NewsSourceError, content_hash
from core.providers.news.catalog import SOURCE_DEFINITIONS, build_source
from core.providers.news.central_bank import CentralBankFeedSource
from core.providers.news.rss import parse_date, parse_feed

NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>Federal Reserve issues FOMC statement</title>
    <link>https://www.federalreserve.gov/newsevents/pressreleases/monetary20260916a.htm</link>
    <guid>fed-1</guid>
    <pubDate>Wed, 16 Sep 2026 18:00:00 GMT</pubDate>
    <description>Statement text</description>
    <category>Monetary Policy</category>
  </item>
  <item>
    <title>Remarks by Governor Lisa Cook</title>
    <link>https://www.federalreserve.gov/newsevents/speech/cook20260917a.htm</link>
    <pubDate>Thu, 17 Sep 2026 14:00:00 GMT</pubDate>
  </item>
  <item>
    <title>No link or guid item</title>
  </item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>ECB holds rates steady</title>
    <id>tag:ecb,2026:1</id>
    <link rel="alternate" href="https://www.ecb.europa.eu/press/pr/date/2026/html/pr260918.en.html"/>
    <updated>2026-09-18T11:30:00Z</updated>
    <summary>Rate decision</summary>
  </entry>
</feed>"""


def _feed_source(body: bytes, status: int = 200, name: str = "test_feed"):
    metadata = SourceMetadata(name=name, display_name="Test", source_type=SourceType.NEWS)

    async def handler(request):
        return httpx.Response(status, content=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return (
        CentralBankFeedSource(
            metadata=metadata,
            url="https://example.test/feed.xml",
            central_bank="FED",
            client=client,
            clock=lambda: NOW,
        ),
        client,
    )


async def test_rss_parses_and_skips_unidentified_items() -> None:
    source, client = _feed_source(RSS)
    items = await source.fetch()
    assert [item.source_id for item in items] == ["fed-1", items[1].source_id]
    assert items[0].published_at == datetime(2026, 9, 16, 18, tzinfo=UTC)
    assert items[0].central_bank is not None
    assert items[0].central_bank.document_type == "statement"
    assert items[1].central_bank is not None
    assert items[1].central_bank.speaker == "Governor Lisa Cook"
    assert items[1].central_bank.document_type == "speech"
    await client.aclose()


async def test_atom_parsing() -> None:
    metadata = SourceMetadata(name="ecb", display_name="ECB", source_type=SourceType.CENTRAL_BANK)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=ATOM))
    ) as client:
        source = CentralBankFeedSource(
            metadata=metadata,
            url="https://example.test/atom.xml",
            central_bank="ECB",
            client=client,
            clock=lambda: NOW,
        )
        items = await source.fetch()
    assert len(items) == 1
    assert items[0].source_id == "tag:ecb,2026:1"
    assert items[0].source_url is not None


async def test_http_error_is_isolated_and_raises_source_error() -> None:
    source, client = _feed_source(b"", status=503)
    with pytest.raises(NewsSourceError):
        await source.fetch()
    await client.aclose()


async def test_oversized_payload_rejected() -> None:
    source, client = _feed_source(b"x" * 2_000_001)
    with pytest.raises(NewsSourceError):
        await source.fetch()
    await client.aclose()


@pytest.mark.parametrize("body", [b"not xml", b"<html></html>", b"<rss></rss>", b""])
async def test_malformed_documents_rejected(body: bytes) -> None:
    source, client = _feed_source(body)
    with pytest.raises((ValueError, ParseError, NewsSourceError)):
        await source.fetch()
    await client.aclose()


def test_parse_date_handles_formats_and_rejects_garbage() -> None:
    assert parse_date("Wed, 16 Sep 2026 18:00:00 GMT") == datetime(2026, 9, 16, 18, tzinfo=UTC)
    assert parse_date("2026-09-18T11:30:00Z") == datetime(2026, 9, 18, 11, 30, tzinfo=UTC)
    assert parse_date("2026-09-18T11:30:00") == datetime(2026, 9, 18, 11, 30, tzinfo=UTC)
    assert parse_date("not a date") is None
    assert parse_date(None) is None


def test_parse_feed_rejects_too_many_items() -> None:
    from core.providers.news.rss import MAX_ITEMS

    entries = "".join(
        f"<item><title>t{i}</title><guid>g{i}</guid></item>" for i in range(MAX_ITEMS + 1)
    )
    with pytest.raises(ValueError):
        parse_feed(f"<rss><channel>{entries}</channel></rss>".encode())


def test_content_hash_is_stable_and_order_insensitive() -> None:
    assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})
    assert content_hash({"a": 1}) != content_hash({"a": 2})


def test_news_source_health_lifecycle() -> None:
    class Stub(NewsSource):
        @property
        def name(self) -> str:
            return "stub"

        @property
        def metadata(self) -> SourceMetadata:
            return SourceMetadata(name="stub", display_name="Stub", source_type=SourceType.NEWS)

        async def fetch(self):
            return []

    source = Stub(clock=lambda: NOW, enabled=True)
    assert source.health().status is SourceStatus.DEGRADED
    source.record_success(3)
    assert source.health().status is SourceStatus.HEALTHY
    assert source.health().recent_event_count == 3
    source.record_failure("boom")
    assert source.health().status is SourceStatus.DEGRADED
    source.record_failure("boom")
    source.record_failure("boom")
    assert source.health().status is SourceStatus.OFFLINE
    disabled = Stub(clock=lambda: NOW, enabled=False)
    assert disabled.health().status is SourceStatus.DISABLED


def test_abstract_news_source_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        NewsSource()  # type: ignore[abstract]


def test_catalog_definitions_are_unique_and_buildable() -> None:
    names = [definition.name for definition in SOURCE_DEFINITIONS]
    assert len(names) == len(set(names))
    for definition in SOURCE_DEFINITIONS:
        source = build_source(definition, clock=lambda: NOW)
        assert source.name == definition.name
        assert source.metadata.source_type is definition.source_type


def test_http_source_abstract_until_fetch_implemented() -> None:
    with pytest.raises(TypeError):
        HttpNewsSource()  # type: ignore[abstract]


FIXTURES = Path(__file__).parents[1] / "fixtures/news"


def _counting_source(responses: list[httpx.Response], sleeps: list[float] | None = None):
    """A feed source answering ``responses`` in order; records waits instead of sleeping."""
    seen: list[httpx.Request] = []

    async def handler(request):
        seen.append(request)
        return responses[min(len(seen), len(responses)) - 1]

    async def sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    metadata = SourceMetadata(name="feed", display_name="Feed", source_type=SourceType.NEWS)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    source = CentralBankFeedSource(
        metadata=metadata, url="https://example.test/feed.xml", central_bank="FED",
        client=client, clock=lambda: NOW, sleep=sleep,
    )
    return source, client, seen


async def test_rejection_reports_the_http_status() -> None:
    """Oct 23, 2026: the Fed's press_all.xml intermittently answers 404 (21 of 857
    polls in 3 days, never twice running); the status must be visible."""
    source, client, seen = _counting_source([httpx.Response(404)])
    with pytest.raises(NewsSourceError, match=r"HTTP request rejected \(HTTP 404\)"):
        await source.fetch()
    assert len(seen) == 1 and source.backoff_seconds == 0
    await client.aclose()


@pytest.mark.parametrize(
    ("headers", "backoff"),
    [
        ({}, 900.0),
        ({"Retry-After": "120"}, 120.0),
        ({"Retry-After": "Fri, 18 Sep 2026 12:30:00 GMT"}, 1800.0),
        ({"Retry-After": "999999"}, 6 * 3600.0),
        ({"Retry-After": "soon"}, 900.0),
    ],
)
async def test_rate_limit_is_not_retried_and_backs_off(headers, backoff) -> None:
    sleeps: list[float] = []
    source, client, seen = _counting_source([httpx.Response(429, headers=headers)], sleeps)
    with pytest.raises(NewsSourceError, match=r"rate limited \(HTTP 429\)"):
        await source.fetch()
    assert len(seen) == 1 and sleeps == []
    assert source.backoff_seconds == backoff
    # The next successful request clears the back-off.
    source, client2, _ = _counting_source([httpx.Response(200, content=RSS)])
    await source.fetch()
    assert source.backoff_seconds == 0
    await client.aclose()
    await client2.aclose()


async def test_server_error_is_retried_once_and_long_retry_after_backs_off() -> None:
    sleeps: list[float] = []
    source, client, seen = _counting_source(
        [httpx.Response(503), httpx.Response(200, content=RSS)], sleeps
    )
    assert len(await source.fetch()) == 2 and len(seen) == 2 and sleeps == [1.0]
    source, client2, seen = _counting_source(
        [httpx.Response(503, headers={"Retry-After": "600"})], sleeps
    )
    with pytest.raises(NewsSourceError, match=r"transient failure \(HTTP 503\)"):
        await source.fetch()
    assert len(seen) == 1 and source.backoff_seconds == 600.0
    await client.aclose()
    await client2.aclose()


def _boj_source(body: bytes):
    from core.providers.news.central_bank import BoJWhatsNewSource

    definition = next(d for d in SOURCE_DEFINITIONS if d.name == "boj_whatsnew")
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=body, headers={"Content-Type": "text/html;charset=UTF-8"}
            )
        )
    )
    source = BoJWhatsNewSource(
        metadata=definition.metadata(), url=definition.url, client=client, clock=lambda: NOW
    )
    return source, client


async def test_boj_real_feed_parses() -> None:
    source, client = _boj_source((FIXTURES / "boj-whatsnew-2026-10-23.xml").read_bytes())
    items = await source.fetch()
    assert len(items) == 50
    assert items[0].headline == "Japanese Government Bonds Held by the Bank of Japan"
    assert items[0].published_at == datetime(2026, 10, 22, 8, tzinfo=UTC)
    await client.aclose()


async def test_boj_html_page_is_a_clean_temporary_failure() -> None:
    """Oct 19-23, 2026: boj_whatsnew failed 9 times with "not well-formed (invalid
    token): line 10, column 69" on HTTP 200. BOJ's HTML template has
    ``<link ... crossorigin>`` (column 69), so the server was sending a web page,
    not RSS. The fixture is a synthetic page with the same shape."""
    body = (FIXTURES / "boj-html-page-2026-10-23.html").read_bytes()
    with pytest.raises(ParseError, match="column 69"):  # what the old parser saw
        fromstring(body)
    source, client = _boj_source(body)
    with pytest.raises(ValueError, match=r"web page \(HTML\) instead of the feed"):
        await source.fetch()
    await client.aclose()


def test_broken_feed_xml_is_a_clean_error() -> None:
    with pytest.raises(ValueError, match=r"feed XML not well-formed \(.*line 1"):
        parse_feed(b"<rss><channel><item><title>A & B</title></item></channel></rss>")


def test_hostile_entities_still_rejected() -> None:
    from defusedxml import DTDForbidden, EntitiesForbidden

    bomb = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "x">]><rss>&a;</rss>'
    with pytest.raises((EntitiesForbidden, DTDForbidden)):
        parse_feed(bomb)
