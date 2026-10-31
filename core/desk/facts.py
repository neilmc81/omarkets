"""Deterministic facts for desk notes.

Everything a note states as a number is computed here: release grouping,
rounding-aware beat/miss verdicts, price and yield moves, the quote strip and
the bond-market view. No language model is involved.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo

from core.analysis.cross_market import DEFAULT_TOLERANCE_PCT, compare_nq_es
from core.desk import explainers as X
from core.desk.models import Figure, Move, Quote, RatesView, Surprise, UpcomingItem, YieldLine
from core.domain.sessions import volatility_index_open

#: Markets measured after a release or move, in display order.
REACTION_SYMBOLS: tuple[str, ...] = ("NQ", "ES", "US2Y", "US10Y", "DXY", "VIX")
#: Markets on the Today strip.
STRIP_SYMBOLS: tuple[str, ...] = ("NQ", "ES", "US10Y", "US2Y", "DXY", "VIX", "WTI", "GOLD")
YIELD_SYMBOLS = frozenset({"US2Y", "US10Y"})
_NEW_YORK = ZoneInfo("America/New_York")
#: Cash indexes computed only in Cboe's hours (see ``volatility_index_open``).
CASH_INDEXES = frozenset({"VIX", "VXN"})

LABELS: dict[str, str] = {
    "NQ": "Nasdaq 100 futures",
    "ES": "S&P 500 futures",
    "US2Y": "2-year yield",
    "US10Y": "10-year yield",
    "DXY": "US dollar",
    "VIX": "VIX fear gauge",
    "VXN": "Nasdaq fear gauge",
    "WTI": "Crude oil",
    "GOLD": "Gold",
    "BTC": "Bitcoin",
}

FED_CODES = frozenset({"FOMC", "FOMC_PRESS_CONFERENCE", "FOMC_MINUTES"})
#: Fed Board speeches and testimony: shown in "Coming up", never written up as a release.
SPEAKER_CODES = frozenset({"FED_SPEECH", "FED_TESTIMONY"})
_RANK = {"critical": 3, "high": 2, "medium": 1, "low": 0}

#: Codes whose figures are named from the explainer library. Kept to this set
#: because figure names key the playbook's history; other codes keep the
#: calendar's own name.
_NAMED_CODES = frozenset({
    "CPI", "CORE_CPI", "PPI", "CORE_PPI", "PCE", "PAYROLLS", "UNEMPLOYMENT",
    "INITIAL_CLAIMS", "JOLTS", "ADP", "RETAIL_SALES", "CORE_RETAIL_SALES", "GDP",
    "INDUSTRIAL_PRODUCTION", "ISM_MFG", "ISM_MFG_PRICES", "ISM_SERVICES",
    "EXISTING_HOME_SALES", "HOUSING_STARTS", "BUILDING_PERMITS", "FOMC",
    "FOMC_PRESS_CONFERENCE", "FOMC_MINUTES", "BEIGE_BOOK",
})


def friendly_name(code: str, fallback: str) -> str:
    entry = X.indicator(code) if code in _NAMED_CODES else None
    return entry.title if entry else fallback


def explainer(code: str) -> str | None:
    """What a calendar release is and why it matters, from the explainer library."""
    entry = X.indicator(code)
    return entry.text if entry else None


# --------------------------------------------------------------------------- numbers


def _decimals(value: float) -> int:
    text = f"{round(value, 4):.4f}".rstrip("0").rstrip(".")
    return len(text.split(".")[1]) if "." in text else 0


def reporting_decimals(unit: str, *refs: float | None) -> int:
    """Decimals a figure is officially reported at, inferred from its forecast/previous."""
    known = [r for r in refs if r is not None and isfinite(r)]
    if unit == "percent":
        return min(2, max([1, *(_decimals(r) for r in known)]))
    return min(2, max([0, *(_decimals(r) for r in known)]))


def _compare_round(value: float, decimals: int) -> float:
    magnitude = abs(value)
    if magnitude >= 1_000_000:
        return round(value, -4)  # e.g. 3.99M
    if magnitude >= 1_000:
        return round(value, -3)  # e.g. 199K
    return round(value, decimals)


def format_value(value: float | None, unit: str, decimals: int = 1) -> str:
    if value is None or not isfinite(value):
        return "—"
    magnitude = abs(value)
    if unit == "percent":
        return f"{value:.{decimals}f}%"
    if magnitude >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if magnitude >= 1_000:
        return f"{value / 1_000:.0f}K"
    return f"{value:.{decimals}f}"


def build_figure(
    name: str,
    unit: str,
    actual: float | None,
    forecast: float | None,
    previous: float | None,
    revised_previous: float | None = None,
) -> Figure:
    decimals = reporting_decimals(unit, forecast, previous)
    shown = None if actual is None else _compare_round(actual, decimals)
    if shown is None:
        verdict = "not_reported"
    elif forecast is None:
        verdict = "no_forecast"
    else:
        expected = _compare_round(forecast, decimals)
        verdict = "in_line" if shown == expected else ("above" if shown > expected else "below")
    revised, revision_text = _revision(unit, decimals, previous, revised_previous)
    return Figure(
        name=name,
        unit=unit,
        actual=shown,
        forecast=forecast,
        previous=previous,
        decimals=decimals,
        verdict=verdict,
        actual_text=format_value(shown, unit, decimals),
        forecast_text=format_value(forecast, unit, decimals),
        previous_text=format_value(previous, unit, decimals),
        revised_previous=revised,
        revised_previous_text=None if revised is None else format_value(revised, unit, decimals),
        revision_text=revision_text,
    )


def _shown(value: float, unit: str, decimals: int) -> float:
    """``value`` rounded exactly as ``format_value`` displays it."""
    if unit != "percent" and abs(value) >= 1_000_000:
        return round(value / 1_000_000, 2) * 1_000_000
    if unit != "percent" and abs(value) >= 1_000:
        return round(value / 1_000) * 1_000.0
    return round(value, decimals)


def _revision(unit: str, decimals: int, previous: float | None,
              revised: float | None) -> tuple[float | None, str | None]:
    """The revised previous value and "revised up/down by X", at official rounding.

    No revision (or one too small to show at the reported precision) gives
    ``(None, None)``; a revised value without the first-reported one gives no size.
    """
    if revised is None or not isfinite(revised):
        return None, None
    if previous is None or not isfinite(previous):
        return revised, None
    change = _shown(revised, unit, decimals) - _shown(previous, unit, decimals)
    if change == 0:
        return None, None
    size = abs(round(change, decimals))
    size_text = (f"{size:.{decimals}f} percentage points" if unit == "percent"
                 else format_value(size, unit, decimals))
    return revised, f"revised {'up' if change > 0 else 'down'} by {size_text}"


#: Past surprises compared against (most recent first), and the minimum needed.
SURPRISE_HISTORY = 12
SURPRISE_MIN_HISTORY = 5
#: How far back an indicator's history is looked for.
SURPRISE_LOOKBACK = timedelta(days=4 * 365)


def surprise_value(figure: Figure) -> float | None:
    """Actual minus forecast at the official reporting precision."""
    if figure.verdict not in ("above", "below", "in_line") or figure.forecast is None:
        return None
    value = figure.actual - _compare_round(figure.forecast, figure.decimals)
    return 0.0 if figure.verdict == "in_line" else round(value, figure.decimals)


def surprise_text(value: float, unit: str, decimals: int) -> str:
    sign = "+" if value > 0 else "−" if value < 0 else ""
    if unit == "percent":
        return f"{sign}{abs(value):.{decimals}f} pt"
    return sign + format_value(abs(value), unit, decimals)


def size_surprises(figures: Sequence[Figure], history: Iterable[Any], *,
                   before: datetime) -> list[Figure]:
    """Attach a :class:`Surprise` to each figure that has an actual and a forecast.

    The surprise is compared with the same figure's surprises at earlier
    releases (matched by name, e.g. "CPI m/m" and "CPI y/y" stay separate),
    using the same grouping and rounding as the note itself.
    """
    past: dict[str, list[float]] = {}
    for slot in sorted(release_slots(history, start=before - SURPRISE_LOOKBACK, end=before,
                                     min_importance="low"),
                       key=lambda s: s.at, reverse=True):
        for old in slot.figures:
            value = surprise_value(old)
            if value is not None:
                past.setdefault(old.name.casefold(), []).append(value)
    sized: list[Figure] = []
    for figure in figures:
        value = surprise_value(figure)
        if value is None:
            sized.append(figure)
            continue
        earlier = past.get(figure.name.casefold(), [])[:SURPRISE_HISTORY]
        count = len(earlier)
        direction = "above" if value > 0 else "below"
        larger: int | None = None
        if value == 0:
            size, text = "in_line", "In line with the forecast"
        elif count < SURPRISE_MIN_HISTORY:
            size = "not_enough_history"
            text = (f"Not enough history yet to size this surprise "
                    f"({count} earlier release{'s' if count != 1 else ''} stored)")
        else:
            larger = sum(1 for old in earlier if abs(old) < abs(value))
            share = larger / count
            size = "bigger" if share >= 0.8 else "smaller" if share <= 0.3 else "typical"
            words = {"bigger": "Bigger than usual", "smaller": "Smaller than usual",
                     "typical": "A typical-sized"}[size]
            text = (f"{words} surprise {direction} the forecast — larger than "
                    f"{larger} of the last {count}")
        sized.append(figure.model_copy(update={"surprise": Surprise(
            value=value, value_text=surprise_text(value, figure.unit, figure.decimals),
            size=size, history_count=count, larger_than=larger, text=text,
        )}))
    return sized


def signed(value: float, unit: str) -> str:
    if unit == "bp":
        return f"{value:+.1f}bp"
    return f"{value:+.2f}%"


# --------------------------------------------------------------------------- releases


@dataclass
class ReleaseSlot:
    """All calendar rows released at the same moment, merged into one story."""

    at: datetime
    codes: list[str]
    title: str
    importance: str
    figures: list[Figure] = field(default_factory=list)
    #: Where a Fed speech takes place, from the source's "Venue:" note.
    venue: str = ""

    @property
    def is_fed(self) -> bool:
        return any(code in FED_CODES for code in self.codes)

    @property
    def speakers_only(self) -> bool:
        return all(code in SPEAKER_CODES for code in self.codes)

    @property
    def primary_code(self) -> str:
        return self.codes[0]

    @property
    def key(self) -> str:
        return f"{self.at:%Y%m%dT%H%M}:{'+'.join(sorted(set(self.codes)))}"

    @property
    def complete(self) -> bool:
        return bool(self.figures) and all(f.actual is not None for f in self.figures)


def _value(item: Any, name: str):
    value = getattr(item, name, None)
    return getattr(value, "value", value)


def release_slots(
    events: Iterable[Any],
    *,
    start: datetime,
    end: datetime,
    min_importance: str = "high",
) -> list[ReleaseSlot]:
    """Group calendar rows into release moments between ``start`` and ``end``.

    Rows from different sources describing the same figure are merged; a slot is
    kept only when its most important row reaches ``min_importance``.
    """
    groups: dict[datetime, list[Any]] = {}
    for item in events:
        at = getattr(item, "scheduled_at", None)
        if at is None or not (start <= at < end):
            continue
        if getattr(item, "country", "US") != "US" or _value(item, "status") == "cancelled":
            continue
        groups.setdefault(at, []).append(item)

    slots: list[ReleaseSlot] = []
    for at, rows in sorted(groups.items()):
        rows.sort(key=lambda r: -_RANK.get(_value(r, "importance"), 0))
        importance = _value(rows[0], "importance")
        if _RANK.get(importance, 0) < _RANK[min_importance]:
            continue
        codes: list[str] = []
        for row in rows:
            if row.event_code not in codes:
                codes.append(row.event_code)
        merged: dict[str, dict[str, Any]] = {}
        for row in rows:
            if row.unit == "release" or (
                row.actual is None and row.consensus is None and row.previous is None
            ):
                continue
            slot = merged.setdefault(
                row.name.casefold(),
                {"name": row.name, "unit": row.unit, "actual": None,
                 "forecast": None, "previous": None, "revised_previous": None,
                 "rank": _RANK.get(_value(row, "importance"), 0)},
            )
            for target, source in (("actual", "actual"), ("forecast", "consensus"),
                                   ("previous", "previous")):
                if slot[target] is None and getattr(row, source) is not None:
                    slot[target] = getattr(row, source)
            # A revision is only meaningful against the same source's first report.
            revised = getattr(row, "revised_previous", None)
            if slot["revised_previous"] is None and revised is not None:
                slot["revised_previous"] = revised
                if row.previous is not None:
                    slot["previous"] = row.previous
        figures = [
            build_figure(m["name"], m["unit"], m["actual"], m["forecast"], m["previous"],
                         m["revised_previous"])
            for m in sorted(merged.values(), key=lambda m: -m["rank"])
        ]
        names: list[str] = []
        for code in codes:
            name = friendly_name(code, next(r.name for r in rows if r.event_code == code))
            if name not in names:
                names.append(name)
        venue = next((note.removeprefix("Venue: ") for row in rows
                      for note in (getattr(row, "notes", None) or [])
                      if isinstance(note, str) and note.startswith("Venue: ")), "")
        slots.append(ReleaseSlot(
            at=at, codes=codes, title=" & ".join(names[:3]),
            importance=importance, figures=figures, venue=venue,
        ))
    return slots


def economic_upcoming(
    events: Iterable[Any], *, now: datetime, until: datetime
) -> list[UpcomingItem]:
    items: list[UpcomingItem] = []
    for slot in release_slots(events, start=now, end=until, min_importance="medium"):
        detail = ", ".join(
            f"{f.name}: forecast {f.forecast_text}" for f in slot.figures if f.forecast is not None
        ) or None
        if slot.speakers_only:
            detail = slot.venue or None
        items.append(UpcomingItem(
            kind="fed" if slot.is_fed or slot.speakers_only else "economic",
            title=slot.title,
            at=slot.at,
            when_text="",
            importance=slot.importance if slot.importance in {"critical", "high"} else "medium",
            detail=detail,
            explainer=explainer("FED_SPEECH" if slot.speakers_only else slot.primary_code),
        ))
    return items


# --------------------------------------------------------------------------- moves

HistoryFn = Callable[..., Sequence[Any]]


def last_quote(history: HistoryFn, symbol: str, *, start: datetime, end: datetime):
    """The last stored quote between ``start`` and ``end``, or ``None``.

    Asks for one row: the store returns the newest rows in the range, so a long
    window never pushes the wanted quote past the row limit.
    """
    rows = sorted(history(symbol, start=start, end=end, limit=1), key=lambda o: o.timestamp)
    return rows[-1] if rows else None


def change_between(symbol: str, start_value: float, end_value: float) -> tuple[float, str]:
    if symbol in YIELD_SYMBOLS:
        return round((end_value - start_value) * 100.0, 1), "bp"
    if start_value == 0:
        return 0.0, "%"
    return round((end_value - start_value) / start_value * 100.0, 2), "%"


def measure_moves(
    history: HistoryFn,
    *,
    start: datetime,
    end: datetime,
    delayed: frozenset[str] = frozenset(),
    symbols: Sequence[str] = REACTION_SYMBOLS,
    min_window: timedelta = timedelta(minutes=2),
) -> list[Move]:
    """Change in each market from the last quote at ``start`` to the last quote by ``end``.

    A market is omitted, never estimated, when either quote is missing or the
    observed window is shorter than ``min_window``.
    """
    moves: list[Move] = []
    for symbol in symbols:
        try:
            before = last_quote(history, symbol, start=start - timedelta(minutes=30), end=start)
            after = last_quote(history, symbol, start=start - timedelta(minutes=30), end=end)
        except Exception:  # noqa: BLE001 - storage isolation; omit the market
            continue
        if before is None or after is None or after.timestamp - start < min_window:
            continue
        change, unit = change_between(symbol, before.value, after.value)
        moves.append(Move(
            symbol=symbol,
            label=LABELS.get(symbol, symbol),
            start_value=before.value,
            end_value=after.value,
            start_at=before.timestamp,
            end_at=after.timestamp,
            change=change,
            change_unit=unit,
            delayed=symbol in delayed,
            text=signed(change, unit),
        ))
    return moves


# --------------------------------------------------------------------------- quotes


def _value_text(symbol: str, value: float | None) -> str:
    if value is None:
        return "—"
    if symbol in YIELD_SYMBOLS:
        return f"{value:.3f}%"
    if symbol in {"WTI", "GOLD", "BTC"}:
        return f"${value:,.2f}"
    return f"{value:,.2f}"


def quote_strip(state: Any, symbols: Sequence[str] = STRIP_SYMBOLS) -> list[Quote]:
    now = getattr(state, "generated_at", None)
    index_closed = now is not None and not volatility_index_open(now)
    quotes: list[Quote] = []
    for symbol in symbols:
        inst = state.instruments.get(symbol)
        if inst is None:
            continue
        status = _value(inst, "status")
        feed = _value(inst, "feed_status")
        change_text = None
        direction = "unknown"
        if symbol in YIELD_SYMBOLS and inst.change is not None:
            bp = round(inst.change * 100.0, 1)
            change_text = signed(bp, "bp")
            direction = "up" if bp > 0 else "down" if bp < 0 else "flat"
        elif inst.change_pct is not None:
            change_text = signed(inst.change_pct, "%")
            direction = ("up" if inst.change_pct > 0 else "down" if inst.change_pct < 0
                         else "flat")
        cash_closed = (symbol in CASH_INDEXES and index_closed and status == "stale"
                       and inst.value is not None)
        market_closed = bool(getattr(inst, "market_closed", False)) and inst.value is not None
        closed = market_closed or cash_closed
        quotes.append(Quote(
            symbol=symbol,
            label=LABELS.get(symbol, inst.display_name),
            value=inst.value,
            value_text=_value_text(symbol, inst.value),
            change_text=change_text,
            direction=direction,
            as_of=inst.timestamp,
            delayed=feed == "delayed",
            stale=status in {"stale", "unavailable"} and not closed,
            closed=closed,
            closed_text=("Markets closed" if market_closed else "cash closed" if closed
                         else None),
            explainer=entry.text if (entry := X.instrument(symbol)) else None,
        ))
    return quotes


def _et(moment: datetime) -> str:
    return moment.astimezone(_NEW_YORK).strftime("%a %b %d, %H:%M ET")


def quote_status(inst: Any, quote: Quote) -> str:
    """One word for a quote: ``live``, ``closed`` (a last close while the market
    is shut), ``stale`` (an old value from a feed that should be updating) or
    ``unavailable`` (no value at all). Delay is reported separately."""
    if inst.value is None or _value(inst, "status") == "unavailable":
        return "unavailable"
    if quote.closed:
        return "closed"
    return "stale" if _value(inst, "status") == "stale" else "live"


def _delay_text(inst: Any) -> str | None:
    seconds = getattr(inst, "expected_delay_seconds", 0) or 0
    if seconds >= 60:
        return f"about {round(seconds / 60)} minutes"
    if seconds > 0:
        return f"about {seconds:.0f} seconds"
    return "none" if _value(inst, "feed_status") == "live" else None


def _baseline_text(inst: Any, symbol: str) -> str:
    """What today's change is measured from (``change_today``)."""
    if getattr(inst, "change", None) is None and getattr(inst, "change_pct", None) is None:
        return "not known (no baseline from the feed)"
    if (getattr(inst, "source", None) or "").startswith("binance"):
        return "the price 24 hours earlier (rolling)"
    if _value(inst, "asset_class") == "equity_index_future" or symbol in {"WTI", "GOLD"}:
        return "the previous session's settle"
    return "the previous session's close"


