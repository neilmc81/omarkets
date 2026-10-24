"""The News page's view model: recent headlines, tagged without AI.

Each headline gets a market-moving category from fixed keyword and source
rules (Fed & central banks, Economy, Big tech, Energy, Geopolitics) or none,
which hides it behind a count; "scheduled" when it lands just after a
scheduled release or Fed event it is about; and NQ's move in the 15 minutes
after it was published (the same measurement as the chart pins). Headlines are
not analysis: the tags are rules of thumb and the move may have other causes.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any

from core.desk import breaking as B
from core.desk import facts as F
from core.desk import pins as PN
from core.desk.followup import FEED_CATCH_UP
from core.desk.models import NewsBoard, NewsCategory, NewsItem

#: The page shows this long a window by default.
WINDOW = timedelta(hours=24)
#: A headline this soon after a scheduled release (or Fed event) of its kind is "scheduled".
SCHEDULED_AFTER = timedelta(minutes=90)
SCHEDULED_BEFORE = timedelta(minutes=5)

CATEGORY_LABELS: dict[NewsCategory, str] = {
    "fed": "Fed & central banks", "economy": "Economy", "big_tech": "Big tech",
    "energy": "Energy", "geopolitics": "Geopolitics",
}

SOURCE_LABELS = {
    **B.SOURCE_NAMES,
    "cnbc_earnings": "CNBC", "fed_speeches": "Federal Reserve", "ecb_press": "ECB",
    "boe_news": "Bank of England", "boj_whatsnew": "Bank of Japan", "eia_today": "EIA",
    "energy_press": "US Energy Dept", "ofac_actions": "OFAC", "guardian_world": "Guardian",
    "npr_world": "NPR", "ec_press": "European Commission",
}
_CENTRAL_BANK_SOURCES = frozenset({"fed_press_monetary", "fed_press_all", "fed_speeches",
                                   "ecb_press", "boe_news", "boj_whatsnew"})


def _rx(text: str) -> re.Pattern[str]:
    return re.compile(text, re.IGNORECASE)


#: Checked in this order; the first match wins.
_RULES: tuple[tuple[NewsCategory, re.Pattern[str]], ...] = (
    ("fed", _rx(r"\b(fed|federal reserve|fomc|powell|central bank\w*|ecb|bank of (england|"
                r"japan)|boj|rate (cut|hike)s?|interest rates?|monetary policy|"
                r"treasury yields?)\b")),
    ("economy", _rx(r"\b(inflation|cpi|ppi|pce|consumer prices|payrolls?|jobs report|jobless|"
                    r"unemployment|gdp|recession|retail sales|pmi|ism|consumer (confidence|"
                    r"sentiment)|housing starts|home sales|economy|economic)\b")),
    ("big_tech", _rx(r"\b(apple|microsoft|nvidia|amazon|alphabet|google|meta|tesla|broadcom|"
                     r"amd|netflix|tsmc|sk hynix|samsung|intel|openai|semiconductors?|chips?|"
                     r"chipmakers?|nasdaq|big tech|ai)\b")),
    ("energy", _rx(r"\b(oil|crude|brent|wti|opec\+?|natural gas|gasoline|refiner\w*|"
                   r"barrels?)\b")),
    ("geopolitics", _rx(r"\b(war|missiles?|invasion|invades?|sanctions?|tariffs?|trade (deal|"
                        r"war|talks)|china|taiwan|iran|israel|russia|ukraine|ceasefire|"
                        r"nuclear|strait of hormuz|red sea|north korea|government shutdown|"
                        r"debt ceiling|debt default)\b")),
)

#: Event codes behind each category, for "scheduled".
_SCHEDULED_KINDS: dict[NewsCategory, str] = {"fed": "fed", "economy": "economic"}


def categorise(headline: str, source: str) -> NewsCategory | None:
    """The headline's market-moving category, or None (hidden behind a count)."""
    if source in _CENTRAL_BANK_SOURCES:
        return "fed"
    for category, pattern in _RULES:
        if pattern.search(headline):
            return category
    if source == "cnbc_tech":
        return "big_tech"
    if source in ("eia_today", "energy_press"):
        return "energy"
    return None


