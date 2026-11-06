"""Coverage read when a note is written: parsing, failures, limits and safety."""

import httpx
import pytest

from core.providers.news import evidence as FE

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{}</channel></rss>"""


def item(title, *, link="https://example.com/a", date="Thu, 05 Nov 2026 14:43:20 GMT",
         description=None):
    parts = [f"<title>{title}</title>", f"<link>{link}</link>", f"<guid>{title}</guid>"]
    if date:
        parts.append(f"<pubDate>{date}</pubDate>")
    if description:
        parts.append(f"<description><![CDATA[{description}]]></description>")
    return f"<item>{''.join(parts)}</item>"


def feeds(handler, clock=None):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return FE.EvidenceFeeds(client=client, **({"clock": clock} if clock else {}))


def host_routes(routes, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(str(request.url))
        return routes(request)
    return handler


def ok_feeds(request):
    host = request.url.host
    if host == "news.google.com":
        return httpx.Response(200, text=RSS.format(item(
            "Nasdaq falls at open - Reuters", description="<a href='x'>Nasdaq falls</a>")))
    if host == "feeds.finance.yahoo.com":
        return httpx.Response(200, text=RSS.format(item(
            "Chips drop", description="Stocks <b>fell</b> &amp; chips too.")
            + item("No date", date=None)))
    return httpx.Response(200, text=RSS.format(item("Fed's Barkin speaks")))


def test_urls_limit_google_to_the_last_day():
    assert "when%3A1d" in FE.google_news_url('"gold price"')
    assert "s=GC%3DF" in FE.yahoo_url("GC=F")


async def test_reports_name_the_publisher_and_drop_items_without_a_time():
    batch = await feeds(ok_feeds).reports("GOLD")
    assert batch.unavailable == []
    by_title = {r.headline: r for r in batch.reports}
    assert set(by_title) == {"Nasdaq falls at open", "Chips drop", "Fed's Barkin speaks"}
    google = by_title["Nasdaq falls at open"]
    assert (google.source, google.feed, google.summary) == ("Reuters", FE.GOOGLE_NEWS, None)
    assert google.time_approximate and not by_title["Chips drop"].time_approximate
    assert by_title["Chips drop"].source == "example.com"  # who published it, not the feed
    assert by_title["Chips drop"].summary == "Stocks fell & chips too."
    assert by_title["Fed's Barkin speaks"].source == FE.INVESTINGLIVE


async def test_a_broken_feed_is_named_and_the_others_still_count():
    def handler(request):
        if request.url.host == "news.google.com":
            return httpx.Response(503)
        if request.url.host == "feeds.finance.yahoo.com":
            return httpx.Response(200, text="<html>not a feed</html>")
        return ok_feeds(request)
    batch = await feeds(handler).reports("WTI")
    assert batch.unavailable == [FE.GOOGLE_NEWS, FE.YAHOO_FINANCE]
    assert [r.source for r in batch.reports] == [FE.INVESTINGLIVE]


async def test_network_failure_is_named_too():
    def handler(request):
        raise httpx.ConnectError("down")
    batch = await feeds(handler).reports("DXY")
    assert batch.reports == []
    assert set(batch.unavailable) == {FE.GOOGLE_NEWS, FE.INVESTINGLIVE, FE.YAHOO_FINANCE}


async def test_an_instrument_without_coverage_reads_nothing():
    seen = []
    batch = await feeds(host_routes(ok_feeds, seen)).reports("BTC")
    assert (batch.reports, batch.unavailable, seen) == ([], [], [])


async def test_a_feed_is_cached_briefly_across_notes():
    seen, now = [], {"t": 0.0}
    reader = feeds(host_routes(ok_feeds, seen), clock=lambda: now["t"])
    await reader.reports("NQ")
    first = len(seen)
    await reader.reports("ES")  # shares investingLive and the stocks query with NQ
    assert len(seen) < first * 2
    still = len(seen)
    await reader.reports("NQ")
    assert len(seen) == still
    now["t"] = FE.FEED_TTL_SECONDS + 1
    await reader.reports("NQ")
    assert len(seen) == still + first


@pytest.mark.parametrize("status", [403, 429])
async def test_a_refusal_leaves_the_feed_alone_for_a_while(status):
    seen, now = [], {"t": 0.0}

    def routes(request):
        return httpx.Response(status)
    reader = feeds(host_routes(routes, seen), clock=lambda: now["t"])
    await reader.reports("GOLD")
    tried = len(seen)
    now["t"] = FE.FEED_TTL_SECONDS + 1
    batch = await reader.reports("GOLD")
    assert len(seen) == tried and batch.unavailable
    now["t"] = FE.REFUSED_SECONDS + 1
    await reader.reports("GOLD")
    assert len(seen) == tried * 2


@pytest.mark.parametrize(("url", "allowed"), [
    ("https://www.cnbc.com/2026/11/05/a.html", True),
    ("https://finance.yahoo.com/markets/a.html", True),
    ("https://investinglive.com/a/", True),
    ("http://www.cnbc.com/a.html", False),
    ("https://www.cnbc.com:8443/a.html", False),
    ("https://cnbc.com.evil.example/a.html", False),
    ("https://127.0.0.1/a", False),
    ("https://localhost/a", False),
    ("https://news.google.com/rss/articles/x", False),
    ("not a url", False),
    (None, False),
])
def test_only_named_hosts_over_https_are_read(url, allowed):
    assert FE.article_allowed(url) is allowed


async def test_an_article_outside_the_list_is_never_requested():
    seen = []
    reader = feeds(host_routes(lambda r: httpx.Response(200, text="<p>x</p>"), seen))
    assert await reader.article_text("https://127.0.0.1/admin") is None
    assert seen == []


async def test_article_text_is_paragraphs_only_and_cached():
    page = ("<html><head><script>var x='<p>code</p>';</script></head><body><nav><p>Menu</p></nav>"
            "<h1>Title</h1><p>Chips fell &amp; so did <b>Nvidia</b>.</p><ul><li>One point</li></ul>"
            "<footer><p>Copyright</p></footer></body></html>")
    seen = []
    reader = feeds(host_routes(lambda r: httpx.Response(200, text=page), seen))
    url = "https://www.cnbc.com/a.html"
    assert await reader.article_text(url) == "Chips fell & so did Nvidia. One point"
    assert await reader.article_text(url) == "Chips fell & so did Nvidia. One point"
    assert len(seen) == 1


@pytest.mark.parametrize("response", [
    httpx.Response(302, headers={"Location": "https://127.0.0.1/admin"}),
    httpx.Response(404),
    httpx.Response(200, content=b"x" * (FE.ARTICLE_MAX_BYTES + 1)),
    httpx.Response(200, text="<html><body><div>no paragraphs</div></body></html>"),
])
async def test_an_article_that_cant_be_read_is_none(response):
    seen = []
    reader = feeds(host_routes(lambda r: response, seen))
    assert await reader.article_text("https://www.cnbc.com/a.html") is None
    assert len(seen) == 1  # a redirect is not followed


def test_plain_text_strips_markup_and_control_characters():
    assert FE.plain_text("<ul><li>Policy\x07 is</li><li>neutral</li></ul>") == "Policy is neutral"
    assert FE.plain_text("<img src='x'>") is None
    assert FE.plain_text(None) is None


@pytest.mark.parametrize(("link", "publisher"), [
    ("https://www.cbsnews.com/news/stocks", "CBS News"),
    ("https://finance.yahoo.com/markets/a.html", FE.YAHOO_FINANCE),
    ("https://www.someblog.example/a", "someblog.example"),
    ("not a link", FE.YAHOO_FINANCE),
])
async def test_a_yahoo_item_is_credited_to_its_publisher(link, publisher):
    def handler(request):
        if request.url.host == "feeds.finance.yahoo.com":
            return httpx.Response(200, text=RSS.format(item("Chips drop", link=link)))
        return httpx.Response(200, text=RSS.format(""))
    batch = await feeds(handler).reports("GOLD")
    assert [r.source for r in batch.reports] == [publisher]


async def test_a_publisher_given_as_a_web_address_is_tidied():
    def handler(request):
        if request.url.host == "news.google.com":
            return httpx.Response(200, text=RSS.format(item(
                "Dollar drops - https://www.businesstimes-bd.com/")))
        return httpx.Response(200, text=RSS.format(""))
    batch = await feeds(handler).reports("DXY")
    assert [r.source for r in batch.reports] == ["businesstimes-bd.com"]


async def test_a_publisher_that_cleans_to_nothing_doesnt_lose_the_feed():
    def handler(request):
        if request.url.host == "news.google.com":
            return httpx.Response(200, text=RSS.format(
                item("Dollar drops - https://www./") + item("Dollar rises - Reuters")))
        return httpx.Response(200, text=RSS.format(""))
    batch = await feeds(handler).reports("DXY")
    assert [r.source for r in batch.reports] == [FE.GOOGLE_NEWS, "Reuters"]