def market_rows(state: Any, symbols: Sequence[str] = STRIP_SYMBOLS) -> list[dict[str, Any]]:
    """The quote strip as writer facts: level and change, plus each quote's
    status, observation time, declared delay and change baseline.

    ``stale`` is true only for a stale feed: an unavailable quote and a closed
    market are told apart through ``status``.
    """
    rows: list[dict[str, Any]] = []
    for quote in quote_strip(state, symbols):
        inst = state.instruments[quote.symbol]
        status = quote_status(inst, quote)
        row: dict[str, Any] = {"market": quote.label, "level": quote.value_text,
                               "change_today": quote.change_text, "delayed": quote.delayed,
                               "stale": status == "stale", "status": status}
        if quote.as_of is not None:
            row["as_of"] = _et(quote.as_of)
        if status != "unavailable":
            if (delay := _delay_text(inst)) is not None:
                row["expected_delay"] = delay
            row["change_from"] = _baseline_text(inst, quote.symbol)
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- participation

#: The eight megacaps on the desk, in the order they are listed to the writer.
MEGACAPS: tuple[str, ...] = ("NVDA", "MSFT", "AAPL", "AMZN", "META", "GOOGL", "AVGO", "TSLA")
#: Instruments whose notes get NQ vs ES and megacap participation.
PARTICIPATION_INSTRUMENTS = frozenset({"NQ", "ES"})
#: A megacap counts as up or down only beyond this change (percent).
FLAT_PCT = 0.05


