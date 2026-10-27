"""The Macro page's view model: about 14 cards in four groups. Deterministic; no AI.

Each card is one official series from the economic service with its latest
value, a trend label (the last few readings against the few before), the last
release against its forecast (from the calendar, the same figures and
better/worse read as the Calendar page) and whether a rise is usually good or
bad for NQ, so the page colours mean "good/bad for NQ" rather than up/down.

Duplicate series are merged: PPI comes from both FRED and BLS; the card shows
the BLS year-over-year series and names the other sources behind its ⓘ.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import fmean
from typing import Any, Literal
from zoneinfo import ZoneInfo

from core.desk import calendar as CAL
from core.desk import explainers as X
from core.desk import facts as F
from core.desk.models import MacroBoard, MacroCard, MacroGroup, MacroRelease, RatesView

NEW_YORK = ZoneInfo("America/New_York")
#: How far back the last release of a card's figure is looked for.
RELEASE_LOOKBACK = timedelta(days=130)
#: How far ahead the next Fed decision is looked for.
FED_AHEAD = timedelta(days=120)

NqSide = Literal["bad", "good", "mixed"]
Trend = Literal["rising", "falling", "steady", "unknown"]

#: What a rise usually means for NQ, in the library's words. Inflation, the
#: Fed's rate and the 2-year yield rising usually weigh on NQ; jobs and growth
#: cut both ways (good for profits, fewer rate cuts), so they read "mixed".
_NQ_TEXT: dict[NqSide, str] = {
    "bad": "A rise usually weighs on NQ; a fall usually helps it.",
    "good": "A rise usually helps NQ; a fall usually weighs on it.",
    "mixed": "A rise or fall can cut both ways for NQ (stronger economy, but fewer rate "
             "cuts), so it is not coloured good or bad.",
}


@dataclass(frozen=True)
class Spec:
    """One card: the series it shows and how to read it."""

    key: str
    group: str
    title: str
    series: str
    #: Calendar event codes whose releases this card follows.
    codes: tuple[str, ...]
    #: Calendar figure names to take the last release from, in order of preference.
    figures: tuple[str, ...]
    nq_when_rising: NqSide
    #: A change smaller than this (in the card's units) reads "steady".
    steady: float
    #: Readings averaged on each side of the trend comparison.
    window: int = 3
    explainer: str | None = None
    #: Other series that duplicate this one (merged; named behind the ⓘ).
    also: tuple[str, ...] = ()


GROUPS: tuple[tuple[str, str], ...] = (
    ("inflation", "Inflation"), ("jobs", "Jobs"), ("growth", "Growth"), ("fed", "The Fed"))

SPECS: tuple[Spec, ...] = (
    Spec("cpi", "inflation", "CPI year over year", "CPI_YOY", ("CPI",), ("CPI y/y",), "bad", 0.1,
         explainer="indicator:CPI"),
    Spec("core_cpi", "inflation", "Core CPI year over year", "CORE_CPI_YOY", ("CORE_CPI",),
         ("Core CPI y/y",), "bad", 0.1, explainer="indicator:CORE_CPI"),
    Spec("pce", "inflation", "PCE inflation year over year", "PCE_YOY", ("PCE",),
         ("PCE Price Index y/y", "PCE y/y"), "bad", 0.1, explainer="indicator:PCE"),
    Spec("ppi", "inflation", "PPI year over year", "BLS_PPI_YOY", ("PPI", "BLS_PPI"),
         ("PPI y/y", "PPI m/m"), "bad", 0.2, explainer="indicator:PPI",
         also=("PPI", "BLS_PPI_MOM")),
    Spec("unemployment", "jobs", "Unemployment rate", "UNEMPLOYMENT", ("UNEMPLOYMENT",),
         ("Unemployment Rate",), "mixed", 0.1, explainer="indicator:UNEMPLOYMENT"),
    Spec("payrolls", "jobs", "Payrolls monthly change", "PAYROLLS", ("PAYROLLS",),
         ("Non-Farm Employment Change",), "mixed", 25.0, explainer="indicator:PAYROLLS"),
    Spec("ahe", "jobs", "Average hourly earnings year over year", "AHE_YOY", ("AHE",),
         ("Average Hourly Earnings y/y", "Average Hourly Earnings m/m"), "bad", 0.1,
         explainer="indicator:AHE"),
    Spec("jolts", "jobs", "Job openings", "JOLTS", ("JOLTS",), ("JOLTS Job Openings",),
         "mixed", 150.0, explainer="indicator:JOLTS"),
    Spec("gdp", "growth", "GDP growth (annualised)", "GDP", ("GDP",),
         ("Advance GDP q/q", "GDP q/q"), "mixed", 0.5, window=2, explainer="indicator:GDP"),
    Spec("retail", "growth", "Retail sales month over month", "RETAIL_SALES", ("RETAIL_SALES",),
         ("Retail Sales m/m",), "mixed", 0.3, explainer="indicator:RETAIL_SALES"),
    Spec("ism", "growth", "ISM manufacturing", "ISM_MFG", ("ISM_MFG",),
         ("ISM Manufacturing PMI",), "mixed", 1.0, explainer="indicator:ISM_MFG"),
)


def _period_text(period: str) -> str:
    """'2026-09' → 'Sep 2026', '2026-Q2' → 'Q2 2026', '2026-10-17' → 'week to Oct 17'."""
    parts = period.split("-")
    if len(parts) == 2 and parts[1].startswith("Q"):
        return f"{parts[1]} {parts[0]}"
    if len(parts) == 2:
        return datetime(int(parts[0]), int(parts[1]), 1).strftime("%b %Y")
    if len(parts) == 3:
        day = datetime(int(parts[0]), int(parts[1]), int(parts[2]))
        return f"{day:%b} {day.day}, {day.year}"
    return period


def _value_text(value: float, unit: str, *, signed: bool = False) -> str:
    if unit == "percent":
        return f"{value:+.1f}%" if signed else f"{value:.1f}%"
    if unit == "thousand jobs":
        jobs = value * 1000
        if abs(jobs) >= 1_000_000:
            return f"{jobs / 1_000_000:.2f}M"
        sign = "+" if signed and jobs > 0 else "−" if jobs < 0 else ""
        return f"{sign}{abs(jobs) / 1000:.0f}K"
    return f"{value:.1f}"


def trend(values: Sequence[float], *, window: int, steady: float) -> Trend:
    """The mean of the last ``window`` readings against the ``window`` before them."""
    if len(values) < 2 * window:
        return "unknown"
    change = fmean(values[-window:]) - fmean(values[-2 * window:-window])
    if abs(change) < steady:
        return "steady"
    return "rising" if change > 0 else "falling"


def nq_effect(direction: Trend, side: NqSide) -> Literal["good", "bad", "mixed", "neutral"]:
    """What a move in this direction usually means for NQ (colours only)."""
    if direction in ("steady", "unknown"):
        return "neutral"
    if side == "mixed":
        return "mixed"
    good = (direction == "rising") == (side == "good")
    return "good" if good else "bad"


_TREND_TEXT = {
    "rising": "rising", "falling": "falling", "steady": "steady",
    "unknown": "not enough history for a trend",
}


def _trend_text(direction: Trend, window: int, frequency: str) -> str:
    if direction == "unknown":
        return _TREND_TEXT[direction]
    span = {"monthly": "months", "quarterly": "quarters"}.get(frequency, "readings")
    compared = {"rising": "higher than", "falling": "lower than", "steady": "about the same as"}
    return (f"{_TREND_TEXT[direction]}: the last {window} {span} averaged "
            f"{compared[direction]} the {window} before")


def last_release(spec: Spec, slots: Sequence[F.ReleaseSlot], events: Sequence[Any],
                 ) -> MacroRelease | None:
    """The newest release of one of the card's figures that had a forecast."""
    wanted = [name.casefold() for name in spec.figures]
    for slot in sorted(slots, key=lambda s: s.at, reverse=True):
        code = next((c for c in slot.codes if c in spec.codes), None)
        if code is None:
            continue
        by_name = {f.name.casefold(): f for f in slot.figures
                   if f.verdict in ("above", "below", "in_line")}
        figure = next((by_name[name] for name in wanted if name in by_name), None)
        if figure is None:
            continue
        codes = {row.name.casefold(): row.event_code for row in events
                 if getattr(row, "scheduled_at", None) == slot.at}
        read = CAL.read_figure(figure, codes.get(figure.name.casefold(), code))
        direction: Trend = ("steady" if figure.verdict == "in_line" else
                            "rising" if figure.verdict == "above" else "falling")
        word = {"above": "above forecast", "below": "below forecast",
                "in_line": "in line"}[figure.verdict]
        return MacroRelease(at=slot.at, figure=read, text=f"Last release: {word}",
                            nq_effect=nq_effect(direction, spec.nq_when_rising))
    return None


