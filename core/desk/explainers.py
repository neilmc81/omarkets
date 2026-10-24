"""The explainer library: fixed plain-English entries, written once, no AI.

One entry per thing the UI explains on hover: the markets on the strip
(instruments), the calendar releases the desk follows (indicators), the key
price levels, the parts of the futures day (segments), the what-to-watch
readings and a few topics (rates, Fed speakers, Treasury auctions). Each entry
says what the thing is, why it matters for NQ, and what a rise or a fall (or a
figure above or below forecast, or price above or below a level) usually means.

These are explanations, not advice: rules of thumb worded "usually" and "tends
to". Only desk notes interpret what actually happened. The library is pure
data with no imports from the rest of the core, so any layer can use it.
``docs/explainers-review.md`` is generated from it (``python -m
core.desk.explainers``) for the user to read and approve the wording.
"""

from __future__ import annotations

import sys
from typing import Literal

from pydantic import BaseModel, ConfigDict

Kind = Literal["instrument", "indicator", "level", "segment", "reading", "topic"]

#: How the UI labels ``rising`` and ``falling`` for each kind.
_DIRECTION_LABELS: dict[Kind, tuple[str, str]] = {
    "instrument": ("Rising", "Falling"),
    "indicator": ("Above forecast", "Below forecast"),
    "reading": ("Above forecast", "Below forecast"),
    "level": ("Above it", "Below it"),
    "segment": ("", ""),
    "topic": ("Rising", "Falling"),
}


