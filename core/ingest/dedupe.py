"""Deterministic deduplication.

Two layers, both cheap and explainable — no semantic AI:

1. **Exact canonical key.** :func:`core.ingest.normalize.deduplication_key`
   collapses the same story reported with punctuation/case differences.
2. **Near-duplicate detection.** Token-set similarity plus a publication-time
   window groups reports from independent feeds that differ only in outlet
   phrasing. This is deliberately conservative: it never merges events whose
   normalized instruments/countries diverge, and it is windowed in time.

The canonical event keeps a merged ``occurrence_count`` and the set of
reporting sources; every source report is retained as a
:class:`~core.domain.news.SourceOccurrence`. Phase 6 may replace the near-dupe
heuristic with Jev novelty detection without changing the storage contract.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from core.domain.news import MarketEvent
from core.ingest.normalize import normalize_headline

#: Minimum Jaccard token overlap for two events to be considered the same story.
#: Conservative: wording paraphrases below this are left as separate events for
#: Phase 6 novelty detection rather than merged by a weak heuristic.
DEFAULT_SIMILARITY_THRESHOLD = 0.6
#: Reports further apart than this are never merged, even with an identical
#: normalized headline, so a recurring headline ("Markets rise") does not merge
#: across days.
DEFAULT_WINDOW = timedelta(hours=36)

_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "to",
        "was",
        "were",
        "will",
        "with",
        "after",
        "amid",
        "says",
        "said",
        "new",
    }
)


def tokenize(headline: str) -> frozenset[str]:
    """Significant lowercase tokens of a headline."""
    tokens = {
        token
        for token in "".join(
            char if char.isalnum() else " " for char in headline.casefold()
        ).split()
        if token and token not in _STOPWORDS
    }
    return frozenset(tokens)


def similarity(left: str, right: str) -> float:
    """Jaccard similarity of headline token sets (0.0–1.0)."""
    a, b = tokenize(left), tokenize(right)
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    union = len(a | b)
    return intersection / union if union else 0.0


def _event_moment(event: MarketEvent) -> datetime | None:
    return event.published_at or event.received_at


def _compatible(candidate: MarketEvent, event: MarketEvent) -> bool:
    """Do two events describe the same universe of assets/countries?"""
    if candidate.economic is not None or event.economic is not None:
        # Release metrics are not interchangeable stories. Headline token
        # overlap can merge GDP growth with its price index, or m/m with y/y.
        # Keep source-specific names distinct; explicit calendar grouping owns
        # cross-source release identity, not this news similarity heuristic.
        left, right = candidate.economic, event.economic
        if left is None or right is None:
            return False
        if (left.event_code, left.unit) != (right.event_code, right.unit):
            return False
        if left.period != right.period:
            return False
        if candidate.published_at != event.published_at:
            return False
        if (left.actual, left.consensus, left.previous, left.revised_previous) != (
            right.actual, right.consensus, right.previous, right.revised_previous
        ):
            return False
        if normalize_headline(candidate.headline) != normalize_headline(event.headline):
            return False
    if candidate.source_type != event.source_type:
        # A central-bank statement and a news report of it may still match, but
        # only when one side is a news report and the other is official.
        pass
    if set(candidate.instruments) != set(event.instruments):
        return False
    if set(candidate.countries) != set(event.countries):
        return False
    return True


def is_near_duplicate(
    candidate: MarketEvent,
    event: MarketEvent,
    *,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    window: timedelta = DEFAULT_WINDOW,
) -> bool:
    """Whether ``event`` is a near-duplicate of an existing ``candidate``.

    An exact deduplication-key match still respects the time window: recurring
    headlines such as "Markets rise" must not collapse across days.
    """
    if not _compatible(candidate, event):
        return False
    left, right = _event_moment(candidate), _event_moment(event)
    if left is not None and right is not None and abs(left - right) > window:
        return False
    if event.deduplication_key == candidate.deduplication_key:
        return True
    return similarity(candidate.headline, event.headline) >= threshold


def find_match(
    event: MarketEvent,
    existing: Iterable[MarketEvent],
    *,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    window: timedelta = DEFAULT_WINDOW,
) -> MarketEvent | None:
    """Return the best existing canonical match for ``event``, or ``None``.

    Exact key matches win outright; otherwise the most similar compatible,
    in-window candidate above ``threshold`` is returned.
    """
    best: MarketEvent | None = None
    best_score = 0.0
    for candidate in existing:
        if not is_near_duplicate(candidate, event, threshold=threshold, window=window):
            continue
        if candidate.deduplication_key == event.deduplication_key:
            return candidate
        score = similarity(candidate.headline, event.headline)
        if score > best_score:
            best, best_score = candidate, score
    return best


__all__ = [
    "DEFAULT_SIMILARITY_THRESHOLD",
    "DEFAULT_WINDOW",
    "find_match",
    "is_near_duplicate",
    "similarity",
    "tokenize",
]