def _sources(state: Any, also: Iterable[Any]) -> list[str]:
    d = state.definition
    lines = [f"Source: {d.provider.upper()} series {d.source_series_id}."]
    if d.info:
        lines.append(d.info + ".")
    latest = state.latest
    if latest is not None:
        if latest.released_at is not None:
            lines.append(f"Released {latest.released_at.astimezone(NEW_YORK):%b %d, %H:%M} ET.")
        else:
            lines.append(f"Received {latest.received_at.astimezone(NEW_YORK):%b %d, %H:%M} ET "
                         "(the source gives no publication time).")
        if latest.revision > 1:
            lines.append(f"Revision {latest.revision}.")
    for other in also:
        od = other.definition
        lines.append(f"Also published as {od.name} ({od.provider.upper()} {od.source_series_id}); "
                     "merged into this card.")
    if getattr(state, "status", "live") != "live":
        lines.append(f"Status: {state.status}.")
    return lines


def _series_card(spec: Spec, state: Any, also: list[Any], slots, events) -> MacroCard:
    d = state.definition
    points = [o for o in state.recent if o.value is not None]
    values = [o.value for o in points]
    latest = state.latest
    status = str(getattr(state.status, "value", state.status))
    direction = trend(values, window=spec.window, steady=spec.steady)
    signed = spec.series == "PAYROLLS" or d.transform == "mom"
    return MacroCard(
        key=spec.key, group=spec.group, title=spec.title,
        value=latest.value if latest is not None else None,
        value_text=(_value_text(latest.value, d.unit, signed=signed)
                    if latest is not None and latest.value is not None else "—"),
        period_text=_period_text(latest.period) if latest is not None else None,
        trend=direction, trend_text=_trend_text(direction, spec.window, d.frequency),
        nq_when_rising=spec.nq_when_rising, nq_text=_NQ_TEXT[spec.nq_when_rising],
        nq_effect=nq_effect(direction, spec.nq_when_rising),
        points=values[-24:],
        release=last_release(spec, slots, events),
        explainer_key=spec.explainer if spec.explainer in X.LIBRARY else None,
        sources=_sources(state, also),
        stale=status != "live",
    )


