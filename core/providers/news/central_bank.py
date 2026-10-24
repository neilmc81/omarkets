"""Central-bank feed adapters.

Central-bank content is treated as a first-class source type. Deterministic
metadata (document type, central bank, speaker when the source names one) is
extracted from titles/categories; no semantic interpretation is attempted.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from core.domain.news import (
    CentralBankMetadata,
    SourceItem,
    SourceMetadata,
    SourceType,
)
from core.providers.news.base import HttpNewsSource
from core.providers.news.rss import ParsedFeedItem, RssFeedSource, parse_feed

_DOCUMENT_TYPES: tuple[tuple[str, str], ...] = (
    ("minutes", "minutes"),
    ("statement", "statement"),
    ("press conference", "press_conference"),
    ("economic projections", "economic_projections"),
    ("testimony", "testimony"),
    ("testifies", "testimony"),
    ("speech", "speech"),
    ("remarks", "speech"),
    ("transcript", "transcript"),
    ("monetary policy report", "monetary_policy_report"),
    ("beige book", "beige_book"),
    ("decision", "rate_decision"),
    ("rate", "rate_decision"),
)

#: "Remarks by Governor X", "Speech by President Y", "Testimony of Chair Z".
_SPEAKER = re.compile(
    r"\b(?:by|of)\s+((?:Governor|Chair(?:man|woman)?|President|Vice Chair|"
    r"Deputy Governor|Chief|Member|Mr\.|Ms\.|Dr\.)[^,.;:]*|[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})"
)
_MEETING_DATE = re.compile(
    r"\b(\d{4})-(\d{2})-(\d{2})\b|\b([A-Z][a-z]+)\s+(\d{1,2})(?:-\d{1,2})?,?\s+(\d{4})\b"
)


def document_type(title: str, categories: tuple[str, ...] = ()) -> str:
    lowered = f"{title} {' '.join(categories)}".lower()
    for needle, document in _DOCUMENT_TYPES:
        if needle in lowered:
            return document
    return "press_release"


def extract_speaker(title: str) -> str | None:
    match = _SPEAKER.search(title)
    if not match:
        return None
    speaker = match.group(1).strip()
    return speaker or None


def extract_meeting_date(text: str) -> str | None:
    match = _MEETING_DATE.search(text)
    if not match:
        return None
    if match.group(1):
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    return None


class CentralBankFeedSource(RssFeedSource):
    """RSS/Atom feed belonging to a central bank or monetary authority."""

    def __init__(self, *, central_bank: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.central_bank = central_bank

    def document_type(self, item: ParsedFeedItem) -> str | None:
        return document_type(item.title, item.categories)

    def extra_metadata(self, item: ParsedFeedItem) -> dict:
        speaker = extract_speaker(item.title)
        meeting = extract_meeting_date(item.title)
        central_bank = CentralBankMetadata(
            central_bank=self.central_bank,
            speaker=speaker,
            document_type=document_type(item.title, item.categories),
            meeting_date=datetime.fromisoformat(meeting).date() if meeting else None,
        )
        return {
            "central_bank": self.central_bank,
            "speaker": speaker,
            "meeting_date": meeting,
            "central_bank_metadata": central_bank.model_dump(mode="json"),
        }

    async def fetch(self) -> list[SourceItem]:
        items = await super().fetch()
        enriched: list[SourceItem] = []
        for item in items:
            payload = item.metadata.get("central_bank_metadata")
            metadata = item.model_copy(
                update={
                    "central_bank": CentralBankMetadata.model_validate(payload)
                    if payload
                    else None,
                    "event_type": item.metadata.get("document_type"),
                }
            )
            enriched.append(metadata)
        return enriched


class BoJWhatsNewSource(HttpNewsSource):
    """Bank of Japan "What's New" feed (non-standard Atom-ish RSS)."""

    source_type = SourceType.CENTRAL_BANK

    def __init__(
        self, *, metadata: SourceMetadata, url: str, central_bank: str = "BOJ", **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self._metadata = metadata
        self.url = url
        self.central_bank = central_bank

    @property
    def name(self) -> str:
        return self._metadata.name

    @property
    def metadata(self) -> SourceMetadata:
        return self._metadata

    async def fetch(self) -> list[SourceItem]:
        payload = await self.request("GET", self.url)
        parsed = parse_feed(payload)
        now = self.clock()
        items: list[SourceItem] = []
        for entry in parsed:
            source_id = entry.guid or entry.link
            if not source_id:
                continue
            document = document_type(entry.title, entry.categories)
            speaker = extract_speaker(entry.title)
            items.append(
                SourceItem(
                    source=self.name,
                    source_type=self.source_type,
                    source_id=source_id,
                    source_url=entry.link,
                    headline=entry.title,
                    summary=entry.summary,
                    published_at=entry.published_at,
                    received_at=now,
                    language="en",
                    event_type=document,
                    metadata={
                        "central_bank": self.central_bank,
                        "document_type": document,
                        "speaker": speaker,
                    },
                    raw={
                        "title": entry.title,
                        "link": entry.link,
                        "guid": entry.guid,
                        "published_at": entry.published_at.isoformat()
                        if entry.published_at
                        else None,
                    },
                    central_bank=CentralBankMetadata(
                        central_bank=self.central_bank,
                        speaker=speaker,
                        document_type=document,
                    ),
                )
            )
        return items


def _now() -> datetime:  # pragma: no cover - default clock
    return datetime.now(UTC)


__all__ = [
    "BoJWhatsNewSource",
    "CentralBankFeedSource",
    "document_type",
    "extract_meeting_date",
    "extract_speaker",
]