def _scheduled(category: NewsCategory | None, at: datetime,
               slots: Sequence[F.ReleaseSlot]) -> bool:
    kind = _SCHEDULED_KINDS.get(category) if category else None
    if kind is None:
        return False
    return any(slot.at - SCHEDULED_BEFORE <= at <= slot.at + SCHEDULED_AFTER
               and (slot.is_fed if kind == "fed" else not slot.is_fed and not slot.speakers_only)
               for slot in slots)


#: Quotes read at once for the window; past this the store is asked per headline.
PRELOAD_LIMIT = 100_000


def preloaded(history: F.HistoryFn, symbol: str, *, start: datetime, end: datetime,
              ) -> F.HistoryFn:
    """``history`` with one market's quotes in ``[start, end]`` read once.

    Answers like the store (the newest ``limit`` rows in range, oldest first),
    so one News request reads NQ once rather than twice per headline.
    """
    rows = list(history(symbol, start=start, end=end, limit=PRELOAD_LIMIT))
    if len(rows) >= PRELOAD_LIMIT:
        return history
    stamps = [r.timestamp for r in rows]

    def read(sym: str, *, start: datetime | None = None, end: datetime | None = None,
             limit: int = 500) -> Sequence[Any]:
        if (sym != symbol or start is None or end is None
                or start < stamps_start or end > stamps_end):
            return history(sym, start=start, end=end, limit=limit)
        chosen = rows[bisect_left(stamps, start):bisect_right(stamps, end)]
        return chosen[-limit:]

    stamps_start, stamps_end = start, end
    return read


def items(news: Iterable[Any], events: Iterable[Any], history: F.HistoryFn, *,
          now: datetime, start: datetime, alert_ids: Iterable[str] = ()) -> list[NewsItem]:
    """Headlines published in ``[start, now]``, newest first, one per title."""
    alerts = set(alert_ids)
    history = preloaded(history, "NQ", start=start - timedelta(minutes=30), end=now)
    slots = F.release_slots(events, start=start - SCHEDULED_AFTER, end=now + SCHEDULED_BEFORE,
                            min_importance="medium")
    seen: set[str] = set()
    found: list[NewsItem] = []
    for event in sorted(news, key=lambda e: e.published_at or e.received_at, reverse=True):
        at = event.published_at or event.received_at
        if at is None or not (start <= at <= now):
            continue
        title = " ".join(event.headline.split())[:300]
        key = title.casefold()
        if not title or key in seen:
            continue
        seen.add(key)
        category = categorise(title, event.source)
        status = "unmeasured"
        move = None
        if category is not None:
            if now < at + PN.REACTION + FEED_CATCH_UP:
                status = "waiting"
            else:
                move = PN.nq_reaction(history, at, now)
                status = "measured" if move is not None else "unavailable"
        found.append(NewsItem(
            id=event.id, at=at, source=event.source,
            source_label=SOURCE_LABELS.get(event.source, event.source.replace("_", " ")),
            headline=title, url=getattr(event, "source_url", None),
            category=category, category_label=CATEGORY_LABELS[category] if category else None,
            scheduled=_scheduled(category, at, slots),
            nq_15m=move.change if move is not None else None,
            nq_15m_text=f"NQ {move.text} in the next 15 min" if move is not None else None,
            nq_15m_status=status, alert=event.id in alerts,
        ))
    return found


def board(found: list[NewsItem], alerts: list[Any], *, now: datetime, start: datetime,
          ) -> NewsBoard:
    hidden = sum(1 for i in found if i.category is None)
    return NewsBoard(generated_at=now, start=start, items=found, hidden=hidden, alerts=alerts)


__all__ = ["CATEGORY_LABELS", "WINDOW", "board", "categorise", "items", "preloaded"]