def _pct(value: float) -> str:
    return f"{value:+.2f}%"


def nq_vs_es(state: Any) -> dict[str, Any]:
    """NQ's change today against ES's (:func:`core.analysis.cross_market.compare_nq_es`).

    Compared only when both are live, from the same source, within a minute of
    each other and against the same baseline; otherwise the block says why not.
    """
    nq, es = state.instruments.get("NQ"), state.instruments.get("ES")
    for symbol, inst in (("NQ", nq), ("ES", es)):
        if inst is None or inst.value is None or _value(inst, "status") == "unavailable":
            return {"comparable": False, "why_not": f"no {symbol} quote"}
        if _value(inst, "status") != "live":
            return {"comparable": False, "why_not": f"the {symbol} quote is not current"}
    diff, label = compare_nq_es(nq, es)
    if diff.value is None:
        return {"comparable": False, "why_not": diff.reason or "NQ or ES change unavailable"}
    reading = {"outperforming": "NQ did better than ES",
               "underperforming": "NQ did worse than ES",
               "aligned": f"NQ and ES moved together (within "
                          f"{DEFAULT_TOLERANCE_PCT:.2f} percentage points)"}[label.value]
    return {"comparable": True,
            "nq_change_today": _pct(nq.change_pct),
            "es_change_today": _pct(es.change_pct),
            "nq_minus_es": f"{diff.value:+.2f} percentage points",
            "reading": reading,
            "as_of": _et(nq.timestamp)}


