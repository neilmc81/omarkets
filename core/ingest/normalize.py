"""Deterministic normalization from adapter :class:`SourceItem` to :class:`MarketEvent`.

Normalization is shared by every source so the event stream is consistent
regardless of vendor. It:

* trims and collapses text,
* normalizes timestamps to UTC and never invents a publication time,
* extracts entities, instruments, countries and regions deterministically,
* assigns a deterministic deduplication key,
* preserves the raw source payload verbatim.

It performs no classification of relevance, novelty or materiality.
"""

from __future__ import annotations

import hashlib
import re
from html import unescape
from html.parser import HTMLParser

from core.domain.news import (
    EntityRef,
    IngestionStatus,
    MarketEvent,
    SourceItem,
    SourceMetadata,
)
from core.ingest.entities import classify_event_type, extract_entities

_WHITESPACE = re.compile(r"\s+")
_PUNCTUATION = re.compile(r"[^\w\s]+", re.UNICODE)


def clean_text(value: str | None) -> str | None:
    """Collapse whitespace; return ``None`` for empty/whitespace-only input."""
    if value is None:
        return None
    cleaned = _WHITESPACE.sub(" ", value).strip()
    return cleaned or None



class _PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"p", "div", "br", "li", "h1", "h2", "tr"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1
        if tag in {"p", "div", "li", "h1", "h2", "tr"}:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def plain_text(value: str | None) -> str | None:
    if value is None:
        return None
    parser = _PlainText()
    parser.feed(unescape(value))
    parser.close()
    return clean_text("".join(parser.parts))

def normalize_headline(value: str) -> str:
    """Lowercase, punctuation-stripped headline used for exact dedup keys."""
    lowered = _PUNCTUATION.sub(" ", value.casefold())
    return _WHITESPACE.sub(" ", lowered).strip()


def fingerprint(*parts: str) -> str:
    """Stable short hash of normalized parts."""
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return digest[:32]


def deduplication_key(
    *,
    source_type: str,
    headline: str,
    countries: list[str],
    entities: list[EntityRef],
) -> str:
    """Deterministic strong key for a canonical event.

    Combines the normalized headline with stable entity ids so the same story
    with trivially different punctuation collapses, while different stories that
    merely share a headline cannot merge through entity divergence. Near
    duplicates are resolved separately by :mod:`core.ingest.dedupe`.
    """
    entity_ids = sorted(f"{entity.kind.value}:{entity.id}" for entity in entities)
    return fingerprint(
        "v1",
        source_type,
        normalize_headline(headline),
        ",".join(sorted(countries)),
        ",".join(entity_ids),
    )


def event_id_from_key(key: str) -> str:
    """Canonical event id derived from the deduplication key."""
    return f"ev:{key}"


def normalize_item(item: SourceItem, metadata: SourceMetadata) -> MarketEvent:
    """Convert one adapter item into a canonical market event.

    ``published_at`` is copied only when the adapter supplied one. When a feed
    has no usable timestamp the event is stored with ``published_at=None`` and
    consumers must order by ``received_at``.
    """
    headline = clean_text(item.headline) or ""
    if not headline:
        raise ValueError("source item requires a non-empty headline")
    summary = plain_text(item.summary)
    countries = sorted(set(item.metadata.get("countries", []) or []))
    regions = sorted(set(item.metadata.get("regions", []) or []))
    extra_institutions = item.metadata.get("institutions", []) or []
    entities = extract_entities(
        headline,
        summary,
        extra_countries=countries,
        extra_institutions=[tuple(entry) for entry in extra_institutions],
    )
    instruments = [entity.id for entity in entities if entity.kind.value == "instrument"]
    countries = sorted(
        {entity.id for entity in entities if entity.kind.value == "country"} | set(countries)
    )
    regions = sorted(
        {entity.id for entity in entities if entity.kind.value == "region"} | set(regions)
    )
    event_type = item.event_type or item.metadata.get("event_type")
    if not event_type:
        event_type = classify_event_type(headline, summary)
    key = deduplication_key(
        source_type=item.source_type.value,
        headline=headline,
        countries=countries,
        entities=entities,
    )
    published = item.published_at
    if published is not None and published.tzinfo is None:  # pragma: no cover - enforced by type
        published = None
    if item.economic is not None:
        # A recurring release headline is not a release identity. Preserve the
        # dated numeric evidence instead of merging it into a legacy schedule.
        identity = key + "|economic:" + item.economic.model_dump_json() + "|publication:" + (
            published.isoformat() if published is not None else "unverified"
        )
        key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    return MarketEvent(
        id=event_id_from_key(key),
        source=item.source,
        source_type=item.source_type,
        source_id=item.source_id,
        source_url=item.source_url,
        headline=headline,
        summary=summary,
        published_at=published,
        received_at=item.received_at,
        language=item.language or metadata.language,
        event_type=event_type,
        entities=entities,
        instruments=instruments,
        countries=countries,
        regions=regions,
        metadata=dict(item.metadata),
        raw_payload=dict(item.raw),
        central_bank=item.central_bank,
        economic=item.economic,
        deduplication_key=key,
        ingestion_status=IngestionStatus.NORMALIZED,
        first_seen_at=item.published_at,
        last_seen_at=item.published_at,
        occurrence_count=1,
        sources=[item.source],
    )


__all__ = [
    "clean_text",
    "deduplication_key",
    "event_id_from_key",
    "fingerprint",
    "normalize_headline",
    "normalize_item",
]
