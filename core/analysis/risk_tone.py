"""Risk tone: a rule-based read of NQ, the 2-year yield, the dollar and VIX.

No AI. Each input gives a signal for risk appetite from its change since the
previous close, using fixed thresholds:

====================  =====================  =====================
Input                 +1 (helps risk)        -1 (hurts risk)
====================  =====================  =====================
NQ futures            up 0.25% or more       down 0.25% or more
2-year yield          down 3bp or more       up 3bp or more
US dollar (DXY)       down 0.25% or more     up 0.25% or more
VIX                   down 5% or more        up 5% or more, or at 25 or above
====================  =====================  =====================

Anything smaller is 0 (neutral). The label then follows the count of signals:

* the market is shut (weekend, holiday, daily break) and NQ's quote is the
  last session's close: **closed** — the tone resumes when futures reopen;
* fewer than two usable inputs, or NQ unusable: **unclear**;
* signals both ways: **mixed**;
* two or more helping and none hurting: **risk-on** (mirror: **risk-off**);
* exactly one helping and none hurting: **leaning risk-on** (mirror);
* none either way: **quiet**.

An input is left out when its value or change is unavailable, or when its quote
is stale. VIX is the exception: it is a cash index computed only 03:15–16:15 ET,
so a VIX quote that stopped outside those hours is its close, which still
describes the latest session; it is used and worded as "closed". Thresholds are
typical day-to-day moves, not forecasts.

**Effect on NQ** (:func:`nq_effect`) applies the same rules to one quote at a
time, for every instrument the popup shows, so the shell colours a row by what
the move means for NQ rather than by its raw direction:

====================  =====================  =====================
Input                 helps NQ               hurts NQ
====================  =====================  =====================
NQ, ES, YM futures    up 0.25% or more       down 0.25% or more
2- and 10-year yield  down 3bp or more       up 3bp or more
US dollar (DXY)       down 0.25% or more     up 0.25% or more
VIX                   down 5% or more        up 5% or more, or at 25 or above
VXN                   down 5% or more        up 5% or more, or at 30 or above
====================  =====================  =====================

A smaller move is "neutral"; a quote that would be left out of the risk tone has
no effect (``None``). VXN usually sits a few points above VIX, so its "high"
mark is higher.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from core.domain.chart import RiskInput, RiskTone
from core.domain.sessions import NEW_YORK, volatility_index_open

NQ_PCT = 0.25
US2Y_BP = 3.0
DXY_PCT = 0.25
VIX_PCT = 5.0
VIX_HIGH = 25.0
US10Y_BP = US2Y_BP
VXN_PCT = VIX_PCT
VXN_HIGH = 30.0

#: Size of a move that counts, per instrument (percent; basis points for yields).
THRESHOLDS: dict[str, float] = {
    "NQ": NQ_PCT, "ES": NQ_PCT, "YM": NQ_PCT,
    "US2Y": US2Y_BP, "US10Y": US10Y_BP,
    "DXY": DXY_PCT, "VIX": VIX_PCT, "VXN": VXN_PCT,
}
#: +1 when the instrument rising helps NQ, -1 when it hurts.
NQ_DIRECTION: dict[str, int] = {
    "NQ": 1, "ES": 1, "YM": 1,
    "US2Y": -1, "US10Y": -1, "DXY": -1, "VIX": -1, "VXN": -1,
}
#: A volatility gauge at or above this level counts against NQ even when flat.
HIGH_LEVEL: dict[str, float] = {"VIX": VIX_HIGH, "VXN": VXN_HIGH}
YIELDS = frozenset({"US2Y", "US10Y"})
VOLATILITY = frozenset({"VIX", "VXN"})

NqEffect = Literal["helps", "hurts", "neutral"]
EFFECTS: dict[int, NqEffect] = {1: "helps", -1: "hurts", 0: "neutral"}

RULES = (
    "Rules: NQ ±0.25%, 2-year yield ±3bp, dollar ±0.25%, VIX ±5% (or at 25+) since the "
    "previous close. Rising NQ, falling yields, a falling dollar and a falling VIX count as "
    "risk-on; the opposite as risk-off. Stale quotes are left out."
)

LABELS = {
    "risk_on": "risk-on",
    "leaning_risk_on": "leaning risk-on",
    "quiet": "quiet",
    "mixed": "mixed",
    "leaning_risk_off": "leaning risk-off",
    "risk_off": "risk-off",
    "unclear": "unclear",
    "closed": "markets closed",
}


def _status(inst: Any) -> str:
    status = getattr(inst, "status", None)
    return str(getattr(status, "value", status) or "unavailable")


def _pct(value: float) -> str:
    return f"{'+' if value > 0 else '-' if value < 0 else ''}{abs(value):.2f}%"


def _bp(value: float) -> str:
    return f"{'+' if value > 0 else '-' if value < 0 else ''}{abs(value):.1f}bp"


def _closed_at(inst: Any) -> str:
    at = getattr(inst, "timestamp", None)
    return at.astimezone(NEW_YORK).strftime("%a %H:%M ET") if at else "its last close"


class _Reading:
    """One quote read under the rules: its change, and its signal for NQ.

    ``signal`` is None when the quote is left out; ``left_out`` then says why.
    """

    def __init__(self, symbol: str, inst: Any, *, now: datetime) -> None:
        self.value = getattr(inst, "value", None) if inst is not None else None
        if symbol in YIELDS:
            raw = getattr(inst, "change", None) if inst is not None else None
            self.change = None if raw is None else round(raw * 100.0, 1)
            self.text = "—" if self.change is None else _bp(self.change)
        else:
            change = getattr(inst, "change_pct", None) if inst is not None else None
            self.change = None if change is None else round(change, 2)
            self.text = "—" if self.change is None else _pct(self.change)

        status = _status(inst) if inst is not None else "unavailable"
        self.closed = status == "stale" and (
            bool(getattr(inst, "market_closed", False))
            or (symbol in VOLATILITY and not volatility_index_open(now)))
        self.signal: int | None = None
        self.left_out: str | None = None
        if self.value is None or self.change is None or status == "unavailable":
            self.left_out = "no usable quote"
        elif status == "stale" and not self.closed:
            self.left_out = "no fresh quote"
        else:
            threshold = THRESHOLDS[symbol]
            moved = 1 if self.change >= threshold else -1 if self.change <= -threshold else 0
            # Rising NQ helps; rising yields, dollar and volatility hurt.
            self.signal = moved * NQ_DIRECTION[symbol]
            if symbol in HIGH_LEVEL and self.value >= HIGH_LEVEL[symbol]:
                self.signal = -1


def nq_effect(symbol: str, inst: Any, *, now: datetime) -> NqEffect | None:
    """What this quote's move means for NQ under the risk-tone rules.

    ``inst`` is anything with ``value``, ``change``, ``change_pct``, ``status``
    and ``market_closed`` (a market-state instrument or a shell quote). None for
    an instrument the rules do not cover, or a quote they would leave out.
    """
    if symbol not in THRESHOLDS or inst is None:
        return None
    signal = _Reading(symbol, inst, now=now).signal
    return None if signal is None else EFFECTS[signal]


def _input(symbol: str, label: str, inst: Any, *, now: datetime) -> RiskInput:
    reading = _Reading(symbol, inst, now=now)
    value, change, text, closed = reading.value, reading.change, reading.text, reading.closed
    if reading.signal is None:
        return RiskInput(symbol=symbol, label=label, value=value, change=change,
                         change_text=text, signal=None, used=False,
                         note=f"{label}: {reading.left_out}, left out.")
    signal = reading.signal
    when = f"closed {text} ({_closed_at(inst)})" if closed else text
    if symbol == "NQ":
        note = (f"NQ {text} since the settle: " + (
            "rising, a sign of appetite for risk." if signal > 0 else
            "falling, a sign of caution." if signal < 0 else "little changed."))
    elif symbol == "US2Y":
        note = (f"2-year yield {text}: " + (
            "rising yields usually weigh on Nasdaq stocks." if signal < 0 else
            "falling yields usually help Nasdaq stocks." if signal > 0 else
            "little changed, not a factor today."))
    elif symbol == "DXY":
        note = (f"Dollar {text}: " + (
            "a firmer dollar often goes with caution." if signal < 0 else
            "a softer dollar often goes with risk appetite." if signal > 0 else
            "little changed."))
    else:
        level = f" at {value:.2f}"
        note = (f"VIX {when}{level}: " + (
            "the fear gauge is high." if value >= VIX_HIGH else
            "rising fear." if signal < 0 else
            "fear easing." if signal > 0 else "fear little changed."))
    return RiskInput(symbol=symbol, label=label, value=value, change=change, change_text=text,
                     signal=signal, used=True, note=note)


def risk_tone(state: Any, *, now: datetime) -> RiskTone:
    """Apply the rules to the current market state."""
    instruments = getattr(state, "instruments", {}) or {}
    inputs = [
        _input("NQ", "NQ futures", instruments.get("NQ"), now=now),
        _input("US2Y", "2-year yield", instruments.get("US2Y"), now=now),
        _input("DXY", "US dollar", instruments.get("DXY"), now=now),
        _input("VIX", "VIX", instruments.get("VIX"), now=now),
    ]
    nq = instruments.get("NQ")
    if nq is not None and getattr(nq, "market_closed", False):
        reasons = ["Markets closed · risk tone resumes when futures reopen.",
                   *(i.note for i in inputs)]
        return RiskTone(generated_at=now, tone="closed", label=LABELS["closed"],
                        reasons=reasons, inputs=inputs, rules=RULES)
    used = [i for i in inputs if i.used]
    helping = sum(1 for i in used if i.signal == 1)
    hurting = sum(1 for i in used if i.signal == -1)
    if len(used) < 2 or not inputs[0].used:
        tone = "unclear"
    elif helping and hurting:
        tone = "mixed"
    elif helping >= 2:
        tone = "risk_on"
    elif hurting >= 2:
        tone = "risk_off"
    elif helping == 1:
        tone = "leaning_risk_on"
    elif hurting == 1:
        tone = "leaning_risk_off"
    else:
        tone = "quiet"
    # Signals first (strongest wording), then neutral inputs, then left-out ones.
    order = sorted(inputs, key=lambda i: (not i.used, i.signal == 0))
    return RiskTone(generated_at=now, tone=tone, label=LABELS[tone],
                    reasons=[i.note for i in order], inputs=inputs, rules=RULES)


__all__ = ["EFFECTS", "RULES", "NqEffect", "nq_effect", "risk_tone"]
