"""Choose what a move or ask note is told about why a market moved. No AI.

Reports come from the stored news feeds and from coverage read when the note is
written. Fixed per-instrument word rules keep the ones about this market and its
drivers, favour those that say why it moved in the note's direction, and drop old
items, how-to pieces and near-repeats. The writer gets the survivors, numbered, as
``why_evidence``; it cites those numbers and the code resolves them to sources, so
a citation can't be made up (:func:`cited`).

The rules only decide what to show. Whether a report's reason is right stays a
judgment, which the note labels ``reported``, ``likely`` or ``unknown``.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from core.desk.models import Headline, NoteDraft

#: A report this old can't explain today's move.
LOOKBACK = timedelta(hours=24)
RECENT = timedelta(hours=3)
MAX_ITEMS = 8
MAX_PER_SOURCE = 3
#: The strongest reports get a passage from the article itself.
EXCERPT_ITEMS = 3
MIN_SCORE = 4
SUMMARY_CHARS = 300
EXCERPT_CHARS = 500
#: Two titles sharing this much of their words are one report.
SAME_REPORT = 0.7


def _rx(text: str) -> re.Pattern[str]:
    return re.compile(text, re.IGNORECASE)


_UP = (r"rise[sn]?|rising|rose|jump(?:s|ed|ing)?|surg(?:e|es|ed|ing)|rall(?:y|ies|ied|ying)|"
       r"climb(?:s|ed|ing)?|gain(?:s|ed|ing)?|soar(?:s|ed|ing)?|rebound(?:s|ed|ing)?|"
       r"bounc(?:e|es|ed|ing)|advanc(?:e|es|ed|ing)|spik(?:e|es|ed|ing)|higher")
_DOWN = (r"fall(?:s|ing|en)?|fell|drop(?:s|ped|ping)?|slid(?:e|es|ing)?|tumbl(?:e|es|ed|ing)|"
         r"sink(?:s|ing)?|sank|plung(?:e|es|ed|ing)|slump(?:s|ed|ing)?|crater(?:s|ed|ing)?|"
         r"sell-?offs?|rout|declin(?:e|es|ed|ing)|slip(?:s|ped|ping)?|los(?:e|es|ing)|"
         r"weak(?:er|ens?|ened)|retreat(?:s|ed|ing)?|lower")
_MOVED_UP = _rx(rf"\b(?:{_UP})\b")
_MOVED_DOWN = _rx(rf"\b(?:{_DOWN})\b")
#: Where a headline's main clause ends and its reason begins.
_CLAUSE_BREAK = _rx(r"\b(?:as|after|amid|on|because|following|due to)\b")
#: Words that tie a move to a reason, plus "rotation" and similar named causes.
_BECAUSE = _rx(r"\b(?:as|after|amid|on|because|following|due to|why|behind|driven|led by|"
               r"leading|drag(?:s|ged)?|fuel(?:s|ed)|rotation|weighs?|hit|hits)\b")
_NOT_NEWS = _rx(r"\b(?:prices? today|live (?:prices?|updates?|blog)|how to|should you|"
                r"stocks? to (?:buy|sell|watch)|best \w+ stocks?|top \d+|\d+ things|"
                r"price (?:forecast|prediction)|buy the dip|dividend)\b")
_BOILERPLATE = _rx(r"\b(?:subscribe|sign up|newsletter|click here|read more|cookie|"
                   r"terms of (?:service|use)|all rights reserved|follow us)\b")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[\"'A-Z(])")
#: Page furniture: live quote widgets ("TSLA -9.50%") are not the article's facts,
#: and they change after it is published.
_PAGE_NOISE = re.compile(r"Oops, something went wrong|Watch CBS News|"
                         r"Updated on: [^/]{5,30}/[^/]{5,20}/ CBS News|"
                         r"[\^A-Z][A-Z0-9.=^-]{0,7}\s+[+\-−]\d+(?:\.\d+)?%")
#: Publishers whose reports rank ahead of unknown sites, all else equal.
KNOWN_PUBLISHERS = frozenset({
    "reuters", "bloomberg", "bloomberg.com", "wsj", "the wall street journal", "cnbc", "ap news",
    "associated press", "financial times", "marketwatch", "barron's", "yahoo finance",
    "investing.com", "seeking alpha", "investinglive", "kitco", "fxstreet", "federal reserve",
    "cbs news", "cnn", "fox business",
})


@dataclass(frozen=True)
class Terms:
    #: Names the market itself (or the market it follows).
    subject: re.Pattern[str]
    #: What usually moves it.
    drivers: re.Pattern[str]
    #: VIX usually moves against stocks, so "stocks fall" fits a rising VIX:
    #: a report's direction is only compared when it names the market directly.
    direct: re.Pattern[str] | None = None


_STOCK_DRIVERS = (r"\b(?:nvidia|microsoft|apple|amazon|meta|alphabet|google|tesla|broadcom|amd|"
                  r"qualcomm|arm|micron|tsmc|intel|openai|chips?|chipmakers?|semiconductors?|"
                  r"ai (?:stocks|trade|spending|bubble)|data centers?|earnings|guidance|"
                  r"fed|federal reserve|rate (?:hikes?|cuts?)|treasury yields?|inflation|"
                  r"payrolls|tariffs?|midterms?|election|nikkei|kospi|asian (?:stocks|markets))\b")
_STOCKS = r"stocks|equities|wall street|stock market|stock futures"
_RATES_DRIVERS = (r"\b(?:fed|federal reserve|fomc|powell|warsh|rate (?:hikes?|cuts?)|inflation|"
                  r"cpi|ppi|pce|payrolls|jobs report|jobless claims|auctions?|treasury supply|"
                  r"deficits?|flight to (?:safety|quality)|safe haven)\b")

TERMS: Mapping[str, Terms] = {
    "NQ": Terms(_rx(rf"\b(?:nasdaq|qqq|tech stocks|technology stocks|chip stocks|"
                    rf"semiconductor stocks|big tech|megacaps?|{_STOCKS})\b"),
                _rx(_STOCK_DRIVERS)),
    "ES": Terms(_rx(rf"\b(?:s&p(?: 500)?|spx|spy|{_STOCKS})\b"), _rx(_STOCK_DRIVERS)),
    "YM": Terms(_rx(rf"\b(?:dow(?: jones)?|dow futures|djia|blue[- ]chips?|{_STOCKS})\b"),
                _rx(_STOCK_DRIVERS)),
    "GOLD": Terms(_rx(r"\b(?:gold|bullion|gld)\b"),
                  _rx(rf"{_RATES_DRIVERS}|\b(?:dollar|yields?|safe[- ]haven|central banks?|"
                      r"geopolitic\w*|war)\b")),
    "WTI": Terms(_rx(r"\b(?:oil|crude|wti|brent)\b"),
                 _rx(r"\b(?:opec\+?|iran|hormuz|sanctions?|inventor(?:y|ies)|stockpiles?|eia|"
                     r"refiner\w*|shale|supply|demand|dollar|china|hurricane|war|ceasefire)\b")),
    "DXY": Terms(_rx(r"\b(?:dollar(?: index)?|dxy|greenback|euro|yen|sterling|pound)\b"),
                 _rx(rf"{_RATES_DRIVERS}|\b(?:yields?|ecb|boj|bank of (?:england|japan))\b")),
    "US2Y": Terms(_rx(r"\b(?:treasur(?:y|ies)|2-year|two-year|bond (?:market|yields?)|"
                      r"yields?)\b"),
                  _rx(_RATES_DRIVERS)),
    "US10Y": Terms(_rx(r"\b(?:treasur(?:y|ies)|10-year|ten-year|bond (?:market|yields?)|"
                       r"yields?)\b"),
                   _rx(_RATES_DRIVERS)),
    "VIX": Terms(_rx(rf"\b(?:vix|volatility index|fear gauge|{_STOCKS}|s&p(?: 500)?|nasdaq)\b"),
                 _rx(rf"{_RATES_DRIVERS}|\b(?:volatility|hedging|options|geopolitic\w*|war)\b"),
                 direct=_rx(r"\b(?:vix|volatility index|fear gauge)\b")),
}


@dataclass(frozen=True)
class Candidate:
    """A report before ranking, from either kind of source."""

    source: str
    headline: str
    published_at: datetime
    url: str | None = None
    summary: str | None = None
    #: The feed can re-date an older story, so ``published_at`` is not exact.
    time_approximate: bool = False


def _direction(text: str) -> int:
    """+1 for words of rising only, -1 for falling only, 0 for neither or both.

    The main clause decides: in "Gold climbs as falling yields support it", gold
    climbed. A clause with no such word falls back to the whole text.
    """
    def said(part: str) -> int:
        return bool(_MOVED_UP.search(part)) - bool(_MOVED_DOWN.search(part))
    main = _CLAUSE_BREAK.split(text, maxsplit=1)[0]
    return said(main) or said(text)


def score(item: Candidate, symbol: str, direction: int, now: datetime) -> int:
    """How much this report may explain ``symbol`` moving in ``direction`` (+1, -1, 0)."""
    terms = TERMS[symbol]
    title = item.headline
    named = bool(terms.subject.search(title))
    driver = bool(terms.drivers.search(title))
    # Move words alone are about some other market; a how-to is not a report.
    if not (named or driver) or _NOT_NEWS.search(title):
        return 0
    moved = bool(_MOVED_UP.search(title) or _MOVED_DOWN.search(title))
    total = 0
    if named:
        total += 4
    if driver:
        total += 2
    if moved:
        total += 2
        if _BECAUSE.search(title):
            total += 2
    said = _direction(title)
    compare = terms.direct.search(title) if terms.direct is not None else named
    if direction and said and compare:
        total += 1 if said == direction else -4
    # A re-dated story mustn't get credit for looking new.
    if now - item.published_at <= RECENT and not item.time_approximate:
        total += 1
    if item.source.casefold() in KNOWN_PUBLISHERS:
        total += 2
    return total


def _words(title: str) -> set[str]:
    return set(re.findall(r"[a-z0-9&']+", title.casefold()))


def _same_report(a: set[str], b: set[str]) -> bool:
    return bool(a and b) and len(a & b) / len(a | b) >= SAME_REPORT


def rank(candidates: Iterable[Candidate], symbol: str, *, direction: int, now: datetime,
         limit: int = MAX_ITEMS) -> list[Candidate]:
    """The reports worth showing, strongest first. The order never depends on
    input order, and nothing published after ``now`` or before ``LOOKBACK`` counts."""
    scored = [(score(c, symbol, direction, now), c) for c in candidates
              if now - LOOKBACK <= c.published_at <= now]
    scored = [row for row in scored if row[0] >= MIN_SCORE]
    scored.sort(key=lambda row: (-row[0], -row[1].published_at.timestamp(), row[1].source,
                                 row[1].headline, row[1].url or ""))
    chosen: list[Candidate] = []
    seen: list[set[str]] = []
    per_source: dict[str, int] = {}
    for _, item in scored:
        words = _words(item.headline)
        if any(_same_report(words, other) for other in seen):
            continue
        if per_source.get(item.source, 0) >= MAX_PER_SOURCE:
            continue
        seen.append(words)
        per_source[item.source] = per_source.get(item.source, 0) + 1
        chosen.append(item)
        if len(chosen) == limit:
            break
    return chosen


def excerpt(text: str | None, symbol: str, *, limit: int = EXCERPT_CHARS) -> str | None:
    """Sentences from an article about this market or its drivers and how they
    moved, in article order; ``None`` when none qualify."""
    if not text:
        return None
    terms = TERMS[symbol]
    kept: list[str] = []
    used = 0
    for sentence in _SENTENCE.split(" ".join(_PAGE_NOISE.sub(" ", text).split())):
        sentence = sentence.strip()
        if not 30 <= len(sentence) <= 400 or _BOILERPLATE.search(sentence):
            continue
        about = terms.subject.search(sentence) or terms.drivers.search(sentence)
        moved = _MOVED_UP.search(sentence) or _MOVED_DOWN.search(sentence)
        if not about or not (moved or _BECAUSE.search(sentence)):
            continue
        if used + len(sentence) > limit:
            break
        kept.append(sentence)
        used += len(sentence) + 1
    return " ".join(kept) or None


def _shorten(text: str | None, limit: int) -> str | None:
    if not text or len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


async def evidence_facts(chosen: Sequence[Candidate], *, symbol: str, unavailable: Sequence[str],
                         article_text: Callable[[str], Awaitable[str | None]] | None,
                         readable: Callable[[str | None], bool] = bool,
                         when: Callable[[datetime], str], now: datetime) -> dict[str, Any]:
    """The ``why_evidence`` fact: numbered reports, from ``now - LOOKBACK`` to ``now``,
    with a passage from the strongest ones whose pages can be read."""
    passages: dict[int, str | None] = {}
    if article_text is not None:
        wanted = [(i, c.url) for i, c in enumerate(chosen) if c.url and readable(c.url)]
        wanted = wanted[:EXCERPT_ITEMS]
        pages = await asyncio.gather(*(article_text(url) for _, url in wanted),
                                     return_exceptions=True)
        for (index, _), page in zip(wanted, pages, strict=True):
            # A passage is never worth losing a note.
            passages[index] = excerpt(page, symbol) if isinstance(page, str) else None
    items: list[dict[str, Any]] = []
    for index, item in enumerate(chosen):
        row: dict[str, Any] = {"id": index + 1, "time": when(item.published_at),
                               "source": item.source, "headline": item.headline}
        summary = _shorten(item.summary, SUMMARY_CHARS)
        if item.time_approximate:
            row["time_is_approximate"] = True
        if summary and summary.casefold() != item.headline.casefold():
            row["summary"] = summary
        if passages.get(index):
            row["excerpt"] = passages[index]
        items.append(row)
    return {"searched_from": when(now - LOOKBACK), "searched_to": when(now), "items": items,
            "feeds_unavailable": list(unavailable)}


def cited(draft: NoteDraft | None, chosen: Sequence[Candidate]) -> list[Headline]:
    """The sources behind a draft's ``why``, from the numbers it cites."""
    if draft is None or draft.why is None:
        return []
    by_id = {index + 1: item for index, item in enumerate(chosen)}
    # An approximate time isn't shown as if it were when the report came out.
    return [Headline(source=by_id[i].source, title=by_id[i].headline,
                     published_at=None if by_id[i].time_approximate else by_id[i].published_at,
                     url=by_id[i].url)
            for i in dict.fromkeys(draft.why.evidence) if i in by_id]


__all__ = ["Candidate", "KNOWN_PUBLISHERS", "LOOKBACK", "TERMS", "cited", "evidence_facts",
           "excerpt", "rank", "score"]
