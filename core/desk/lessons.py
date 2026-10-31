"""The Learn page's lesson topics and progress. Fixed keyword rules; no AI.

Each lesson (a note's Learn box) is tagged with one topic from the term it
teaches: the words before a colon, or else its first sentence. A lesson that
names none of the topic words is filed under "Market moves". A short heading
is taken from the same place when there is one.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable

from core.desk.models import LearnEntry, LearnLibrary, LearnTopic

#: (key, label, words), checked in this order; the first match wins.
TOPICS: tuple[tuple[str, str, re.Pattern[str]], ...] = tuple(
    (key, label, re.compile(words, re.IGNORECASE)) for key, label, words in (
        ("auctions", "Auctions", r"\b(auctions?|bid-to-cover|tail(ed|s)?|primary dealers?)\b"),
        ("earnings", "Earnings", r"\b(earnings|eps|guidance|revenue|profits? per share|beat|"
                                 r"results)\b"),
        ("jobs", "Jobs", r"\b(jobs?|jobless|payrolls?|unemployment|layoffs?|claims|hiring|"
                         r"wages?)\b"),
        ("inflation", "Inflation", r"\b(inflation|cpi|ppi|pce|prices? index|core)\b"),
        ("fed", "The Fed", r"\b(fed|federal reserve|fomc|rate (cut|hike)s?|dot plot|powell)\b"),
        ("market", "Market moves", r"\b(vix|risk-(on|off)|divergence|futures|flight to safety|"
                                   r"surprise|gap|sell-?off|rally|momentum)\b"),
        ("yields", "Bonds & yields", r"\b(yields?|basis points?|bp|bonds?|treasur(y|ies)|"
                                     r"curve|2-year|10-year)\b"),
    ))
DEFAULT = ("market", "Market moves")
LABELS = {key: label for key, label, _ in TOPICS}
#: Lessons shown under "Most taught".
MOST_TAUGHT = 3
#: How many of the latest lessons a note is told not to repeat (desk:v9).
RECENT = 5

_SENTENCE = re.compile(r"(?<=[.!?])\s")
_SUBJECT = re.compile(r"^(?:(?:an?|the)\s+)?([^,]{2,40}?)\s+(?:is|are|means|measures|compares)\b",
                      re.IGNORECASE)


def _lead(text: str) -> str:
    """The term a lesson teaches: before a colon, else its first sentence."""
    head, colon, _ = text.partition(":")
    if colon and len(head) <= 50:
        return head
    return _SENTENCE.split(text, maxsplit=1)[0]


def _clip(text: str, limit: int = 120) -> str:
    """``text`` cut to ``limit`` characters at a word boundary."""
    if len(text) <= limit:
        return text
    head = text[:limit + 1]
    return (head.rsplit(" ", 1)[0] if " " in head else text[:limit]).rstrip(" ,;.")


def recent_lessons(learns: Iterable[str], limit: int = RECENT) -> list[str]:
    """What the latest lessons taught (each one's term, see ``_lead``), newest
    first and without repeats; ``learns`` is the Learn box texts, newest first."""
    found: list[str] = []
    for text in learns:
        lead = _clip(" ".join(_lead(text or "").split()).strip(" ."))
        if lead and lead.casefold() not in {f.casefold() for f in found}:
            found.append(lead)
        if len(found) == limit:
            break
    return found


def topic(text: str) -> str:
    lead = _lead(text)
    return next((key for key, _, words in TOPICS if words.search(lead)), DEFAULT[0])


def heading(text: str) -> str | None:
    """A short title for the lesson card, or None when no term stands out."""
    head, colon, _ = text.partition(":")
    found = head if colon and len(head) <= 40 else None
    if found is None:
        match = _SUBJECT.match(_SENTENCE.split(text, maxsplit=1)[0])
        found = match.group(1) if match else None
    if not found:
        return None
    found = found.strip(" \"'“”‘’")
    return found[:1].upper() + found[1:] if found else None


def library(entries: Iterable[LearnEntry]) -> LearnLibrary:
    """Lessons tagged with their topic, with the counts the progress panel shows."""
    tagged: list[LearnEntry] = []
    for entry in entries:
        key = topic(entry.text)
        tagged.append(entry.model_copy(update={
            "topic": key, "topic_label": LABELS[key], "heading": heading(entry.text)}))
    counts = Counter(e.topic for e in tagged)
    topics = [LearnTopic(key=key, label=label, count=counts[key])
              for key, label, _ in TOPICS if counts[key]]
    most = sorted((e for e in tagged if e.times > 1), key=lambda e: -e.times)[:MOST_TAUGHT]
    return LearnLibrary(count=len(tagged), entries=tagged, topics=topics, most_taught=most,
                        taught=sum(e.times for e in tagged))


__all__ = ["TOPICS", "heading", "library", "topic"]