def megacap_participation(state: Any, symbols: Sequence[str] = MEGACAPS) -> dict[str, Any]:
    """How many of the megacaps are up or down today, from live quotes only.

    A closed, stale or missing quote is listed with its status and left out of
    the count; with no live quote the count is "not available".
    """
    stocks: list[dict[str, Any]] = []
    up = down = flat = 0
    for symbol in symbols:
        inst = state.instruments.get(symbol)
        if inst is None:
            stocks.append({"stock": symbol, "status": "unavailable"})
            continue
        closed = bool(getattr(inst, "market_closed", False)) and inst.value is not None
        status = ("unavailable" if inst.value is None or _value(inst, "status") == "unavailable"
                  else "closed" if closed
                  else "stale" if _value(inst, "status") == "stale" else "live")
        row: dict[str, Any] = {"stock": symbol, "status": status}
        if status != "unavailable" and inst.change_pct is not None:
            row["change_today"] = _pct(inst.change_pct)
        if inst.timestamp is not None:
            row["as_of"] = _et(inst.timestamp)
        stocks.append(row)
        if status != "live" or inst.change_pct is None:
            continue
        if inst.change_pct > FLAT_PCT:
            up += 1
        elif inst.change_pct < -FLAT_PCT:
            down += 1
        else:
            flat += 1
    counted = up + down + flat
    if counted:
        summary = (f"{up} up, {down} down, {flat} flat of {counted} with live quotes "
                   f"({len(symbols) - counted} not counted: closed, stale or unavailable)")
    elif all(row["status"] == "unavailable" for row in stocks):
        return {"summary": "not available: no megacap quotes"}
    else:
        summary = "not available: no live megacap quotes"
    return {"summary": summary, "flat_means": f"within {FLAT_PCT:.2f}% of the baseline",
            "stocks": stocks}