class Explainer(BaseModel):
    """One fixed explainer. ``key`` is ``<kind>:<name>``, e.g. ``instrument:NQ``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    kind: Kind
    name: str
    title: str
    #: What it is.
    what: str
    #: Why it matters for NQ.
    why_nq: str
    #: What a rise (or above forecast, or price above the level) usually means.
    rising: str | None = None
    falling: str | None = None
    rising_label: str | None = None
    falling_label: str | None = None

    @property
    def text(self) -> str:
        """The short form: what it is and why it matters, as one paragraph."""
        return f"{self.what} {self.why_nq}"

    @property
    def full_text(self) -> str:
        """Everything in one paragraph, for payloads that carry a single string."""
        parts = [self.what, self.why_nq]
        parts += [f"{label}: {text}" for label, text in
                  ((self.rising_label, self.rising), (self.falling_label, self.falling)) if text]
        return " ".join(parts)


def _entry(kind: Kind, name: str, title: str, what: str, why_nq: str,
           rising: str | None = None, falling: str | None = None) -> Explainer:
    up, down = _DIRECTION_LABELS[kind]
    return Explainer(key=f"{kind}:{name}", kind=kind, name=name, title=title, what=what,
                     why_nq=why_nq, rising=rising, falling=falling,
                     rising_label=up if rising else None, falling_label=down if falling else None)


# --------------------------------------------------------------------------- instruments

INSTRUMENTS: tuple[Explainer, ...] = (
    _entry("instrument", "NQ", "Nasdaq 100 futures",
           "Futures on the Nasdaq 100, the 100 biggest non-financial Nasdaq companies, led by "
           "big tech.",
           "This is the market the desk follows. It trades almost 24 hours a day, so it shows "
           "the mood before and after the US stock market's hours.",
           "Buyers are paying up for big tech; the rest of the strip usually tells you why.",
           "Sellers are in charge for now; check yields, the dollar and the VIX for the "
           "reason."),
    _entry("instrument", "ES", "S&P 500 futures",
           "Futures on the S&P 500, the broad US stock market.",
           "When NQ and ES move together the move is market-wide; when NQ moves alone it is "
           "usually about tech.",
           "The whole US market is rising. If NQ rises more, tech is leading.",
           "The whole US market is falling. If NQ falls more, tech is leading the drop."),
    _entry("instrument", "US2Y", "2-year yield",
           "The interest rate on 2-year US government bonds.",
           "It follows what traders expect the Fed to do with interest rates, and Nasdaq "
           "stocks are sensitive to that.",
           "Traders expect higher rates for longer. That usually weighs on NQ.",
           "Traders expect rate cuts sooner or more of them. That usually helps NQ."),
    _entry("instrument", "US10Y", "10-year yield",
           "The interest rate on 10-year US government bonds. It reflects longer-run growth "
           "and inflation.",
           "Higher long yields make future tech profits worth less today, so NQ tends to "
           "move the opposite way.",
           "Usually weighs on NQ, especially on fast moves.",
           "Usually helps NQ, unless it is falling because investors fear a slowdown."),
    _entry("instrument", "DXY", "US dollar",
           "The US dollar against six major currencies.",
           "Big tech earns a lot overseas, and a sharply stronger dollar often goes with "
           "caution in markets.",
           "A sharply stronger dollar often goes with caution and can trim big tech's "
           "overseas profits; it usually weighs on NQ.",
           "A weaker dollar usually goes with calmer markets and tends to help NQ."),
    _entry("instrument", "VIX", "VIX fear gauge",
           "The VIX 'fear gauge' measures how much movement options traders expect in "
           "the S&P 500 over the next month. It is only calculated 03:15–16:15 ET; "
           "outside those hours it shows its close.",
           "It is the market's nervousness in one number, and it usually moves the opposite "
           "way to stocks.",
           "A jump in the VIX usually comes with falling stocks and bigger swings in NQ.",
           "A falling VIX usually comes with calmer, rising stocks."),
    _entry("instrument", "VXN", "Nasdaq fear gauge",
           "Like the VIX, but for the Nasdaq 100: how much movement options traders expect "
           "in it over the next month. Calculated in Cboe's hours only.",
           "It measures nervousness about NQ itself.",
           "Usually comes with falling NQ and bigger swings.",
           "Usually comes with calmer, rising NQ."),
    _entry("instrument", "WTI", "Crude oil",
           "US crude oil (West Texas Intermediate) futures.",
           "Oil feeds into inflation, and inflation feeds into yields.",
           "A big rise in oil can push inflation up and with it yields, which usually "
           "weighs on NQ.",
           "Cheaper oil eases inflation worries, which tends to help NQ; a crash in oil can "
           "also signal a slowing economy."),
    _entry("instrument", "GOLD", "Gold",
           "Gold futures. Gold is a classic safe haven.",
           "It is a read on how nervous investors are, and it reacts to yields and the "
           "dollar.",
           "Gold often rises when investors are nervous or when yields and the dollar fall.",
           "Gold often falls when yields and the dollar rise or nerves calm."),
)

# --------------------------------------------------------------------------- levels

_ABOVE_LOW = "Holding above it suggests buyers are still around."
LEVELS: tuple[Explainer, ...] = (
    _entry("level", "overnight_high", "Overnight high",
           "The highest price since futures reopened at 18:00 ET yesterday.",
           "Traders often watch whether the cash session can push above it.",
           "A break above is usually read as strength.",
           "A turn back down from it is usually read as hesitation."),
    _entry("level", "overnight_low", "Overnight low",
           "The lowest price since futures reopened at 18:00 ET yesterday.",
           "It is the floor of the overnight range, which many traders mark.",
           _ABOVE_LOW,
           "A drop below it is usually read as weakness."),
    _entry("level", "prior_settle", "Prior settle",
           "Yesterday's official settlement price.",
           "Today's change is measured from here, and prices often pause near it.",
           "NQ is up on the day.",
           "NQ is down on the day."),
    _entry("level", "prior_day_high", "Prior day high",
           "The highest price of the previous full session (18:00–17:00 ET).",
           "Many traders mark it.",
           "Trading above it usually means buyers are in control for now.",
           "Below it, price is still inside yesterday's range."),
    _entry("level", "prior_day_low", "Prior day low",
           "The lowest price of the previous full session (18:00–17:00 ET).",
           "Many traders mark it.",
           "Above it, price is still inside yesterday's range.",
           "Trading below it usually means sellers are in control for now."),
    _entry("level", "prior_week_high", "Prior week high",
           "The highest price of last week.",
           "A bigger-picture line: moves through it tend to get attention from longer-term "
           "traders.",
           "A break above it tends to draw in more buying.",
           None),
    _entry("level", "prior_week_low", "Prior week low",
           "The lowest price of last week.",
           "A bigger-picture line: moves through it tend to get attention from longer-term "
           "traders.",
           None,
           "A break below it often draws in more selling."),
    _entry("level", "gap_vs_settle", "Gap vs settle",
           "How far price is from yesterday's settle when the cash market opens at 09:30 ET "
           "(before then: where it would open now).",
           "Large gaps sometimes partly 'fill' back toward the settle during the day, but "
           "not reliably.",
           "A gap up: the market opens above yesterday's settle.",
           "A gap down: the market opens below yesterday's settle."),
)

# --------------------------------------------------------------------------- segments

SEGMENTS: tuple[Explainer, ...] = (
    _entry("segment", "asia", "Asia",
           "Futures reopen at 18:00 ET. Asian markets trade now.",
           "Volume in NQ is usually thin, so moves can be jumpy and may not last."),
    _entry("segment", "europe", "Europe",
           "European markets open.",
           "Activity in US futures usually picks up."),
    _entry("segment", "premarket", "Pre-market",
           "The US pre-market, from 08:00 ET until the 09:30 ET cash open.",
           "The 08:30 ET economic data comes out here and often moves NQ."),
    _entry("segment", "cash", "US cash",
           "The US stock market is open (09:30–16:00 ET).",
           "Most of the day's volume and the biggest moves usually happen here."),
    _entry("segment", "after_hours", "After the cash close",
           "After the US cash close, futures trade on until 17:00 ET.",
           "Big tech earnings often come out now and can move NQ sharply."),
)

# --------------------------------------------------------------------------- readings

READINGS: tuple[Explainer, ...] = (
    _entry("reading", "inflation", "Inflation figures",
           "Figures that measure how fast prices are rising.",
           "Inflation decides how soon the Fed can cut rates, and rates move NQ.",
           "Hotter than forecast: inflation is stickier, so Fed rate cuts look less likely. "
           "Yields usually rise, and that usually weighs on NQ.",
           "Cooler than forecast: rate cuts look more likely. Yields usually fall, and that "
           "usually helps NQ."),
    _entry("reading", "jobless", "Jobless figures",
           "Figures that count people out of work.",
           "They show how tight the job market is, which shapes the Fed's next move.",
           "Higher than forecast means a weaker job market: rate cuts look more likely, so "
           "yields usually fall. NQ's reaction is mixed: lower yields help, a weaker economy "
           "does not.",
           "Lower than forecast means a tight job market: yields usually rise. NQ's reaction "
           "is mixed."),
    _entry("reading", "growth", "Growth and jobs figures",
           "Figures that measure how fast the economy is growing or hiring.",
           "Growth helps company profits, but strong growth can keep rates high.",
           "Stronger than forecast: good for company profits, but it can mean fewer rate "
           "cuts, so yields usually rise. NQ's reaction depends on which matters more to "
           "traders that day: growth or rates.",
           "Weaker than forecast: rate cuts look more likely and yields usually fall, but "
           "slower growth hurts profits. NQ's reaction is often mixed."),
    _entry("reading", "fed", "Fed",
           "The Federal Reserve's rate decisions, projections and messages.",
           "What counts is the decision and message against what markets expected.",
           "A more hawkish message (rates higher for longer) usually lifts yields and "
           "weighs on NQ.",
           "A more dovish message (cuts sooner or more of them) usually lowers yields and "
           "helps NQ."),
)

#: Event codes per reading family, tried in order (so ``GDP_PRICE_INDEX`` is
#: inflation, not growth). Variants like ``CPI_MOM`` match ``CPI``.
READING_CODES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("fed", ("FOMC", "SEP")),
    ("jobless", ("INITIAL_CLAIMS", "CONTINUING_CLAIMS", "UNEMPLOYMENT")),
    ("inflation", ("CPI", "CORE_CPI", "PPI", "CORE_PPI", "BLS_PPI", "BLS_CORE_PPI", "PCE",
                   "CORE_PCE", "ISM_MFG_PRICES", "AHE", "GDP_PRICE_INDEX")),
    ("growth", ("PAYROLLS", "ADP", "RETAIL_SALES", "CORE_RETAIL_SALES", "GDP", "ISM_MFG",
                "ISM_SERVICES", "INDUSTRIAL_PRODUCTION", "JOLTS", "DURABLE_GOODS",
                "HOUSING_STARTS", "BUILDING_PERMITS", "EXISTING_HOME_SALES",
                "NEW_HOME_SALES", "EMPIRE_STATE_MFG", "PHILLY_FED_MFG")),
)


def reading_family(code: str) -> str | None:
    """The reading family for an event code, or None (Fed minutes, speeches...)."""
    for family, prefixes in READING_CODES:
        if any(code == prefix or code.startswith(prefix + "_") for prefix in prefixes):
            return family
    return None


# --------------------------------------------------------------------------- indicators

_INFL_UP = "Hotter inflation makes Fed rate cuts less likely; yields usually rise and NQ " \
           "usually falls."
_INFL_DOWN = "Cooler inflation makes rate cuts more likely; yields usually fall and NQ " \
             "usually rises."
_GROWTH_UP = "A stronger economy is good for profits but can mean fewer rate cuts; NQ's " \
             "reaction depends on which matters more that day."
_GROWTH_DOWN = "A weaker economy makes rate cuts more likely but hurts profits; NQ's " \
               "reaction is often mixed."
_HOUSING_UP = "More housing activity points to a firmer economy. Usually a small mover for NQ."
_HOUSING_DOWN = "Less housing activity points to a softer economy. Usually a small mover for NQ."


def _indicator(code: str, title: str, what: str, why_nq: str,
               rising: str | None = None, falling: str | None = None) -> Explainer:
    return _entry("indicator", code, title, what, why_nq, rising, falling)


INDICATORS: tuple[Explainer, ...] = (
    _indicator("CPI", "Consumer prices (CPI)",
               "Inflation for everyday goods and services.",
               "Hotter inflation makes Fed rate cuts less likely, which usually hurts Nasdaq.",
               _INFL_UP, _INFL_DOWN),
    _indicator("CORE_CPI", "Core CPI", "Inflation excluding food and energy.",
               "The Fed watches this closely, so it often moves NQ more than headline CPI.",
               _INFL_UP, _INFL_DOWN),
    _indicator("PPI", "Producer prices (PPI)", "Inflation at the wholesale level.",
               "It often leads consumer inflation.", _INFL_UP, _INFL_DOWN),
    _indicator("CORE_PPI", "Core PPI", "Wholesale inflation excluding food and energy.",
               "A hint at where consumer inflation may go.", _INFL_UP, _INFL_DOWN),
    _indicator("BLS_PPI", "Producer prices (PPI, BLS)",
               "Wholesale inflation as published by the Bureau of Labor Statistics.",
               "It often leads consumer inflation.", _INFL_UP, _INFL_DOWN),
    _indicator("BLS_CORE_PPI", "Core PPI (BLS)",
               "Wholesale inflation excluding food and energy, from the Bureau of Labor "
               "Statistics.",
               "A hint at where consumer inflation may go.", _INFL_UP, _INFL_DOWN),
    _indicator("PCE", "PCE inflation", "The Fed's preferred inflation measure.",
               "Matters a lot for rate expectations.", _INFL_UP, _INFL_DOWN),
    _indicator("CORE_PCE", "Core PCE inflation",
               "The Fed's preferred inflation measure, excluding food and energy.",
               "The Fed's own forecasts are written in this number.", _INFL_UP, _INFL_DOWN),
    _indicator("AHE", "Average hourly earnings",
               "How fast wages are rising; part of the jobs report.",
               "Fast wage growth can keep inflation up, which matters for rates.",
               _INFL_UP, _INFL_DOWN),
    _indicator("GDP_PRICE_INDEX", "GDP price index",
               "Inflation across the whole economy, published with GDP.",
               "Usually a smaller mover than CPI or PCE.", _INFL_UP, _INFL_DOWN),
    _indicator("PAYROLLS", "Jobs report (payrolls)", "How many jobs the economy added.",
               "The biggest monthly data release; it often moves NQ sharply.",
               _GROWTH_UP, _GROWTH_DOWN),
    _indicator("UNEMPLOYMENT", "Unemployment rate", "Share of people looking for work.",
               "A rising rate points to a weaker job market and more rate cuts.",
               "Higher than forecast: a weaker job market. Rate cuts look more likely; NQ's "
               "reaction is mixed.",
               "Lower than forecast: a tight job market. Yields usually rise; NQ's reaction "
               "is mixed."),
    _indicator("INITIAL_CLAIMS", "Jobless claims",
               "Weekly count of new unemployment filings.",
               "Rising claims signal a weakening job market.",
               "More filings than forecast: a softer job market. Yields usually fall; NQ's "
               "reaction is mixed.",
               "Fewer filings than forecast: a tight job market. Yields usually rise; NQ's "
               "reaction is mixed."),
    _indicator("CONTINUING_CLAIMS", "Continuing jobless claims",
               "How many people are still claiming unemployment benefits.",
               "It shows whether people who lose jobs find new ones quickly.",
               "More than forecast: finding work is getting harder. NQ's reaction is mixed.",
               "Fewer than forecast: the job market is holding up. NQ's reaction is mixed."),
    _indicator("JOLTS", "Job openings (JOLTS)", "How many jobs employers are trying to fill.",
               "A read on how hot the job market is.", _GROWTH_UP, _GROWTH_DOWN),
    _indicator("ADP", "ADP private jobs", "A private-sector estimate of hiring.",
               "A preview of the official jobs report two days later; usually a smaller "
               "mover.", _GROWTH_UP, _GROWTH_DOWN),
    _indicator("RETAIL_SALES", "Retail sales", "How much consumers spent in shops.",
               "Consumers drive most of the US economy.", _GROWTH_UP, _GROWTH_DOWN),
    _indicator("CORE_RETAIL_SALES", "Core retail sales", "Retail spending excluding cars.",
               "A steadier read on consumer spending.", _GROWTH_UP, _GROWTH_DOWN),
    _indicator("GDP", "GDP (economic growth)",
               "The broadest measure of how fast the economy grows.",
               "It is backward-looking, so it usually moves NQ less than jobs or inflation.",
               _GROWTH_UP, _GROWTH_DOWN),
    _indicator("INDUSTRIAL_PRODUCTION", "Industrial production",
               "Output from factories, mines and utilities.", "A medium mover for NQ.",
               _GROWTH_UP, _GROWTH_DOWN),
    _indicator("DURABLE_GOODS", "Durable goods orders",
               "Orders for long-lasting goods like machines and planes.",
               "A hint at business investment; usually a medium mover.",
               _GROWTH_UP, _GROWTH_DOWN),
    _indicator("ISM_MFG", "ISM manufacturing survey",
               "Purchasing managers' view of factories. Above 50 means growth.",
               "An early read on the economy each month.", _GROWTH_UP, _GROWTH_DOWN),
    _indicator("ISM_MFG_PRICES", "ISM prices paid", "Price pressure reported by factories.",
               "An early hint at inflation.", _INFL_UP, _INFL_DOWN),
    _indicator("ISM_SERVICES", "ISM services survey",
               "Purchasing managers' view of services. Above 50 means growth.",
               "Services are most of the US economy.", _GROWTH_UP, _GROWTH_DOWN),
    _indicator("EMPIRE_STATE_MFG", "Empire State manufacturing",
               "A New York Fed survey of factories in New York State.",
               "An early, regional read on manufacturing; usually a small mover.",
               _GROWTH_UP, _GROWTH_DOWN),
    _indicator("PHILLY_FED_MFG", "Philly Fed manufacturing",
               "A Philadelphia Fed survey of factories in its region.",
               "An early, regional read on manufacturing; usually a small mover.",
               _GROWTH_UP, _GROWTH_DOWN),
    _indicator("EXISTING_HOME_SALES", "Existing home sales", "Sales of previously owned homes.",
               "Housing is sensitive to interest rates.", _HOUSING_UP, _HOUSING_DOWN),
    _indicator("NEW_HOME_SALES", "New home sales", "Sales of newly built homes.",
               "Housing is sensitive to interest rates.", _HOUSING_UP, _HOUSING_DOWN),
    _indicator("HOUSING_STARTS", "Housing starts", "New homes started this month.",
               "Housing is sensitive to interest rates.", _HOUSING_UP, _HOUSING_DOWN),
    _indicator("BUILDING_PERMITS", "Building permits", "Permits for future home building.",
               "A hint at future construction.", _HOUSING_UP, _HOUSING_DOWN),
    _indicator("CORPORATE_PROFITS", "Corporate profits",
               "Total US company profits, published with GDP.",
               "Usually a small mover: company earnings reports matter far more for NQ."),
    _indicator("FOMC", "Fed rate decision", "The Federal Reserve sets interest rates.",
               "The single biggest scheduled event for markets.",
               "A higher rate or a more hawkish message than expected usually lifts yields "
               "and weighs on NQ.",
               "A lower rate or a more dovish message than expected usually lowers yields "
               "and helps NQ."),
    _indicator("FOMC_STATEMENT", "Fed statement",
               "The written statement released with the Fed's rate decision.",
               "Markets read its wording for hints about the next moves."),
    _indicator("FOMC_PRESS_CONFERENCE", "Fed chair press conference",
               "The Fed chair explains the decision and takes questions.",
               "Markets often swing during it."),
    _indicator("FOMC_MINUTES", "Fed meeting minutes",
               "Detailed notes from the last Fed meeting, released three weeks later.",
               "Markets look for how many officials leaned toward higher or lower rates."),
    _indicator("SEP", "Fed projections",
               "Fed officials' forecasts for rates, growth and inflation, published four "
               "times a year with the decision.",
               "The 'dot plot' shows how many rate cuts officials expect.",
               "Higher rate projections than expected usually lift yields and weigh on NQ.",
               "Lower rate projections than expected usually help NQ."),
    _indicator("BEIGE_BOOK", "Fed Beige Book", "The Fed's survey of business conditions.",
               "Usually a small mover for NQ."),
    _indicator("FED_SPEECH", "Fed speech",
               "A speech by a Fed official.",
               "Fed officials' speeches can change what markets expect for interest rates. "
               "The Chair's words matter most; markets listen for hints about the next rate "
               "decision."),
    _indicator("FED_TESTIMONY", "Fed testimony",
               "A Fed official answering questions in Congress, usually the Chair.",
               "Markets listen for hints about the next rate decision."),
)

# --------------------------------------------------------------------------- topics

TOPICS: tuple[Explainer, ...] = (
    _entry("topic", "rates", "Bonds & rates",
           "Treasury yields are the interest rate the US government pays to borrow. The "
           "2-year follows what traders expect the Fed to do with rates; the 10-year "
           "reflects longer-run growth and inflation.",
           "Rising yields usually weigh on Nasdaq stocks, because the future profits that "
           "tech prices depend on are worth less today when safe bonds pay more. Falling "
           "yields usually help.",
           "Usually weighs on NQ.", "Usually helps NQ."),
    _entry("topic", "treasury_auction", "Treasury auctions",
           "The US Treasury sells new government debt at auction.",
           "Strong demand tends to pull yields down; weak demand can push yields up, which "
           "often weighs on Nasdaq stocks."),
)

ENTRIES: tuple[Explainer, ...] = (
    *INSTRUMENTS, *INDICATORS, *LEVELS, *SEGMENTS, *READINGS, *TOPICS)
LIBRARY: dict[str, Explainer] = {e.key: e for e in ENTRIES}

#: Entries whose wording the author was unsure of; listed in the review document.
UNSURE: dict[str, str] = {
    "instrument:NQ": "The rising/falling lines point to the rest of the strip rather than "
                     "giving a rule of thumb. Is that useful?",
    "instrument:WTI": "Oil's link to NQ is loose; 'a crash can signal a slowdown' may be "
                      "more than a beginner needs.",
    "level:prior_settle": "Rising/falling read as 'up/down on the day', which is a fact, "
                          "not a rule of thumb.",
    "level:prior_week_high": "No 'below it' line, and prior_week_low has no 'above it': "
                             "nothing useful to say.",
    "indicator:CORPORATE_PROFITS": "Written new; the desk rarely sees this code.",
    "indicator:FOMC_STATEMENT": "Written new; overlaps with the Fed rate decision entry.",
    "indicator:SEP": "Says 'four times a year'; the 'dot plot' term may need its own entry.",
    "indicator:UNEMPLOYMENT": "Higher unemployment is filed under 'jobless' (rate cuts) but "
                              "the jobs report's headline is payrolls; check the wording "
                              "reads naturally.",
}


def get(kind: Kind, name: str) -> Explainer | None:
    return LIBRARY.get(f"{kind}:{name}")


def instrument(symbol: str) -> Explainer | None:
    return get("instrument", symbol)


def indicator(code: str) -> Explainer | None:
    return get("indicator", code)


def level(key: str) -> Explainer:
    return LIBRARY[f"level:{key}"]


def segment(name: str) -> Explainer:
    return LIBRARY[f"segment:{name}"]


def reading(family: str) -> Explainer:
    return LIBRARY[f"reading:{family}"]


def topic(name: str) -> Explainer:
    return LIBRARY[f"topic:{name}"]


# --------------------------------------------------------------------------- review doc

_KIND_HEADINGS: dict[Kind, str] = {
    "instrument": "Markets (the quote strip)",
    "indicator": "Calendar releases (indicators)",
    "level": "Key price levels",
    "segment": "Parts of the futures day",
    "reading": "What-to-watch readings",
    "topic": "Topics",
}


def review_markdown() -> str:
    """The review document: every entry, in one readable pass."""
    out = [
        "# Explainer library: wording review",
        "",
        "Generated from `core/desk/explainers.py` by `python -m core.desk.explainers`. "
        "Do not edit by hand: change the library and regenerate.",
        "",
        "Every hover explainer in Omarkets comes from this list. Read each entry for "
        "clarity; entries marked **⚠ unsure** are ones the author was unsure of, with the "
        "reason. Rules of thumb only, never advice.",
        "",
    ]
    for kind, heading in _KIND_HEADINGS.items():
        out += [f"## {heading}", ""]
        for e in (e for e in ENTRIES if e.kind == kind):
            out.append(f"### {e.title} (`{e.key}`)")
            out.append("")
            if e.key in UNSURE:
                out += [f"> **⚠ unsure:** {UNSURE[e.key]}", ""]
            out.append(f"- **What it is:** {e.what}")
            out.append(f"- **Why it matters for NQ:** {e.why_nq}")
            if e.rising:
                out.append(f"- **{e.rising_label}:** {e.rising}")
            if e.falling:
                out.append(f"- **{e.falling_label}:** {e.falling}")
            out.append("")
    return "\n".join(out)


if __name__ == "__main__":
    sys.stdout.write(review_markdown())


__all__ = [
    "ENTRIES", "INDICATORS", "INSTRUMENTS", "LEVELS", "LIBRARY", "READINGS",
    "READING_CODES", "SEGMENTS", "TOPICS", "UNSURE", "Explainer", "get", "indicator",
    "instrument", "level", "reading", "reading_family", "review_markdown", "segment", "topic",
]
