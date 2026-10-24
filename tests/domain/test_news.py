from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from core.domain.news import (
    CentralBankMetadata,
    EconomicCalendarDetail,
    EntityKind,
    EntityRef,
    IngestionStatus,
    MarketEvent,
    RawEvent,
    SourceHealth,
    SourceItem,
    SourceMetadata,
    SourceOccurrence,
    SourceStatus,
    SourceType,
)

NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)


def _item(**overrides) -> SourceItem:
    values = {
        "source": "fed_press_monetary",
        "source_type": SourceType.CENTRAL_BANK,
        "source_id": "fed-1",
        "headline": "Federal Reserve issues FOMC statement",
        "published_at": NOW,
        "received_at": NOW,
        "raw": {"title": "x"},
    }
    values.update(overrides)
    return SourceItem.model_validate(values)


def _event(**overrides) -> MarketEvent:
    values = {
        "id": "ev:abc",
        "source": "fed_press_monetary",
        "source_type": SourceType.CENTRAL_BANK,
        "source_id": "fed-1",
        "headline": "Federal Reserve issues FOMC statement",
        "received_at": NOW,
        "deduplication_key": "abc",
    }
    values.update(overrides)
    return MarketEvent.model_validate(values)


def test_source_item_requires_aware_received_at() -> None:
    with pytest.raises(ValidationError):
        _item(received_at=datetime(2026, 9, 18, 12, 0, 0))


def test_source_item_normalizes_to_utc() -> None:
    item = _item(published_at=NOW.astimezone(UTC))
    assert item.published_at == NOW
    assert item.published_at.tzinfo is UTC


def test_market_event_rejects_unknown_instrument() -> None:
    with pytest.raises(ValidationError):
        _event(instruments=["NOTREAL"])


def test_market_event_rejects_duplicate_instruments() -> None:
    with pytest.raises(ValidationError):
        _event(instruments=["NQ", "NQ"])


def test_market_event_defaults_are_not_shared() -> None:
    first = _event()
    second = _event()
    first.countries.append("US")
    assert second.countries == []


def test_market_event_roundtrips_with_metadata() -> None:
    event = _event(
        instruments=["NQ"],
        countries=["US"],
        entities=[EntityRef(kind=EntityKind.INSTITUTION, id="FED", label="Federal Reserve")],
        central_bank=CentralBankMetadata(central_bank="FED", document_type="statement"),
        economic=EconomicCalendarDetail(
            event_code="CPI",
            name="Consumer Price Index",
            unit="percent",
            actual=None,
            consensus=2.4,
            previous=2.5,
        ),
    )
    assert MarketEvent.model_validate_json(event.model_dump_json()) == event
    assert event.central_bank is not None and event.central_bank.central_bank == "FED"


def test_source_health_disabled_must_report_disabled_status() -> None:
    with pytest.raises(ValidationError):
        SourceHealth(
            name="x",
            source_type=SourceType.NEWS,
            status=SourceStatus.HEALTHY,
            enabled=False,
        )
    health = SourceHealth(
        name="x",
        source_type=SourceType.NEWS,
        status=SourceStatus.DISABLED,
        enabled=False,
    )
    assert health.status is SourceStatus.DISABLED


def test_source_occurrence_and_raw_event_roundtrip() -> None:
    occurrence = SourceOccurrence(
        occurrence_key="fed_press_monetary:fed-1",
        event_id="ev:abc",
        source="fed_press_monetary",
        source_type=SourceType.CENTRAL_BANK,
        source_id="fed-1",
        headline="Headline",
        published_at=None,
        received_at=NOW,
        fingerprint="abc",
    )
    assert SourceOccurrence.model_validate_json(occurrence.model_dump_json()) == occurrence
    raw = RawEvent(
        source="fed_press_monetary",
        source_id="fed-1",
        fetched_at=NOW,
        content_hash="deadbeef",
        payload={"a": 1},
    )
    assert RawEvent.model_validate_json(raw.model_dump_json()) == raw


def test_source_metadata_is_extensible_and_typed() -> None:
    metadata = SourceMetadata(
        name="x",
        display_name="X",
        source_type=SourceType.GOVERNMENT,
        official=True,
        attribution="X",
    )
    assert metadata.source_type is SourceType.GOVERNMENT
    assert metadata.requires_credentials is False
    with pytest.raises(ValidationError):
        SourceMetadata(name="x", display_name="X", source_type="not_a_type")


def test_ingestion_status_values() -> None:
    assert IngestionStatus.STORED.value == "stored"
    assert _event(ingestion_status=IngestionStatus.DUPLICATE).ingestion_status is (
        IngestionStatus.DUPLICATE
    )