def participation(state: Any, symbol: str) -> dict[str, Any]:
    """NQ vs ES and megacap participation for an NQ or ES note (else nothing)."""
    if symbol not in PARTICIPATION_INSTRUMENTS:
        return {}
    return {"nq_vs_es": nq_vs_es(state), "megacap_participation": megacap_participation(state)}


# --------------------------------------------------------------------------- events in a window


def events_in_window(events: Iterable[Any], *, start: datetime, end: datetime,
                     lead: timedelta) -> dict[str, Any]:
    """Scheduled economic and Fed events from ``lead`` before ``start`` to ``end``.

    The checked window is stated, so an empty list reads as "no event on the
    supplied calendar", not "nothing happened".
    """
    slots = release_slots(events, start=start - lead, end=end, min_importance="medium")
    return {"checked_from": _et(start - lead), "checked_to": _et(end),
            "calendar": "US economic releases and Fed events (medium importance and up)",
            "events": [{"when": _et(slot.at), "what": slot.title,
                        "importance": slot.importance} for slot in slots]}


# --------------------------------------------------------------------------- price location

#: Key levels the writer gets, in this order. The gap is left out: today's
#: change from the settle is already in ``markets_today``.
LOCATION_LEVELS: tuple[str, ...] = (
    "prior_settle", "overnight_high", "overnight_low", "prior_day_high", "prior_day_low",
    "prior_week_high", "prior_week_low",
)


