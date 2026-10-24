"""The explainer library: coverage of everything the UI explains, no advice, API contract."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from core.analysis import levels
from core.api import explainers as api
from core.desk import auctions, facts, watch
from core.desk import explainers as X
from core.domain.chart import KeyLevel
from core.domain.sessions import SegmentName
from core.providers.economic_consensus import EVENT_CODES

ROOT = Path(__file__).resolve().parents[2]


def test_every_strip_and_reaction_instrument_has_an_entry():
    for symbol in {*facts.STRIP_SYMBOLS, *facts.REACTION_SYMBOLS, *facts.CASH_INDEXES}:
        entry = X.instrument(symbol)
        assert entry is not None, symbol
        assert entry.title == facts.LABELS[symbol]
        assert entry.rising and entry.falling


def test_every_key_level_has_an_entry():
    keys = KeyLevel.model_fields["key"].annotation.__args__
    assert set(keys) == set(levels.LABELS) == {e.name for e in X.LEVELS}
    for key in keys:
        assert X.level(key).title == levels.LABELS[key]
        assert levels.EXPLAINERS[key] == X.level(key).full_text


def test_every_session_segment_has_an_entry():
    assert {e.name for e in X.SEGMENTS} == {s.value for s in SegmentName}


def test_every_calendar_code_the_desk_uses_has_an_entry():
    codes = {code for _, code, _, _ in EVENT_CODES}
    codes |= {p for _, prefixes in X.READING_CODES for p in prefixes}
    codes |= facts.FED_CODES | facts.SPEAKER_CODES | {"BEIGE_BOOK", "CORPORATE_PROFITS"}
    missing = sorted(c for c in codes if X.indicator(c) is None)
    assert not missing


def test_every_reading_family_has_an_entry_with_both_directions():
    for family, _ in X.READING_CODES:
        entry = X.reading(family)
        assert entry.rising and entry.falling
    assert watch.reading("CPI_MOM").label == X.reading("inflation").title
    assert watch.reading("GDP_PRICE_INDEX").family == "inflation"
    assert watch.reading("BEIGE_BOOK") is None


def test_old_homes_read_from_the_library():
    assert facts.explainer("CPI") == X.indicator("CPI").text
    assert facts.friendly_name("CPI", "x") == "Consumer prices (CPI)"
    assert facts.friendly_name("AHE", "Average Hourly Earnings m/m") == \
        "Average Hourly Earnings m/m"  # names key the playbook; unchanged codes keep theirs
    assert auctions.EXPLAINER == X.topic("treasury_auction").text
    assert X.topic("rates").text.startswith("Treasury yields")


_ADVICE = re.compile(r"\b(buy|sell|you should|should you|go long|go short|short it)\b",
                     re.IGNORECASE)


def test_no_entry_gives_advice():
    for entry in X.ENTRIES:
        for text in (entry.what, entry.why_nq, entry.rising or "", entry.falling or ""):
            assert not _ADVICE.search(text), (entry.key, text)


def test_entries_are_unique_complete_and_labelled():
    assert len(X.LIBRARY) == len(X.ENTRIES)
    for entry in X.ENTRIES:
        assert entry.key == f"{entry.kind}:{entry.name}"
        assert entry.title and entry.what and entry.why_nq
        assert (entry.rising is None) == (entry.rising_label is None)
        assert (entry.falling is None) == (entry.falling_label is None)
    assert set(X.UNSURE) <= set(X.LIBRARY)


def test_review_document_is_current():
    path = ROOT / "docs" / "explainers-review.md"
    if not path.exists():
        pytest.skip("internal review document is not shipped in releases")
    doc = path.read_text()
    assert doc == X.review_markdown(), "regenerate: python -m core.desk.explainers"


def test_explainers_api_contract():
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        body = client.get("/api/v1/explainers").json()
    assert set(body) == {"entries"}
    entries = body["entries"]
    assert len(entries) == len(X.ENTRIES)
    first = entries[0]
    assert set(first) == {"key", "kind", "name", "title", "what", "why_nq", "rising",
                          "falling", "rising_label", "falling_label"}
    assert first["key"] == "instrument:NQ" and first["rising_label"] == "Rising"
    by_key = {e["key"]: e for e in entries}
    assert by_key["level:overnight_high"]["rising_label"] == "Above it"
    assert by_key["indicator:CPI"]["falling_label"] == "Below forecast"
    assert by_key["segment:cash"]["rising"] is None
