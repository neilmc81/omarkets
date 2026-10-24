from __future__ import annotations

from datetime import UTC, datetime, timedelta

from core.domain.news import (
    EconomicCalendarDetail,
    SourceItem,
    SourceMetadata,
    SourceType,
)
from core.ingest.dedupe import (
    DEFAULT_WINDOW,
    find_match,
    is_near_duplicate,
    similarity,
    tokenize,
)
from core.ingest.normalize import (
    clean_text,
    deduplication_key,
    event_id_from_key,
    normalize_headline,
    normalize_item,
)

NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)

METADATA = SourceMetadata(
    name="test_source",
    display_name="Test",
    source_type=SourceType.NEWS,
)


def _item(**overrides) -> SourceItem:
    values = {
        "source": "test_source",
        "source_type": SourceType.NEWS,
        "source_id": "1",
        "headline": "Fed signals a rate cut",
        "received_at": NOW,
        "raw": {"a": 1},
    }
    values.update(overrides)
    return SourceItem.model_validate(values)


def test_clean_text_collapses_and_nulls_empty() -> None:
    assert clean_text("  a   b\n c ") == "a b c"
    assert clean_text("   ") is None
    assert clean_text(None) is None


def test_normalize_headline_strips_case_and_punctuation() -> None:
    assert normalize_headline("Fed Signals, a RATE CUT!") == normalize_headline(
        "fed signals a rate cut"
    )


def test_dedup_key_stable_across_punctuation() -> None:
    first = normalize_item(_item(), METADATA)
    second = normalize_item(_item(headline="Fed signals, a rate cut!"), METADATA)
    assert first.id == second.id
    assert first.id == event_id_from_key(first.deduplication_key)


def test_dedup_key_differs_on_instrument_divergence() -> None:
    key_a = deduplication_key(
        source_type="news", headline="Markets rise", countries=[], entities=[]
    )
    key_b = deduplication_key(
        source_type="geopolitical", headline="Markets rise", countries=[], entities=[]
    )
    assert key_a != key_b


def test_normalize_item_extracts_entities_and_events() -> None:
    event = normalize_item(
        _item(
            headline="Nvidia beats as Fed signals a rate cut, China watches",
            metadata={"countries": ["US"]},
        ),
        METADATA,
    )
    assert "NVDA" in event.instruments
    assert "US" in event.countries
    assert "CN" in event.countries
    assert event.event_type == "rate_decision"
    assert event.ingestion_status.value == "normalized"
    assert event.first_seen_at == event.published_at


def test_normalize_item_preserves_central_bank_and_economic() -> None:
    detail = EconomicCalendarDetail(event_code="CPI", name="CPI", unit="percent", consensus=2.4)
    event = normalize_item(_item(economic=detail), METADATA)
    assert event.economic == detail


def test_normalize_item_without_timestamp_has_null_published() -> None:
    event = normalize_item(_item(published_at=None), METADATA)
    assert event.published_at is None
    assert event.received_at == NOW


def test_tokenize_and_similarity() -> None:
    assert "the" not in tokenize("the fed holds rates")
    assert similarity("Fed holds rates steady", "Fed holds rates steady") == 1.0
    assert similarity("Fed holds rates", "Nvidia beats earnings") < 0.2


def test_economic_metrics_do_not_merge_by_headline_similarity() -> None:
    def release(title, code="GDP", period="2026-Q2"):
        return normalize_item(
            _item(
                headline=title,
                source_type=SourceType.ECONOMIC_CALENDAR,
                economic=EconomicCalendarDetail(
                    event_code=code, name=title, unit="percent", period=period
                ),
            ),
            METADATA,
        )

    growth = release("Final GDP q/q")
    price = release("Final GDP Price Index q/q", "GDP_PRICE_INDEX")
    assert not is_near_duplicate(growth, price, threshold=0.1)
    assert not is_near_duplicate(release("CPI m/m", "CPI"), release("CPI y/y", "CPI"))
    assert not is_near_duplicate(growth, release("Final GDP q/q", period="2026-Q1"))
    assert is_near_duplicate(growth, release("Final GDP, q/q!"))


def test_published_numeric_release_is_not_a_legacy_schedule_or_other_vintage() -> None:
    detail = EconomicCalendarDetail(
        event_code="INITIAL_CLAIMS", name="Unemployment Claims", unit="value",
        period="2026-09-26", actual=197000,
    )
    release = normalize_item(
        _item(headline=detail.name, economic=detail, published_at=NOW), METADATA,
    )
    schedule = normalize_item(_item(
        headline=detail.name, economic=detail.model_copy(update={"period": None, "actual": None}),
    ), METADATA)
    revised = normalize_item(_item(
        headline=detail.name, economic=detail.model_copy(update={"actual": 198000}),
        published_at=NOW,
    ), METADATA)
    assert len({release.id, schedule.id, revised.id}) == 3
    assert len(release.id) == 35
    assert not is_near_duplicate(release, schedule)
    assert not is_near_duplicate(release, revised)
    assert normalize_item(_item(
        headline=detail.name, economic=detail, published_at=NOW,
        received_at=NOW + timedelta(minutes=5),
    ), METADATA).id == release.id


def test_near_duplicate_requires_compatible_universe() -> None:
    base = normalize_item(_item(headline="Fed signals a rate cut"), METADATA)
    same = normalize_item(
        _item(source_id="2", headline="Fed signals a rate cut after meeting"), METADATA
    )
    different = normalize_item(
        _item(source_id="3", headline="Fed signals a rate cut", metadata={"countries": ["CN"]}),
        METADATA,
    )
    assert is_near_duplicate(base, same)
    assert not is_near_duplicate(base, different)


def test_near_duplicate_window_blocks_far_reports() -> None:
    base = normalize_item(_item(published_at=NOW), METADATA)
    far = normalize_item(
        _item(
            source_id="2",
            headline="Fed signals a rate cut",
            published_at=NOW + DEFAULT_WINDOW + timedelta(hours=1),
        ),
        METADATA,
    )
    assert not is_near_duplicate(base, far)


def test_find_match_prefers_exact_key() -> None:
    first = normalize_item(_item(), METADATA)
    second = normalize_item(_item(source_id="2"), METADATA)
    match = find_match(second, [first])
    assert match is not None and match.id == first.id
    assert find_match(second, []) is None


def test_html_summary_is_readable_plain_text_and_source_is_preserved():
    summary = '<p>Chips &amp; markets</p><p>Read <a href="https://example.com">more</a>.</p>'
    item = _item(summary=summary, source_url="https://example.com/story",
                 raw={"summary": summary})
    result = normalize_item(item, METADATA)
    assert result.summary == "Chips & markets Read more."
    assert result.source_url == item.source_url
    assert result.raw_payload["summary"] == summary
    assert normalize_item(_item(summary='<script>alert(1)</script><p>Safe</p>'),
                          METADATA).summary == "Safe"


def test_encoded_html_summary_and_plain_comparisons_remain_readable():
    from core.ingest.normalize import plain_text

    assert plain_text("&lt;p&gt;Chips &amp;amp; markets&lt;/p&gt;") == "Chips & markets"
    assert plain_text("PMI < 50 and rates > 0") == "PMI < 50 and rates > 0"
