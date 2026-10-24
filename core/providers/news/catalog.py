"""Built-in news/geopolitical source catalog and factory.

Sources are declared as data in :data:`SOURCE_DEFINITIONS`. The factory wires
them from settings. Only reachable, public/official feeds are enabled by default;
each can be disabled and per-source polling intervals overridden. Credentialed
sources are declared but disabled until a key is configured.

A new source should be addable here with minimal changes elsewhere: declare a
definition and (if it is not a plain feed) an adapter class.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from core.domain.news import SourceMetadata, SourceType
from core.providers.news.base import NewsSource, utc_now
from core.providers.news.central_bank import BoJWhatsNewSource, CentralBankFeedSource
from core.providers.news.rss import RssFeedSource


@dataclass(frozen=True)
class SourceDefinition:
    name: str
    display_name: str
    source_type: SourceType
    url: str
    kind: str = "rss"  # rss | central_bank | boj | economic_calendar
    central_bank: str | None = None
    official: bool = True
    reliable: bool = True
    default_enabled: bool = True
    requires_credentials: bool = False
    language: str = "en"
    attribution: str | None = None
    poll_seconds: float = 120.0
    notes: tuple[str, ...] = field(default_factory=tuple)

    def metadata(self) -> SourceMetadata:
        return SourceMetadata(
            name=self.name,
            display_name=self.display_name,
            source_type=self.source_type,
            homepage=self.url,
            language=self.language,
            official=self.official,
            reliable=self.reliable,
            requires_credentials=self.requires_credentials,
            attribution=self.attribution,
            notes=list(self.notes),
        )


#: The complete built-in catalog. Order is display order.
SOURCE_DEFINITIONS: tuple[SourceDefinition, ...] = (
    # --- Central banks ---
    SourceDefinition(
        name="fed_press_monetary",
        display_name="Federal Reserve · Monetary Policy",
        source_type=SourceType.CENTRAL_BANK,
        url="https://www.federalreserve.gov/feeds/press_monetary.xml",
        kind="central_bank",
        central_bank="FED",
        poll_seconds=300.0,
        attribution="Federal Reserve Board",
        notes=["Official FOMC statements, minutes and monetary-policy notices."],
    ),
    SourceDefinition(
        name="fed_press_all",
        display_name="Federal Reserve · All Press Releases",
        source_type=SourceType.CENTRAL_BANK,
        url="https://www.federalreserve.gov/feeds/press_all.xml",
        kind="central_bank",
        central_bank="FED",
        poll_seconds=300.0,
        attribution="Federal Reserve Board",
    ),
    SourceDefinition(
        name="fed_speeches",
        display_name="Federal Reserve · Speeches",
        source_type=SourceType.CENTRAL_BANK,
        url="https://www.federalreserve.gov/feeds/speeches.xml",
        kind="central_bank",
        central_bank="FED",
        poll_seconds=600.0,
        attribution="Federal Reserve Board",
    ),
    SourceDefinition(
        name="ecb_press",
        display_name="European Central Bank · Press",
        source_type=SourceType.CENTRAL_BANK,
        url="https://www.ecb.europa.eu/rss/press.html",
        kind="central_bank",
        central_bank="ECB",
        poll_seconds=300.0,
        attribution="European Central Bank",
    ),
    SourceDefinition(
        name="boe_news",
        display_name="Bank of England · News",
        source_type=SourceType.CENTRAL_BANK,
        url="https://www.bankofengland.co.uk/rss/news",
        kind="central_bank",
        central_bank="BOE",
        poll_seconds=300.0,
        attribution="Bank of England",
    ),
    SourceDefinition(
        name="boj_whatsnew",
        display_name="Bank of Japan · What's New",
        source_type=SourceType.CENTRAL_BANK,
        url="https://www.boj.or.jp/en/rss/whatsnew.xml",
        kind="boj",
        central_bank="BOJ",
        poll_seconds=300.0,
        attribution="Bank of Japan",
    ),
    # --- Government / geopolitical / official statements ---
    SourceDefinition(
        name="treasury_press",
        display_name="US Treasury · Federal Register Documents",
        source_type=SourceType.GOVERNMENT,
        url=(
            "https://www.federalregister.gov/api/v1/documents.rss"
            "?conditions%5Bagencies%5D%5B%5D=treasury-department"
        ),
        kind="rss",
        poll_seconds=600.0,
        attribution="US Federal Register · Treasury Department",
        notes=[
            "Treasury press pages do not expose a durable RSS feed; the official "
            "Federal Register API feed is used instead."
        ],
    ),
    SourceDefinition(
        name="ofac_actions",
        display_name="US Treasury OFAC · Sanctions Actions",
        source_type=SourceType.GEOPOLITICAL,
        url=(
            "https://www.federalregister.gov/api/v1/documents.rss"
            "?conditions%5Bagencies%5D%5B%5D=foreign-assets-control-office"
        ),
        kind="rss",
        poll_seconds=600.0,
        attribution="US Federal Register · OFAC",
        notes=[
            "Sanctions and designations; often the primary market-moving geopolitical source.",
            "Served via the official Federal Register API because the OFAC XML "
            "endpoint is not machine-readable.",
        ],
    ),
    SourceDefinition(
        name="federal_register_presidential",
        display_name="US Federal Register · Presidential Documents",
        source_type=SourceType.GOVERNMENT,
        url=(
            "https://www.federalregister.gov/api/v1/documents.rss"
            "?conditions%5Btype%5D%5B%5D=PRESDOCU"
        ),
        kind="rss",
        poll_seconds=600.0,
        attribution="US Federal Register",
        notes=["Executive orders, proclamations and determinations."],
    ),
    SourceDefinition(
        name="state_press",
        display_name="US State Department · Press Releases",
        source_type=SourceType.GEOPOLITICAL,
        url="https://www.state.gov/rss-feed/press-releases/feed/",
        kind="rss",
        poll_seconds=600.0,
        attribution="US Department of State",
    ),
    SourceDefinition(
        name="whitehouse_actions",
        display_name="White House · Presidential Actions",
        source_type=SourceType.GOVERNMENT,
        url="https://www.whitehouse.gov/presidential-actions/feed/",
        kind="rss",
        poll_seconds=600.0,
        attribution="The White House",
    ),
    SourceDefinition(
        name="ec_press",
        display_name="European Commission · Press",
        source_type=SourceType.GOVERNMENT,
        url="https://ec.europa.eu/commission/presscorner/api/rss?language=en",
        kind="rss",
        poll_seconds=600.0,
        attribution="European Commission",
    ),
    # --- Commodity / energy ---
    SourceDefinition(
        name="eia_today",
        display_name="US EIA · Today in Energy",
        source_type=SourceType.COMMODITY,
        url="https://www.eia.gov/rss/todayinenergy.xml",
        kind="rss",
        poll_seconds=1800.0,
        attribution="US Energy Information Administration",
    ),
    SourceDefinition(
        name="energy_press",
        display_name="US Department of Energy · Press",
        source_type=SourceType.COMMODITY,
        url="https://www.energy.gov/rss/press-releases",
        kind="rss",
        poll_seconds=900.0,
        attribution="US Department of Energy",
    ),
    # --- News / geopolitical wire feeds ---
    SourceDefinition(
        name="bbc_world",
        display_name="BBC World News",
        source_type=SourceType.NEWS,
        url="https://feeds.bbci.co.uk/news/world/rss.xml",
        kind="rss",
        official=False,
        poll_seconds=120.0,
        attribution="BBC News",
        notes=["Third-party news wire; headline-level metadata only, no full article text."],
    ),
    SourceDefinition(
        name="guardian_world",
        display_name="The Guardian · World",
        source_type=SourceType.NEWS,
        url="https://www.theguardian.com/world/rss",
        kind="rss",
        official=False,
        poll_seconds=180.0,
        attribution="The Guardian",
        notes=["Third-party news wire; headline-level metadata only, no full article text."],
    ),
    SourceDefinition(
        name="npr_world",
        display_name="NPR · World",
        source_type=SourceType.NEWS,
        url="https://feeds.npr.org/1004/rss.xml",
        kind="rss",
        official=False,
        poll_seconds=300.0,
        attribution="NPR",
        notes=["Third-party news wire; headline-level metadata only, no full article text."],
    ),
    SourceDefinition(
        name="cnbc_top",
        display_name="CNBC · Top News",
        source_type=SourceType.NEWS,
        url="https://www.cnbc.com/id/100003114/device/rss/rss.html",
        kind="rss",
        official=False,
        poll_seconds=120.0,
        attribution="CNBC",
        notes=["Third-party market news wire; headline-level metadata only."],
    ),
    SourceDefinition(
        name="cnbc_markets",
        display_name="CNBC · Market Insider",
        source_type=SourceType.NEWS,
        url="https://www.cnbc.com/id/20409666/device/rss/rss.html",
        kind="rss",
        official=False,
        poll_seconds=120.0,
        attribution="CNBC",
        notes=["Third-party market news wire; headline-level metadata only."],
    ),
    SourceDefinition(
        name="cnbc_economy",
        display_name="CNBC · Economy",
        source_type=SourceType.NEWS,
        url="https://www.cnbc.com/id/20910258/device/rss/rss.html",
        kind="rss",
        official=False,
        poll_seconds=120.0,
        attribution="CNBC",
        notes=["Third-party economy news wire; headline-level metadata only."],
    ),
    SourceDefinition(
        name="cnbc_tech",
        display_name="CNBC · Tech",
        source_type=SourceType.NEWS,
        url="https://www.cnbc.com/id/19854910/device/rss/rss.html",
        kind="rss",
        official=False,
        poll_seconds=120.0,
        attribution="CNBC",
        notes=["Third-party technology news wire; headline-level metadata only."],
    ),
    SourceDefinition(
        name="cnbc_earnings",
        display_name="CNBC · Earnings",
        source_type=SourceType.NEWS,
        url="https://www.cnbc.com/id/15839135/device/rss/rss.html",
        kind="rss",
        official=False,
        poll_seconds=120.0,
        attribution="CNBC",
        notes=["Third-party earnings news wire; headline-level metadata only."],
    ),
)


#: Names enabled by default. All catalog sources are enabled; operators can
#: disable any by name via ``OMARKETS_NEWS_DISABLED_SOURCES``.
DEFAULT_ENABLED_SOURCES: tuple[str, ...] = tuple(
    definition.name for definition in SOURCE_DEFINITIONS if definition.default_enabled
)


def build_source(
    definition: SourceDefinition,
    *,
    clock: Callable[[], datetime] = utc_now,
    enabled: bool = True,
) -> NewsSource:
    metadata = definition.metadata()
    if definition.kind == "central_bank":
        return CentralBankFeedSource(
            metadata=metadata,
            url=definition.url,
            central_bank=definition.central_bank or definition.name,
            clock=clock,
            enabled=enabled,
        )
    if definition.kind == "boj":
        return BoJWhatsNewSource(
            metadata=metadata,
            url=definition.url,
            central_bank=definition.central_bank or "BOJ",
            clock=clock,
            enabled=enabled,
        )
    return RssFeedSource(metadata=metadata, url=definition.url, clock=clock, enabled=enabled)


__all__ = [
    "DEFAULT_ENABLED_SOURCES",
    "SOURCE_DEFINITIONS",
    "SourceDefinition",
    "build_source",
]
