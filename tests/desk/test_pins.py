"""Chart pins and the next high-impact event (deterministic, no AI)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from core.desk import pins as PN
from core.desk import playbook as P
from core.desk.links import ReleaseNotes

# Thursday Oct 22 2026, 08:30 ET.
CLAIMS = datetime(2026, 10, 22, 12, 30, tzinfo=UTC)


def row(at, *, code="INITIAL_CLAIMS", name="Unemployment Claims", actual=197_000.0,
        consensus=201_000.0, importance="high"):
    return SimpleNamespace(event_code=code, name=name, scheduled_at=at, actual=actual,
                           consensus=consensus, previous=200_000.0, unit="count",
                           importance=importance, status="released", country="US",
                           statement=None)


def obs(ts, value):
    return SimpleNamespace(timestamp=ts, value=value)


def history_of(series):
    def history(symbol, *, start=None, end=None, limit=500):
        return [o for o in series.get(symbol, []) if start <= o.timestamp <= end][-limit:]
    return history


NQ = {"NQ": [obs(CLAIMS - timedelta(seconds=20), 29_000.0),
             obs(CLAIMS + timedelta(minutes=14), 28_982.6)]}


def test_released_pin_carries_result_move_and_note():
    note = "release:20261022T1230:INITIAL_CLAIMS"
    [pin] = PN.pins([row(CLAIMS)], history_of(NQ), start=CLAIMS - timedelta(hours=1),
                    end=CLAIMS + timedelta(hours=1), now=CLAIMS + timedelta(hours=1),
                    notes=ReleaseNotes([note]))
    assert pin.importance == "high" and pin.released
    assert pin.result_text == "Lower than forecast"
    assert pin.nq_15m == -0.06 and pin.nq_15m_text == "NQ -0.06% in 15 min"
    assert pin.note_id == note
    assert pin.explainer


def test_move_waits_for_the_delayed_feed_and_upcoming_pins_have_no_result():
    early = PN.pins([row(CLAIMS)], history_of(NQ), start=CLAIMS - timedelta(hours=1),
                    end=CLAIMS + timedelta(hours=1), now=CLAIMS + timedelta(minutes=20),
                    notes=ReleaseNotes([]))
    assert early[0].released and early[0].nq_15m is None and early[0].note_id is None
    upcoming = PN.pins([row(CLAIMS, actual=None)], history_of({}),
                       start=CLAIMS - timedelta(hours=1), end=CLAIMS + timedelta(hours=1),
                       now=CLAIMS - timedelta(minutes=5), notes=ReleaseNotes([]))
    assert not upcoming[0].released and upcoming[0].result_text is None


def test_medium_pins_are_kept_low_and_speeches_are_left_out():
    rows = [row(CLAIMS, importance="medium"),
            row(CLAIMS + timedelta(hours=1), importance="low"),
            row(CLAIMS + timedelta(hours=2), code="FED_SPEECH", name="Speech",
                actual=None, consensus=None)]
    found = PN.pins(rows, history_of({}), start=CLAIMS - timedelta(hours=1),
                    end=CLAIMS + timedelta(hours=4), now=CLAIMS, notes=ReleaseNotes([]))
    assert [(p.at, p.importance) for p in found] == [(CLAIMS, "medium")]


def test_next_event_is_the_next_high_release_with_its_playbook_line():
    now = CLAIMS + timedelta(days=6)
    past = [CLAIMS - timedelta(days=7 * k) for k in range(3)]
    series = {"NQ": [o for at in past for o in (obs(at - timedelta(seconds=20), 29_000.0),
                                                 obs(at + timedelta(minutes=14), 28_900.0))]}
    book = P.build([row(at) for at in past], history_of(series), now=now)
    nxt_at = CLAIMS + timedelta(days=7)
    rows = [row(now + timedelta(hours=1), importance="medium"),
            row(nxt_at, actual=None)]
    event = PN.next_event(rows, book, now=now)
    assert event.at == nxt_at and event.importance == "high" and event.kind == "economic"
    assert event.forecast_text == "Unemployment Claims: forecast 201K"
    assert event.playbook_text == (
        "Unemployment Claims, first 15 min: Below forecast (3): NQ fell 3. "
        "Only 3 measured: examples, not a pattern.")


def test_next_event_without_history_or_events():
    now = CLAIMS - timedelta(hours=1)
    event = PN.next_event([row(CLAIMS, actual=None)], None, now=now)
    assert event is not None and event.playbook_text is None
    empty = P.build([], history_of({}), now=now)
    assert PN.next_event([row(CLAIMS, actual=None)], empty, now=now).playbook_text == (
        "No earlier releases measured yet.")
    assert PN.next_event([], empty, now=now) is None