def _when(moment: datetime) -> str:
    return moment.astimezone(_NEW_YORK).strftime("%a %b %d %H:%M ET")


def price_location(levels: Any, price: float | None,
                   quote_at: datetime | None = None) -> dict[str, Any] | None:
    """Where ``price`` sits against the key levels, as ready-made text.

    Each level carries the window it was measured over, so "prior day" and
    "prior week" are unambiguous around weekends and holidays. Levels that could
    not be measured are left out; with no price or no measured level there is
    nothing to say and the result is ``None``. The overnight range is "so far"
    while the window is running or the latest quote (``quote_at``, delayed)
    has not reached its end.
    """
    if price is None or not isfinite(price):
        return None
    by_key = {level.key: level for level in levels.levels}
    rows: list[dict[str, Any]] = []
    overnight_so_far = False
    for key in LOCATION_LEVELS:
        level = by_key.get(key)
        if level is None or level.value is None:
            continue
        gap = round(price - level.value, 2)
        side = "above" if gap > 0 else "below" if gap < 0 else "at"
        row = {"level": level.label, "value": level.value_text,
               "nq_is": side if gap == 0 else f"{abs(gap):,.2f} points {side}"}
        if level.window_start is not None and level.window_end is not None:
            row["measured"] = f"{_when(level.window_start)} to {_when(level.window_end)}"
        rows.append(row)
        if key.startswith("overnight"):
            overnight_so_far = overnight_so_far or level.partial or (
                quote_at is not None and level.window_end is not None
                and quote_at < level.window_end)
    if not rows:
        return None
    return {"nq_price": _value_text(levels.symbol, price), "levels": rows,
            **({"overnight_so_far": True} if overnight_so_far else {})}