def _missing_card(spec: Spec) -> MacroCard:
    return MacroCard(key=spec.key, group=spec.group, title=spec.title, value=None,
                     value_text="—", period_text=None, trend="unknown",
                     trend_text="unavailable", nq_when_rising=spec.nq_when_rising,
                     nq_text=_NQ_TEXT[spec.nq_when_rising], nq_effect="neutral",
                     explainer_key=spec.explainer if spec.explainer in X.LIBRARY else None,
                     sources=[f"Series {spec.series} is not available from the core."],
                     stale=True)


def _fed_target(by_id: dict[str, Any]) -> MacroCard:
    lower, upper = by_id.get("FED_TARGET_LOWER"), by_id.get("FED_TARGET_UPPER")
    highs = [o.value for o in upper.recent if o.value is not None] if upper else []
    have = bool(lower and upper and lower.latest and upper.latest
                and lower.latest.value is not None and upper.latest.value is not None)
    direction: Trend = "unknown"
    if len(highs) >= 2:
        change = highs[-1] - highs[0]
        direction = "steady" if change == 0 else "rising" if change > 0 else "falling"
    sources = _sources(upper, [lower]) if upper and lower else ["Fed target range unavailable."]
    return MacroCard(
        key="fed_target", group="fed", title="Fed funds target",
        value=upper.latest.value if have else None,
        value_text=f"{lower.latest.value:.2f}–{upper.latest.value:.2f}%" if have else "—",
        period_text=_period_text(upper.latest.period) if have else None,
        trend=direction,
        trend_text=(f"{_TREND_TEXT[direction]} over the last {len(highs)} daily readings"
                    if direction != "unknown" else _TREND_TEXT[direction]),
        nq_when_rising="bad", nq_text=_NQ_TEXT["bad"], nq_effect=nq_effect(direction, "bad"),
        points=highs[-24:],
        explainer_key="indicator:FOMC", sources=sources,
        stale=not have or str(getattr(upper.status, "value", upper.status)) != "live",
    )


