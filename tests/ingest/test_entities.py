from __future__ import annotations

from core.ingest.entities import (
    classify_event_type,
    extract_countries,
    extract_entities,
    extract_institutions,
    extract_instruments,
    extract_regions,
)


def test_extract_instruments_from_headline() -> None:
    text = "Nasdaq futures rise as Nvidia and Broadcom lead chip rally"
    assert extract_instruments(text) == ["NQ", "NVDA", "AVGO"]


def test_extract_instruments_bare_tickers_and_longest_alias() -> None:
    assert extract_instruments("NQ and ES futures") == ["NQ", "ES"]
    # "S&P 500" must beat nothing and map to ES, not a bare ticker.
    assert extract_instruments("S&P 500 closes higher") == ["ES"]


def test_extract_instruments_does_not_invent_unknown_tickers() -> None:
    assert extract_instruments("XYZ corporation announces results") == []


def test_extract_countries_regions_and_institutions() -> None:
    text = "The Federal Reserve and the ECB warned about China and the Middle East"
    assert extract_countries(text) == ["CN", "EU", "US"]
    assert extract_regions(text) == ["MIDDLE_EAST"]
    institutions = extract_institutions(text)
    ids = {item[0] for item in institutions}
    assert ids == {"FED", "ECB"}


def test_extract_entities_links_institution_country() -> None:
    entities = extract_entities("Bank of Japan holds rates")
    kinds = {(entity.kind.value, entity.id) for entity in entities}
    assert ("institution", "BOJ") in kinds
    assert ("country", "JP") in kinds


def test_extract_entities_includes_extra_metadata() -> None:
    entities = extract_entities(
        "Quarterly release",
        extra_countries=["US"],
        extra_institutions=[("UST", "US Treasury", "US")],
    )
    kinds = {(entity.kind.value, entity.id) for entity in entities}
    assert ("country", "US") in kinds
    assert ("institution", "UST") in kinds


def test_classify_event_type_is_deterministic_and_neutral() -> None:
    assert classify_event_type("Fed signals a rate cut") == "rate_decision"
    assert classify_event_type("Treasury imposes new sanctions") == "sanctions"
    assert classify_event_type("Something entirely unremarkable") is None
    assert classify_event_type(None, "") is None


def test_classify_event_type_uses_word_boundaries_not_substrings() -> None:
    # "port" must not match "Portugal"/"reported"; "war" must not match "warning".
    assert classify_event_type("Ronaldo named in Portugal squad") is None
    assert classify_event_type("Central bank issues warning on liquidity") is None
    assert classify_event_type("Shipping through the Red Sea port halted") == "shipping_disruption"


def test_extract_is_case_insensitive() -> None:
    assert extract_instruments("NASDAQ FUTURES") == ["NQ"]
    assert extract_countries("JAPAN") == ["JP"]
