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
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from core.domain.chart import RiskInput, RiskTone
from core.domain.sessions import NEW_YORK, volatility_index_open

NQ_PCT = 0.25
US2Y_BP = 3.0
DXY_PCT = 0.25
VIX_PCT = 5.0
VIX_HIGH = 25.0

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


def _input(symbol: str, label: str, inst: Any, *, now: datetime) -> RiskInput:
    value = getattr(inst, "value", None) if inst is not None else None
    if symbol == "US2Y":
        raw = getattr(inst, "change", None) if inst is not None else None
        change = None if raw is None else round(raw * 100.0, 1)
        text = "—" if change is None else _bp(change)
        threshold = US2Y_BP
    else:
        change = getattr(inst, "change_pct", None) if inst is not None else None
        change = None if change is None else round(change, 2)
        text = "—" if change is None else _pct(change)
        threshold = {"NQ": NQ_PCT, "DXY": DXY_PCT, "VIX": VIX_PCT}[symbol]

    status = _status(inst) if inst is not None else "unavailable"
    closed = status == "stale" and (
        bool(getattr(inst, "market_closed", False))
        or (symbol == "VIX" and not volatility_index_open(now)))
    if value is None or change is None or status == "unavailable":
        return RiskInput(symbol=symbol, label=label, value=value, change=change,
                         change_text=text, signal=None, used=False,
                         note=f"{label}: no usable quote, left out.")
    if status == "stale" and not closed:
        return RiskInput(symbol=symbol, label=label, value=value, change=change,
                         change_text=text, signal=None, used=False,
                         note=f"{label}: no fresh quote, left out.")

    moved = 1 if change >= threshold else -1 if change <= -threshold else 0
    # Rising NQ helps; rising yields, dollar and VIX hurt.
    signal = moved if symbol == "NQ" else -moved
    if symbol == "VIX" and value >= VIX_HIGH:
        signal = -1
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


__all__ = ["RULES", "risk_tone"]
