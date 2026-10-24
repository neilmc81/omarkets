"""Generic RSS 2.0 / Atom parsing and a configurable feed source.

The parser is deliberately strict: it validates the document root, enforces a
maximum item count, requires an item identity (guid/id or link), normalizes all
timestamps to UTC, and rejects items with no usable headline. It never guesses a
missing publication time. Parsing uses ``defusedxml`` so hostile feeds cannot
mount entity-expansion attacks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from defusedxml.ElementTree import ParseError, fromstring

from core.domain.news import SourceItem, SourceMetadata, SourceType
from core.providers.news.base import HttpNewsSource

MAX_ITEMS = 200
_ATOM_NS = "{http://www.w3.org/2005/Atom}"


@dataclass(frozen=True)
class ParsedFeedItem:
    title: str
    link: str | None
    guid: str | None
    published_at: datetime | None
    summary: str | None
    categories: tuple[str, ...] = field(default_factory=tuple)


def _text(element, tag: str) -> str | None:
    child = element.find(tag)
    if child is None:
        return None
    value = (child.text or "").strip()
    return value or None


def parse_date(value: str | None) -> datetime | None:
    """Parse an RFC-822 or ISO-8601 feed timestamp into aware UTC.

    Returns ``None`` rather than guessing when the value is missing or
    unparseable. Naive ISO timestamps are interpreted as UTC because RSS/Atom
    timestamps are defined in UTC; the caller may mark them as assumed.
    """
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _child_text(element, names: tuple[str, ...]) -> str | None:
    for name in names:
        value = _text(element, name)
        if value:
            return value
        value = _text(element, _ATOM_NS + name)
        if value:
            return value
    return None


def parse_feed(payload: bytes) -> list[ParsedFeedItem]:
    """Parse an RSS 2.0 or Atom document into feed items.

    A web page instead of the feed (the Bank of Japan sometimes answers its RSS
    URL with an HTML page and HTTP 200) or broken XML is a clean ``ValueError``
    for this poll; stored items are untouched and the next poll starts afresh.
    """
    head = payload[:256].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith((b"<!doctype html", b"<html")):
        raise ValueError("received a web page (HTML) instead of the feed")
    try:
        root = fromstring(payload)
    except ParseError as exc:
        raise ValueError(f"feed XML not well-formed ({exc})") from None
    tag = root.tag
    if tag == "rss":
        channel = root.find("channel")
        if channel is None:
            raise ValueError("rss document has no channel")
        entries = channel.findall("item")
        atom = False
    elif tag == _ATOM_NS + "feed" or tag == "feed":
        entries = root.findall(_ATOM_NS + "entry") or root.findall("entry")
        atom = True
    else:
        raise ValueError("unsupported feed document")
    if len(entries) > MAX_ITEMS:
        raise ValueError("feed has too many items")

    items: list[ParsedFeedItem] = []
    for entry in entries:
        title = _child_text(entry, ("title",))
        if not title:
            continue
        if atom:
            link = None
            for link_element in entry.findall(_ATOM_NS + "link") + entry.findall("link"):
                href = link_element.get("href")
                rel = link_element.get("rel", "alternate")
                if href and rel in ("alternate", ""):
                    link = href.strip()
                    break
            guid = _child_text(entry, ("id",))
            published = parse_date(_child_text(entry, ("updated", "published")))
            summary = _child_text(entry, ("summary", "content"))
        else:
            link = _child_text(entry, ("link",))
            guid = _child_text(entry, ("guid",))
            published = parse_date(_child_text(entry, ("pubDate", "date")))
            summary = _child_text(entry, ("description", "summary"))
        categories = tuple(
            (element.text or "").strip()
            for element in entry.findall("category") + entry.findall(_ATOM_NS + "category")
            if (element.text or "").strip()
        )
        items.append(
            ParsedFeedItem(
                title=" ".join(title.split()),
                link=link,
                guid=guid,
                published_at=published,
                summary=" ".join(summary.split()) if summary else None,
                categories=categories,
            )
        )
    return items


class RssFeedSource(HttpNewsSource):
    """A configurable RSS/Atom feed adapter.

    ``source_id`` for each item prefers the feed ``guid``/Atom ``id`` and falls
    back to the link so re-fetching a feed is idempotent.
    """

    def __init__(self, *, metadata: SourceMetadata, url: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._metadata = metadata
        self.url = url

    @property
    def name(self) -> str:
        return self._metadata.name

    @property
    def metadata(self) -> SourceMetadata:
        return self._metadata

    def document_type(self, item: ParsedFeedItem) -> str | None:
        """Deterministic document type from a title. Overridable per source."""
        return None

    def extra_metadata(self, item: ParsedFeedItem) -> dict:
        return {}

    async def fetch(self) -> list[SourceItem]:
        payload = await self.request("GET", self.url)
        parsed = parse_feed(payload)
        now = self.clock()
        items: list[SourceItem] = []
        for entry in parsed:
            source_id = entry.guid or entry.link
            if not source_id:
                continue
            items.append(
                SourceItem(
                    source=self.name,
                    source_type=self._metadata.source_type,
                    source_id=source_id,
                    source_url=entry.link,
                    headline=entry.title,
                    summary=entry.summary,
                    published_at=entry.published_at,
                    received_at=now,
                    language=self._metadata.language,
                    event_type=None,
                    metadata={
                        "categories": list(entry.categories),
                        "document_type": self.document_type(entry),
                        **self.extra_metadata(entry),
                    },
                    raw={
                        "title": entry.title,
                        "link": entry.link,
                        "guid": entry.guid,
                        "published_at": entry.published_at.isoformat()
                        if entry.published_at
                        else None,
                        "summary": entry.summary,
                        "categories": list(entry.categories),
                    },
                )
            )
        return items


__all__ = [
    "MAX_ITEMS",
    "ParsedFeedItem",
    "RssFeedSource",
    "parse_date",
    "parse_feed",
]


def as_source_type(value: str) -> SourceType:  # pragma: no cover - convenience
    return SourceType(value)
