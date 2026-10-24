from datetime import UTC, datetime

import httpx

from core.providers.economic_bea_releases import BEAReleasesProvider

NOW = datetime(2026, 9, 17, 15, tzinfo=UTC)


def rss(items: str) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>BEA</title>
{items}
</channel></rss>"""


def item(title, link, guid=None, pubdate="Wed, 26 Aug 2026 08:30:00 EDT", vintage=None):
    vintage_tag = f"<vintage>{vintage}</vintage>" if vintage else ""
    guid_tag = f"<guid>{guid or link}</guid>"
    return (
        f"<item><title>{title}</title><link>{link}</link>{guid_tag}"
        f"{vintage_tag}<pubDate>{pubdate}</pubDate>"
        f"<description>prose only</description></item>"
    )


async def fetch(body: str):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body))
    ) as client:
        return await BEAReleasesProvider(client, clock=lambda: NOW).fetch()


async def test_classifies_gdp_pce_and_other():
    body = rss(
        item(
            "GDP (Second Estimate) and Corporate Profits, 2nd Quarter 2026",
            "https://www.bea.gov/news/2026/gdp",
            vintage="2nd",
        )
        + item("Personal Income and Outlays, July 2026", "https://www.bea.gov/news/2026/pio")
        + item(
            "U.S. International Trade in Goods and Services, July 2026",
            "https://www.bea.gov/news/2026/trade",
        )
    )
    batch = await fetch(body)
    assert batch.provider == "bea_releases"
    by_code = {event.event_code: event for event in batch.events}
    assert set(by_code) == {"GDP", "PCE", "TRADE"}
    assert by_code["GDP"].category == "growth"
    assert by_code["PCE"].category == "inflation"
    assert by_code["PCE"].importance == "critical"
    assert all(event.status == "released" for event in batch.events)
    assert all(event.scheduled_at is None for event in batch.events)
    assert all(event.actual is None and event.consensus is None for event in batch.events)
    assert all(event.released_at is not None for event in batch.events)
    assert any("vintage" in note.lower() for note in by_code["GDP"].notes)


async def test_rejects_untrusted_link_and_future_and_malformed():
    body = rss(
        item("GDP, 2nd Quarter 2026", "https://evil.example.com/gdp")
        + item(
            "GDP, 1st Quarter 2026",
            "https://www.bea.gov/news/2026/gdp-future",
            pubdate="Wed, 26 Aug 2030 08:30:00 EDT",
        )
        + "<item><title>No link</title><pubDate>Wed, 26 Aug 2026 08:30:00 EDT</pubDate></item>"
    )
    batch = await fetch(body)
    assert batch.events == []
    assert batch.status == "degraded"
    assert batch.issues


async def test_rejects_invalid_root():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="<html/>"))
    ) as client:
        batch = await BEAReleasesProvider(client, clock=lambda: NOW).fetch()
    assert batch.status == "unavailable"


async def test_stable_ids_across_fetch():
    body = rss(item("Personal Income and Outlays, July 2026", "https://www.bea.gov/news/2026/pio"))
    first = await fetch(body)
    second = await fetch(body)
    assert first.events[0].id == second.events[0].id