# --------------------------------------------------------------------------- normal move size

#: VXN and VIX are compared only when their quotes are this close in time.
VOL_INDEX_SKEW = timedelta(minutes=30)

#: Each instrument's own implied-volatility index and the broad one it is
#: compared with: NQ reads VXN against VIX, ES reads VIX alone. An instrument
#: not listed gets no fear gauge rather than another market's.
VOL_INDEXES: dict[str, tuple[str, str | None]] = {"NQ": ("VXN", "VIX"), "ES": ("VIX", None)}


def normal_move(size: Any, vxn: Any, vix: Any, *, trigger_pct: float,
                trigger_scaled: bool, gauge: str = "VXN",
                benchmark: str | None = "VIX") -> dict[str, Any] | None:
    """How big an instrument's moves normally are, and its fear gauge, as text.

    ``size`` is a :class:`core.analysis.move_size.MoveSize`; ``vxn``/``vix`` are
    the market-state instruments for ``gauge`` and ``benchmark`` (or None);
    NQ's are VXN and VIX (:data:`VOL_INDEXES`). Unmeasurable parts are left
    out, and a finished session's range only when its quotes were complete.
    """
    out: dict[str, Any] = {}
    if size is not None and size.typical_range is not None:
        out["typical_daily_range"] = (f"{size.typical_range:,.0f} points "
                                      f"({size.typical_range_pct:.2f}%)")
        out["typical_measured_over"] = f"median of the last {size.sessions} complete sessions"
    if size is not None and size.today_range is not None and (
            size.today_partial or size.today_complete):
        today = (f"{size.trade_date:%a %b %d} session: {size.today_range:,.0f} points "
                 f"({size.today_range_pct:.2f}%)")
        if size.typical_range_pct:
            share = size.today_range_pct / size.typical_range_pct * 100
            today += f", {share:.0f}% of a typical day"
        out["session_range_so_far" if size.today_partial else "session_range"] = today
    if size is not None and size.realised_vol_pct is not None:
        out["realised_volatility"] = f"{size.realised_vol_pct:.1f}% a year"
    vxn_value = getattr(vxn, "value", None)
    vix_value = getattr(vix, "value", None) if benchmark else None
    own, other = gauge.lower(), (benchmark or "").lower()
    if vxn_value is not None and isfinite(vxn_value):
        out[own] = f"{vxn_value:.2f}"
        if (at := getattr(vxn, "timestamp", None)) is not None:
            out[f"{own}_as_of"] = _when(at)
        vix_at = getattr(vix, "timestamp", None)
        if (vix_value is not None and isfinite(vix_value) and at is not None
                and vix_at is not None and abs(at - vix_at) <= VOL_INDEX_SKEW):
            out[other] = f"{vix_value:.2f}"
            out[f"{own}_minus_{other}"] = f"{vxn_value - vix_value:+.2f} points"
    if not out:
        return None
    out["large_move_trigger"] = (f"{trigger_pct:.2f}% in one hour, "
                                 + ("scaled to the typical daily range" if trigger_scaled
                                    else "fixed (not enough history to scale it)"))
    return out


