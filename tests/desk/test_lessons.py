"""Learn page topics, headings and progress (fixed rules, no AI)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.desk import lessons as LS
from core.desk.models import LearnEntry

AT = datetime(2026, 10, 22, 13, 0, tzinfo=UTC)


@pytest.mark.parametrize(("text", "topic", "heading"), [
    ("Bid-to-cover: dollars bid for every dollar of bonds sold.", "auctions", "Bid-to-cover"),
    ("A basis point (bp) is one-hundredth of a percentage point.", "yields", "Basis point (bp)"),
    ("Guidance is a company's own forecast for the coming months.", "earnings", "Guidance"),
    ("For jobless claims, 'below forecast' means fewer layoffs.", "jobs", None),
    ("The VIX is called the fear gauge. Yields often fall when it jumps.", "market", "VIX"),
    ("Flight to safety: investors sell stocks and buy bonds, so yields fall.", "market",
     "Flight to safety"),
    ("Something with no topic words at all.", "market", None),
])
def test_topic_and_heading_come_from_the_term_taught(text, topic, heading):
    assert LS.topic(text) == topic
    assert LS.heading(text) == heading


def entry(text, times=1):
    return LearnEntry(text=text, note_id=text[:8], kind="ask", title="Market now",
                      created_at=AT, times=times)


def test_library_counts_topics_and_most_taught():
    found = LS.library([entry("A basis point (bp) is 0.01%.", times=5),
                        entry("The yield curve compares two yields.", times=2),
                        entry("Bid-to-cover: demand at an auction.")])
    assert found.count == 3 and found.taught == 8
    assert [(t.key, t.count) for t in found.topics] == [("auctions", 1), ("yields", 2)]
    assert [e.heading for e in found.most_taught] == ["Basis point (bp)", "Yield curve"]
    assert found.entries[2].topic_label == "Auctions"
