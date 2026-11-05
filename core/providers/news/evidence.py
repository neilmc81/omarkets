"""Market coverage fetched when a move or ask note is written, not polled.

The stored catalog (``catalog.py``) has wires and official sources but little that
says why a market moved today. These feeds do: a Google News query, Yahoo Finance
ticker feeds and investingLive, chosen per instrument. They are read only when a
note needs them, so they don't fill the News page or the event store; each note's
stored facts keep what it was given.

Article pages are read only from :data:`ARTICLE_HOSTS` over https, without following
redirects. A failed feed or article is reported or skipped, never fatal to a note.
All of this text is untrusted: it reaches the writer as data.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time as _time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlencode, urlsplit

import httpx

from core.domain.news import ExternalReport, ReportBatch
from core.providers.news.rss import ParsedFeedItem, parse_feed

logger = logging.getLogger(__name__)

GOOGLE_NEWS = "Google News"
YAHOO_FINANCE = "Yahoo Finance"
INVESTINGLIVE = "investingLive"

#: The same feed is read at most this often; a large move can trigger several notes.
FEED_TTL_SECONDS = 120.0
#: After a refusal the feed is left alone for this long.
REFUSED_SECONDS = 900.0
ARTICLE_TTL_SECONDS = 1800.0
REQUEST_TIMEOUT = 8.0
FEED_MAX_BYTES = 2_000_000
ARTICLE_MAX_BYTES = 3_000_000
#: Text kept from one article page; the desk picks its excerpt from this.
ARTICLE_MAX_CHARS = 20_000
_ARTICLE_CACHE_SIZE = 64
#: Pages whose article text sits in plain ``<p>`` tags. Google News links are
#: redirects to many sites, so its items stay headline-only.
ARTICLE_HOSTS = frozenset({"www.cnbc.com", "finance.yahoo.com", "investinglive.com"})

INVESTINGLIVE_URL = "https://investinglive.com/feed/"


@dataclass(frozen=True)
class Coverage:
    """What to read for one instrument: Google News queries and Yahoo tickers."""

    queries: tuple[str, ...]
    tickers: tuple[str, ...]


_STOCKS_QUERY = '("stock market" OR "Wall Street" OR "stocks today")'
_MEGACAPS = "NVDA,MSFT,AAPL,AMZN,META,GOOGL,AVGO,TSLA"

#: Every instrument the desk writes for has its own coverage.
COVERAGE: Mapping[str, Coverage] = {
    "NQ": Coverage(('(Nasdaq OR "tech stocks" OR "chip stocks") stocks', _STOCKS_QUERY),
                   ("^IXIC,QQQ,SMH", _MEGACAPS)),
    "ES": Coverage(('("S&P 500" OR "stock futures")', _STOCKS_QUERY), ("^GSPC,SPY",)),
    "YM": Coverage(('("Dow Jones" OR "Dow futures" OR "the Dow")', _STOCKS_QUERY), ("^DJI,DIA",)),
    "GOLD": Coverage(('("gold price" OR "gold prices" OR bullion)',), ("GC=F,GLD",)),
    "WTI": Coverage(('("oil prices" OR "crude oil" OR WTI OR Brent)',), ("CL=F,BZ=F,USO",)),
    "DXY": Coverage(('("dollar index" OR "US dollar" OR DXY)',), ("DX-Y.NYB,UUP",)),
    "US2Y": Coverage(('("Treasury yields" OR "2-year yield" OR "two-year yield")',),
                     ("^IRX,SHY",)),
    "US10Y": Coverage(('("Treasury yields" OR "10-year yield" OR "bond market")',),
                      ("^TNX,TLT",)),
    "VIX": Coverage(('(VIX OR "volatility index" OR "fear gauge")', _STOCKS_QUERY),
                    ("^VIX,VXX", "^GSPC,SPY")),
}


def google_news_url(query: str) -> str:
    """A Google News search limited to the last day."""
    params = {"q": f"{query} when:1d", "hl": "en-US", "gl": "US", "ceid": "US:en"}
    return f"https://news.google.com/rss/search?{urlencode(params)}"


def yahoo_url(tickers: str) -> str:
    params = {"s": tickers, "region": "US", "lang": "en-US"}
    return f"https://feeds.finance.yahoo.com/rss/2.0/headline?{urlencode(params)}"


class _TextParser(HTMLParser):
    """Text of ``<p>``/``<li>`` tags (or all text, for a feed summary)."""

    SKIP = frozenset({"script", "style", "noscript", "svg", "nav", "footer", "aside", "form"})
    BLOCKS = frozenset({"p", "li"})

    def __init__(self, *, blocks_only: bool) -> None:
        super().__init__(convert_charrefs=True)
        self._blocks_only = blocks_only
        self._skip = 0
        self._depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCKS:
            self._depth += 1
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self.BLOCKS:
            self._depth = max(0, self._depth - 1)
            self.parts.append(" ")

    def handle_data(self, data):
        if not self._skip and (self._depth or not self._blocks_only):
            self.parts.append(data)

    def text(self) -> str:
        return _clean("".join(self.parts))


_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean(text: str) -> str:
    return " ".join(_CONTROL.sub(" ", text).split())


def plain_text(markup: str | None) -> str | None:
    """A feed summary without its HTML; ``None`` when nothing is left."""
    if not markup:
        return None
    parser = _TextParser(blocks_only=False)
    parser.feed(markup)
    return parser.text() or None


def article_paragraphs(page: str) -> str | None:
    """The paragraph text of an article page; ``None`` when there is none."""
    parser = _TextParser(blocks_only=True)
    parser.feed(page)
    return parser.text()[:ARTICLE_MAX_CHARS] or None


def article_allowed(url: str | None) -> bool:
    if not url:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme == "https" and parts.hostname in ARTICLE_HOSTS and parts.port is None


def _report(item: ParsedFeedItem, feed: str) -> ExternalReport | None:
    """One feed item as a report; ``None`` without a publication time (which
    can't show the report came before the note)."""
    if item.published_at is None:
        return None
    title, source, summary = item.title, feed, plain_text(item.summary)
    if feed == GOOGLE_NEWS:
        # "Title - Publisher"; its description only repeats the title as a link.
        head, _, publisher = title.rpartition(" - ")
        if head and publisher:
            title, source = head, publisher
        summary = None
    if not title.strip():
        return None
    return ExternalReport(source=_clean(source), feed=feed, headline=_clean(title),
                          published_at=item.published_at, url=item.link, summary=summary)


class EvidenceFeeds:
    """Reads an instrument's coverage on demand, with short caches and backoff."""

    def __init__(self, *, client: httpx.AsyncClient | None = None,
                 clock: Callable[[], float] = _time.monotonic) -> None:
        self._client = client
        self._clock = clock
        self._feeds: dict[str, tuple[float, list[ParsedFeedItem]]] = {}
        self._refused_until: dict[str, float] = {}
        self._articles: dict[str, tuple[float, str | None]] = {}

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT, follow_redirects=False,
                limits=httpx.Limits(max_connections=6),
                headers={"User-Agent":
                         "Mozilla/5.0 (X11; Linux x86_64) Omarkets/0.1 (+local workstation)"})
        return self._client

    async def _get(self, url: str, max_bytes: int) -> bytes:
        """The body of a 200 answer; anything else raises."""
        async with asyncio.timeout(REQUEST_TIMEOUT + 2):
            async with self._http().stream("GET", url) as response:
                if response.status_code != 200:
                    raise httpx.HTTPStatusError(f"HTTP {response.status_code}",
                                                request=response.request, response=response)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise ValueError("response too large")
                return bytes(body)

    async def _feed(self, url: str) -> list[ParsedFeedItem]:
        """A feed's items, from the cache when fresh. Raises when it can't be read."""
        now = self._clock()
        cached = self._feeds.get(url)
        if cached is not None and now - cached[0] < FEED_TTL_SECONDS:
            return cached[1]
        if self._refused_until.get(url, 0.0) > now:
            raise RuntimeError("waiting after a refusal")
        try:
            items = parse_feed(await self._get(url, FEED_MAX_BYTES))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403, 429):
                self._refused_until[url] = now + REFUSED_SECONDS
            raise
        self._feeds[url] = (now, items)
        return items

    async def reports(self, symbol: str) -> ReportBatch:
        """Reports for ``symbol``; names the feeds that could not be read."""
        coverage = COVERAGE.get(symbol)
        if coverage is None:
            return ReportBatch()
        wanted = [(google_news_url(q), GOOGLE_NEWS) for q in coverage.queries]
        wanted += [(yahoo_url(t), YAHOO_FINANCE) for t in coverage.tickers]
        wanted.append((INVESTINGLIVE_URL, INVESTINGLIVE))
        results = await asyncio.gather(*(self._feed(url) for url, _ in wanted),
                                       return_exceptions=True)
        reports: list[ExternalReport] = []
        failed: set[str] = set()
        for (_, feed), result in zip(wanted, results, strict=True):
            if isinstance(result, BaseException):
                if not isinstance(result, Exception):
                    raise result
                logger.info("evidence feed %s failed: %s", feed, type(result).__name__)
                failed.add(feed)
                continue
            reports += [r for r in (_report(item, feed) for item in result) if r is not None]
        return ReportBatch(reports=reports, unavailable=sorted(failed))

    async def article_text(self, url: str) -> str | None:
        """An allowed article page's paragraph text; ``None`` if it can't be read."""
        if not article_allowed(url):
            return None
        now = self._clock()
        cached = self._articles.get(url)
        if cached is not None and now - cached[0] < ARTICLE_TTL_SECONDS:
            return cached[1]
        try:
            body = await self._get(url, ARTICLE_MAX_BYTES)
            text = article_paragraphs(body.decode("utf-8", errors="replace"))
        except (httpx.HTTPError, TimeoutError, ValueError) as exc:
            logger.info("article unavailable (%s): %s", type(exc).__name__, urlsplit(url).hostname)
            text = None
        if len(self._articles) >= _ARTICLE_CACHE_SIZE:
            self._articles.pop(next(iter(self._articles)))
        self._articles[url] = (now, text)
        return text


__all__ = ["ARTICLE_HOSTS", "COVERAGE", "Coverage", "EvidenceFeeds", "article_allowed",
           "article_paragraphs", "google_news_url", "plain_text", "yahoo_url"]
