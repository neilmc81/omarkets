"""Risk tone rules: thresholds, labels, stale and missing inputs, VIX after its close."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from core.analysis.risk_tone import nq_effect, risk_tone

# Thursday Oct 22 2026, 10:00 ET: VIX is being computed.
OPEN = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)
# Friday Oct 23 2026, 01:55 ET: VIX is not computed.
NIGHT = datetime(2026, 10, 23, 5, 55, tzinfo=UTC)


def inst(value, *, pct=None, change=None, status="live"):
    return SimpleNamespace(value=value, change_pct=pct, change=change, status=status,
                           timestamp=datetime(2026, 10, 22, 20, 15, tzinfo=UTC))


def state(nq=0.0, us2y_bp=0.0, dxy=0.0, vix=0.0, vix_level=18.0, stale=()):
    def status(symbol):
        return "stale" if symbol in stale else "live"
    return SimpleNamespace(instruments={
        "NQ": inst(29_000, pct=nq, status=status("NQ")),
        "US2Y": inst(4.6, change=us2y_bp / 100, status=status("US2Y")),
        "DXY": inst(101.6, pct=dxy, status=status("DXY")),
        "VIX": inst(vix_level, pct=vix, status=status("VIX")),
    })


@pytest.mark.parametrize(("market", "tone"), [
    (state(nq=0.5, us2y_bp=-4), "risk_on"),
    (state(nq=-0.5, vix=8), "risk_off"),
    (state(nq=0.3), "leaning_risk_on"),
    (state(us2y_bp=3.5), "leaning_risk_off"),
    (state(nq=0.3, us2y_bp=4), "mixed"),
    (state(nq=0.1, us2y_bp=1, dxy=0.1, vix=2), "quiet"),
])
def test_labels_follow_the_signal_counts(market, tone):
    assert risk_tone(market, now=OPEN).tone == tone


def test_thresholds_are_inclusive_and_signs_follow_the_rules():
    tone = risk_tone(state(nq=0.25, us2y_bp=-3, dxy=-0.25, vix=-5), now=OPEN)
    assert [i.signal for i in tone.inputs] == [1, 1, 1, 1]
    assert tone.tone == "risk_on"


def test_vix_at_25_or_above_counts_against_risk_even_when_flat():
    tone = risk_tone(state(vix=0.0, vix_level=26.0), now=OPEN)
    vix = tone.inputs[3]
    assert vix.signal == -1 and "fear gauge is high" in vix.note


def test_stale_inputs_are_left_out():
    tone = risk_tone(state(nq=0.5, us2y_bp=5, stale=("US2Y",)), now=OPEN)
    us2y = tone.inputs[1]
    assert not us2y.used and us2y.signal is None and "no fresh quote" in us2y.note
    assert tone.tone == "leaning_risk_on"


def test_stale_vix_after_its_close_is_used_and_called_closed():
    tone = risk_tone(state(nq=0.1, vix=14, stale=("VIX",)), now=NIGHT)
    vix = tone.inputs[3]
    assert vix.used and vix.signal == -1
    assert vix.note.startswith("VIX closed +14.00% (Thu 16:15 ET)")
    # During Cboe hours a stale VIX is just stale.
    assert not risk_tone(state(nq=0.1, vix=14, stale=("VIX",)), now=OPEN).inputs[3].used


def test_missing_nq_or_too_few_inputs_is_unclear():
    market = state(us2y_bp=5)
    market.instruments["NQ"] = inst(None, pct=None, status="unavailable")
    assert risk_tone(market, now=OPEN).tone == "unclear"
    only_nq = SimpleNamespace(instruments={"NQ": inst(29_000, pct=1.0)})
    tone = risk_tone(only_nq, now=OPEN)
    assert tone.tone == "unclear"
    assert sum(not i.used for i in tone.inputs) == 3


def test_reasons_put_signals_first_and_left_out_inputs_last():
    tone = risk_tone(state(us2y_bp=4), now=OPEN)
    assert tone.reasons[0].startswith("2-year yield +4.0bp")
    assert tone.rules.startswith("Rules:")


# --- effect on NQ, per quote ------------------------------------------------


def quote(value, *, pct=None, change=None, status="live", market_closed=False):
    return SimpleNamespace(value=value, change_pct=pct, change=change, status=status,
                           market_closed=market_closed)


@pytest.mark.parametrize(("symbol", "row", "effect"), [
    # Equity futures move with NQ.
    ("NQ", quote(29_000, pct=0.25), "helps"),
    ("ES", quote(7_600, pct=-1.79), "hurts"),
    ("YM", quote(47_000, pct=0.1), "neutral"),
    # Rising yields hurt; falling yields help (the change is in percentage points).
    ("US2Y", quote(4.59, change=-0.064), "helps"),
    ("US10Y", quote(4.93, change=0.03), "hurts"),
    ("US10Y", quote(4.93, change=0.029), "neutral"),
    # A firmer dollar hurts.
    ("DXY", quote(101.96, pct=0.25), "hurts"),
    ("DXY", quote(101.96, pct=-0.07), "neutral"),
    # Rising fear hurts; a high gauge hurts even when flat.
    ("VIX", quote(19.42, pct=12.91), "hurts"),
    ("VIX", quote(15.0, pct=-5.0), "helps"),
    ("VIX", quote(25.0, pct=0.0), "hurts"),
    ("VXN", quote(28.5, pct=13.19), "hurts"),
    ("VXN", quote(22.0, pct=-5.0), "helps"),
    ("VXN", quote(29.0, pct=0.0), "neutral"),
    ("VXN", quote(30.0, pct=-6.0), "hurts"),
])
def test_nq_effect_follows_the_risk_tone_rules(symbol, row, effect):
    assert nq_effect(symbol, row, now=OPEN) == effect


def test_nq_effect_agrees_with_the_risk_tone_signals():
    market = state(nq=0.5, us2y_bp=4, dxy=-0.3, vix=1)
    tone = risk_tone(market, now=OPEN)
    effects = {"helps": 1, "hurts": -1, "neutral": 0}
    for row in tone.inputs:
        effect = nq_effect(row.symbol, market.instruments[row.symbol], now=OPEN)
        assert effects[effect] == row.signal


def test_nq_effect_leaves_out_what_the_rules_leave_out():
    assert nq_effect("US2Y", quote(4.6, change=0.05, status="stale"), now=OPEN) is None
    assert nq_effect("NQ", quote(None, pct=None, status="unavailable"), now=OPEN) is None
    assert nq_effect("ES", quote(7_600, pct=None), now=OPEN) is None
    assert nq_effect("ES", None, now=OPEN) is None
    # Instruments the rules do not cover have no effect rather than a guess.
    assert nq_effect("GOLD", quote(4_000, pct=2.0), now=OPEN) is None
    assert nq_effect("NVDA", quote(180, pct=-3.0), now=OPEN) is None


def test_nq_effect_reads_a_closed_market_as_its_last_session():
    closed = quote(28_542, pct=-3.45, status="stale", market_closed=True)
    assert nq_effect("NQ", closed, now=NIGHT) == "hurts"
    # Cash volatility gauges after Cboe hours are their close, not stale.
    assert nq_effect("VXN", quote(28.5, pct=13.19, status="stale"), now=NIGHT) == "hurts"
    assert nq_effect("VXN", quote(28.5, pct=13.19, status="stale"), now=OPEN) is None
