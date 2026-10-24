"""Deterministic entity, instrument, and event-type extraction.

This is cheap pre-processing performed *before* any Phase 6 classifier runs. It
uses fixed alias tables and word-boundary regular expressions only. There is no
LLM call, no fuzzy inference, and no attempt to judge relevance or materiality.

Output is intentionally conservative: a match requires an explicit alias, case is
normalized for display, and instruments are restricted to the canonical universe.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from core.domain.instruments import INSTRUMENTS
from core.domain.news import EntityKind, EntityRef

#: Canonical instrument -> alias phrases. Matching is word-boundary and
#: case-insensitive. Order within a tuple does not matter; aliases are checked
#: longest-first so "S&P 500" wins over a bare ticker.
INSTRUMENT_ALIASES: dict[str, tuple[str, ...]] = {
    "NQ": ("nasdaq-100", "nasdaq 100", "e-mini nasdaq", "nasdaq futures", "nq futures", "nasdaq"),
    "ES": ("s&p 500", "s&p500", "e-mini s&p", "s&p futures", "es futures", "spx"),
    "US10Y": (
        "10-year treasury",
        "10 year treasury",
        "10-year yield",
        "10-year note",
        "10y yield",
        "10-year",
    ),
    "US2Y": (
        "2-year treasury",
        "2 year treasury",
        "2-year yield",
        "2-year note",
        "2y yield",
        "2-year",
    ),
    "DXY": ("us dollar index", "dollar index", "dxy"),
    "VIX": ("vix", "volatility index"),
    "VXN": ("vxn", "nasdaq volatility index"),
    "WTI": (
        "wti",
        "west texas intermediate",
        "crude oil",
        "crude futures",
        "wti crude",
        "oil prices",
    ),
    "GOLD": ("gold price", "gold prices", "gold futures", "bullion", "gold"),
    "BTC": ("bitcoin", "btc"),
    "NVDA": ("nvidia", "nvda"),
    "MSFT": ("microsoft", "msft"),
    "AAPL": ("apple", "aapl"),
    "AMZN": ("amazon", "amzn"),
    "META": ("meta platforms", "meta", "facebook"),
    "GOOGL": ("alphabet", "google", "googl"),
    "AVGO": ("broadcom", "avgo"),
    "TSLA": ("tesla", "tsla"),
}

#: Country/region aliases -> canonical short id (uppercase ISO-like).
COUNTRY_ALIASES: dict[str, tuple[str, ...]] = {
    "US": ("united states", "u.s.", "us ", "america", "washington", "federal reserve"),
    "CN": ("china", "chinese", "beijing", "prc"),
    "JP": ("japan", "japanese", "tokyo"),
    "GB": ("united kingdom", "britain", "british", "uk ", "london", "england"),
    "EU": ("european union", "eurozone", "euro area", "brussels", "ecb"),
    "DE": ("germany", "german", "berlin"),
    "FR": ("france", "french", "paris"),
    "RU": ("russia", "russian", "moscow", "kremlin"),
    "UA": ("ukraine", "ukrainian", "kyiv"),
    "IR": ("iran", "iranian", "tehran"),
    "IL": ("israel", "israeli", "jerusalem"),
    "SA": ("saudi arabia", "saudi", "riyadh"),
    "IN": ("india", "indian", "delhi"),
    "CA": ("canada", "canadian", "ottawa"),
    "AU": ("australia", "australian", "sydney"),
    "KR": ("south korea", "korean", "seoul"),
    "TW": ("taiwan", "taiwanese", "taipei"),
    "CH": ("switzerland", "swiss", "zurich"),
    "BR": ("brazil", "brazilian", "brasilia"),
}

#: Region aliases -> canonical region id.
REGION_ALIASES: dict[str, tuple[str, ...]] = {
    "MIDDLE_EAST": ("middle east", "gulf region", "persian gulf", "red sea"),
    "ASIA_PACIFIC": ("asia-pacific", "asia pacific", "indo-pacific"),
    "EUROPE": ("europe", "european"),
    "LATAM": ("latin america", "south america"),
    "AFRICA": ("africa",),
    "NORTH_AMERICA": ("north america",),
}

#: Institution aliases -> (canonical id, display label, optional country).
INSTITUTION_ALIASES: tuple[tuple[str, str, str, str | None], ...] = (
    ("federal reserve", "FED", "Federal Reserve", "US"),
    ("the fed", "FED", "Federal Reserve", "US"),
    ("fomc", "FOMC", "FOMC", "US"),
    ("ecb", "ECB", "European Central Bank", "EU"),
    ("european central bank", "ECB", "European Central Bank", "EU"),
    ("bank of england", "BOE", "Bank of England", "GB"),
    ("boe", "BOE", "Bank of England", "GB"),
    ("bank of japan", "BOJ", "Bank of Japan", "JP"),
    ("boj", "BOJ", "Bank of Japan", "JP"),
    ("us treasury", "UST", "US Treasury", "US"),
    ("treasury department", "UST", "US Treasury", "US"),
    ("opec+", "OPEC_PLUS", "OPEC+", None),
    ("opec", "OPEC", "OPEC", None),
    ("international monetary fund", "IMF", "IMF", None),
    ("imf", "IMF", "IMF", None),
    ("world bank", "WORLD_BANK", "World Bank", None),
    ("european commission", "EC", "European Commission", "EU"),
    ("european union", "EU_COUNCIL", "European Union", "EU"),
    ("bank of canada", "BOC", "Bank of Canada", "CA"),
    ("reserve bank of australia", "RBA", "Reserve Bank of Australia", "AU"),
    ("people's bank of china", "PBOC", "People's Bank of China", "CN"),
    ("pboc", "PBOC", "People's Bank of China", "CN"),
)

#: Deterministic event-type keyword rules. First match wins. Each needle is
#: matched as a whole word/phrase; a trailing ``*`` allows a word prefix so that
#: e.g. ``"sanction*"`` matches "sanction" and "sanctions" but ``"port"`` never
#: matches "Portugal".
_EVENT_TYPE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "rate_decision",
        (
            "rate decision",
            "interest rate decision",
            "raises rates",
            "cuts rates",
            "hikes rates",
            "rate hike",
            "rate cut",
        ),
    ),
    ("minutes", ("minutes", "meeting minutes")),
    ("testimony", ("testimony", "testifies", "testified")),
    ("speech", ("speech*", "remarks", "speaks", "address*", "keynote")),
    ("statement", ("statement*", "press conference")),
    (
        "monetary_policy",
        ("monetary policy", "fomc", "quantitative", "policy rate", "federal funds"),
    ),
    ("sanctions", ("sanction*", "blacklist*", "designat*")),
    ("tariffs", ("tariff*", "import duty", "levies", "levy")),
    ("trade", ("trade deal", "trade agreement", "trade talks", "trade war", "export control*")),
    (
        "military_conflict",
        (
            "missile*",
            "airstrike*",
            "strikes",
            "strike",
            "attack*",
            "invasion",
            "troops",
            "military",
            "war",
            "ceasefire",
            "drone*",
        ),
    ),
    ("diplomacy", ("diplomat*", "summit", "talks", "negotiat*", "accord")),
    (
        "shipping_disruption",
        ("shipping", "port", "ports", "canal", "strait", "vessel*", "tanker*", "blockade"),
    ),
    (
        "energy_supply",
        ("oil supply", "oil production", "pipeline*", "refiner*", "opec", "natural gas", "lng"),
    ),
    ("supply_chain", ("supply chain", "semiconductor*", "chip*", "shortage*")),
    ("election", ("election*", "poll", "polls", "vote*", "referendum")),
    (
        "economic_release",
        ("cpi", "pce", "payroll*", "gdp", "retail sales", "unemployment", "jobless", "ism", "pmi"),
    ),
    ("inflation", ("inflation*", "cpi", "price index", "deflation")),
    ("employment", ("payroll*", "unemployment", "jobs report", "nonfarm", "jobless claims")),
    ("growth", ("gdp", "growth", "recession", "retail sales")),
    ("housing", ("housing starts", "building permits", "home sales", "mortgage*")),
    (
        "corporate",
        ("earnings", "revenue*", "guidance", "acquisition*", "merger*", "buyback*", "ipo"),
    ),
)


def _event_type_pattern(needle: str) -> re.Pattern[str]:
    prefix = needle.endswith("*")
    stem = needle[:-1] if prefix else needle
    escaped = re.escape(stem)
    return re.compile(r"\b" + escaped + (r"\w*" if prefix else "") + r"\b", re.IGNORECASE)


_EVENT_TYPE_PATTERNS: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = tuple(
    (event_type, tuple(_event_type_pattern(needle) for needle in needles))
    for event_type, needles in _EVENT_TYPE_RULES
)


def _alias_pattern(alias: str) -> re.Pattern[str]:
    escaped = re.escape(alias.strip())
    if alias != alias.strip():
        escaped = r"\b" + escaped + r"\b"
    elif alias and alias[-1].isspace():
        escaped = r"\b" + escaped
    elif alias and alias[0].isalnum():
        escaped = r"\b" + escaped + r"\b"
    return re.compile(escaped, re.IGNORECASE)


# Precompile patterns once. Longer aliases are matched first to reduce partial
# matches (e.g. "nasdaq-100" before the bare "nasdaq").
_INSTRUMENT_PATTERNS = {
    symbol: tuple(
        (alias, _alias_pattern(alias)) for alias in sorted(aliases, key=len, reverse=True)
    )
    for symbol, aliases in INSTRUMENT_ALIASES.items()
}
_COUNTRY_PATTERNS = {
    code: tuple((alias, _alias_pattern(alias)) for alias in aliases)
    for code, aliases in COUNTRY_ALIASES.items()
}
_REGION_PATTERNS = {
    code: tuple((alias, _alias_pattern(alias)) for alias in aliases)
    for code, aliases in REGION_ALIASES.items()
}
_INSTITUTION_PATTERNS = tuple(
    (alias, _alias_pattern(alias), institution_id, label, country)
    for alias, institution_id, label, country in INSTITUTION_ALIASES
)
_BARE_TICKER_PATTERN = re.compile(r"\b([A-Z]{1,5})\b")
_TICKER_SYMBOLS = frozenset(INSTRUMENTS)


def extract_instruments(text: str) -> list[str]:
    """Canonical instrument symbols mentioned in ``text``, universe-ordered."""
    found: set[str] = set()
    for symbol, patterns in _INSTRUMENT_PATTERNS.items():
        if any(pattern.search(text) for _alias, pattern in patterns):
            found.add(symbol)
    for match in _BARE_TICKER_PATTERN.finditer(text):
        ticker = match.group(1)
        if ticker in _TICKER_SYMBOLS and ticker not in {"US", "EU", "UK"}:
            found.add(ticker)
    return [symbol for symbol in INSTRUMENTS if symbol in found]


def extract_countries(text: str) -> list[str]:
    """Canonical country ids mentioned in ``text``."""
    found = [
        code
        for code, patterns in _COUNTRY_PATTERNS.items()
        if any(p.search(text) for _a, p in patterns)
    ]
    return sorted(set(found))


def extract_regions(text: str) -> list[str]:
    """Canonical region ids mentioned in ``text``."""
    found = [
        code
        for code, patterns in _REGION_PATTERNS.items()
        if any(p.search(text) for _a, p in patterns)
    ]
    return sorted(set(found))


def extract_institutions(text: str) -> list[tuple[str, str, str | None]]:
    """Institution id, label, and implied country for matches in ``text``."""
    result: list[tuple[str, str, str | None]] = []
    seen: set[str] = set()
    for _alias, pattern, institution_id, label, country in _INSTITUTION_PATTERNS:
        if institution_id in seen:
            continue
        if pattern.search(text):
            seen.add(institution_id)
            result.append((institution_id, label, country))
    return result


def classify_event_type(*texts: str | None) -> str | None:
    """Deterministic event type from headline/body, or ``None``.

    ``None`` means "no deterministic category"; it is not a judgement about
    importance. Phase 6 may classify further.
    """
    haystack = " ".join(text for text in texts if text)
    if not haystack.strip():
        return None
    for event_type, patterns in _EVENT_TYPE_PATTERNS:
        if any(pattern.search(haystack) for pattern in patterns):
            return event_type
    return None


def extract_entities(
    *texts: str | None,
    extra_countries: Iterable[str] = (),
    extra_institutions: Iterable[tuple[str, str, str | None]] = (),
) -> list[EntityRef]:
    """All deterministic entities for the given texts.

    Instruments are returned as ``EntityKind.INSTRUMENT``; countries/regions as
    their own kinds; institutions and their implied country as separate refs.
    """
    haystack = " ".join(text for text in texts if text)
    entities: list[EntityRef] = []
    seen: set[tuple[EntityKind, str]] = set()

    def add(kind: EntityKind, entity_id: str, label: str) -> None:
        key = (kind, entity_id)
        if entity_id and key not in seen:
            seen.add(key)
            entities.append(EntityRef(kind=kind, id=entity_id, label=label))

    for symbol in extract_instruments(haystack):
        add(EntityKind.INSTRUMENT, symbol, INSTRUMENTS[symbol].display_name)
    for code in extract_countries(haystack):
        add(EntityKind.COUNTRY, code, code)
    for code in extract_regions(haystack):
        add(EntityKind.REGION, code, code)
    for institution_id, label, country in extract_institutions(haystack):
        add(EntityKind.INSTITUTION, institution_id, label)
        if country:
            add(EntityKind.COUNTRY, country, country)
    for code in extra_countries:
        add(EntityKind.COUNTRY, code, code)
    for institution_id, label, country in extra_institutions:
        add(EntityKind.INSTITUTION, institution_id, label)
        if country:
            add(EntityKind.COUNTRY, country, country)
    return entities


def filter_instruments(symbols: Sequence[str]) -> list[str]:
    """Keep only canonical symbols, universe-ordered and deduplicated."""
    wanted = set(symbols)
    return [symbol for symbol in INSTRUMENTS if symbol in wanted]


__all__ = [
    "COUNTRY_ALIASES",
    "INSTRUMENT_ALIASES",
    "classify_event_type",
    "extract_countries",
    "extract_entities",
    "extract_institutions",
    "extract_instruments",
    "extract_regions",
    "filter_instruments",
]