def _next_decision(events: Iterable[Any], now: datetime) -> MacroCard:
    upcoming = sorted((e for e in events if e.event_code == "FOMC" and e.scheduled_at is not None
                       and e.scheduled_at > now), key=lambda e: e.scheduled_at)
    nxt = upcoming[0] if upcoming else None
    local = nxt.scheduled_at.astimezone(NEW_YORK) if nxt else None
    return MacroCard(
        key="fed_next", group="fed", title="Next Fed decision",
        value=None,
        value_text=f"{local:%a %b} {local.day}" if local else "—",
        period_text=f"{local:%H:%M} ET" if local else None,
        at=nxt.scheduled_at if nxt else None,
        trend="unknown", trend_text="", nq_when_rising="bad", nq_text=_NQ_TEXT["bad"],
        nq_effect="neutral", explainer_key="indicator:FOMC",
        sources=(["From the Federal Reserve's published meeting calendar."] if nxt else
                 ["No Fed decision found in the calendar for the next four months."]),
        stale=nxt is None,
    )


def _two_year(rates: RatesView | None) -> MacroCard:
    line = next((x for x in (rates.lines if rates else []) if x.symbol == "US2Y"), None)
    week = line.week_change_bp if line else None
    direction: Trend = ("unknown" if week is None else "steady" if abs(week) < 5
                        else "rising" if week > 0 else "falling")
    as_of = rates.as_of if rates else None
    return MacroCard(
        key="us2y", group="fed", title="2-year yield",
        subtitle="what markets expect from the Fed",
        value=line.value if line else None, value_text=line.value_text if line else "—",
        period_text=(f"{as_of.astimezone(NEW_YORK):%b %d, %H:%M} ET" if as_of else None),
        trend=direction,
        trend_text=(f"{_TREND_TEXT[direction]}: {line.week_text} over the week"
                    if line and week is not None else _TREND_TEXT["unknown"]),
        nq_when_rising="bad", nq_text=_NQ_TEXT["bad"], nq_effect=nq_effect(direction, "bad"),
        explainer_key="instrument:US2Y" if "instrument:US2Y" in X.LIBRARY else None,
        sources=["Delayed quote from the market feed, the same as the Today strip.",
                 *(["Day: " + line.day_text + ". Week: " + line.week_text + "."] if line else [])],
        stale=line is None or line.value is None,
        quote_symbol="US2Y",
    )


def board(series: Iterable[Any], events: Iterable[Any], rates: RatesView | None, *,
          now: datetime) -> MacroBoard:
    """The Macro page: four groups of cards, built from stored data only."""
    by_id = {s.definition.id: s for s in series}
    events = list(events)
    slots = F.release_slots([e for e in events if e.scheduled_at is not None
                             and e.scheduled_at <= now],
                            start=now - RELEASE_LOOKBACK, end=now, min_importance="low")
    cards: list[MacroCard] = []
    for spec in SPECS:
        state = by_id.get(spec.series)
        also = [by_id[s] for s in spec.also if s in by_id]
        cards.append(_series_card(spec, state, also, slots, events)
                     if state is not None else _missing_card(spec))
    cards += [_fed_target(by_id), _next_decision(events, now), _two_year(rates)]
    groups = [MacroGroup(key=key, title=title, cards=[c for c in cards if c.group == key])
              for key, title in GROUPS]
    return MacroBoard(generated_at=now, groups=groups)


#: Cards the desk writer already gets elsewhere (the yields and the calendar).
_NOT_FOR_WRITER = frozenset({"us2y", "fed_next"})
_NQ_SHORT: dict[str, str] = {
    "bad": "a rise usually weighs on NQ",
    "good": "a rise usually helps NQ",
    "mixed": "a rise cuts both ways for NQ",
}


def writer_trends(macro: MacroBoard) -> dict[str, list[dict[str, Any]]]:
    """The Macro page's cards as ready-made text for the desk writer, by group.

    Cards with no value are left out; a stale card says so.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for group in macro.groups:
        rows = []
        for card in group.cards:
            if card.key in _NOT_FOR_WRITER or card.value is None:
                continue
            row: dict[str, Any] = {
                "name": card.title,
                "latest": f"{card.value_text} ({card.period_text})" if card.period_text
                else card.value_text,
                "trend": card.trend_text,
                "for_nq": _NQ_SHORT[card.nq_when_rising],
            }
            if card.release is not None:
                row["last_release"] = card.release.text.removeprefix("Last release: ")
            if card.stale:
                row["stale"] = True
            rows.append(row)
        if rows:
            out[group.key] = rows
    return out


__all__ = ["FED_AHEAD", "RELEASE_LOOKBACK", "SPECS", "board", "last_release", "nq_effect",
           "trend", "writer_trends"]