# --------------------------------------------------------------------------- rates


def _bp_text(value: float | None) -> str:
    return "—" if value is None else signed(value, "bp")


def rates_view(state: Any, history: HistoryFn, *, now: datetime) -> RatesView:
    """2-year, 10-year and the 10y-2y curve with day and one-week changes."""
    lines: list[YieldLine] = []
    current: dict[str, float] = {}
    week_ago: dict[str, float] = {}
    as_of = None
    for symbol in ("US2Y", "US10Y"):
        inst = state.instruments.get(symbol)
        value = getattr(inst, "value", None)
        day = round(inst.change * 100.0, 1) if inst is not None and inst.change is not None \
            else None
        week = None
        if value is not None:
            current[symbol] = value
            as_of = max(filter(None, [as_of, inst.timestamp]), default=None)
            try:
                past = list(history(symbol, start=now - timedelta(days=10),
                                    end=now - timedelta(days=7), limit=1))
            except Exception:  # noqa: BLE001 - storage isolation
                past = []
            if past:
                week_ago[symbol] = past[-1].value
                week = round((value - past[-1].value) * 100.0, 1)
        lines.append(YieldLine(
            symbol=symbol, label=LABELS[symbol], value=value,
            value_text=_value_text(symbol, value),
            day_change_bp=day, week_change_bp=week,
            day_text=_bp_text(day), week_text=_bp_text(week),
        ))
    curve = None
    curve_text = "unavailable"
    if {"US2Y", "US10Y"} <= current.keys():
        curve = round((current["US10Y"] - current["US2Y"]) * 100.0, 1)
        shape = "inverted (10-year below 2-year)" if curve < 0 else "normal (10-year above 2-year)"
        curve_text = f"{curve:+.0f}bp, {shape}"
        day_curve = None
        day_lines = {line.symbol: line.day_change_bp for line in lines}
        if None not in day_lines.values():
            day_curve = round(day_lines["US10Y"] - day_lines["US2Y"], 1)
        week_curve = None
        if {"US2Y", "US10Y"} <= week_ago.keys():
            week_curve = round(curve - (week_ago["US10Y"] - week_ago["US2Y"]) * 100.0, 1)
        lines.append(YieldLine(
            symbol="CURVE", label="Curve (10y minus 2y)", value=curve,
            value_text=f"{curve:+.0f}bp",
            day_change_bp=day_curve, week_change_bp=week_curve,
            day_text=_bp_text(day_curve), week_text=_bp_text(week_curve),
        ))
    return RatesView(as_of=as_of, lines=lines, curve_bp=curve, curve_text=curve_text,
                     explainer=X.topic("rates").text)
