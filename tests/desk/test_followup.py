""""Did it hold?" follow-ups: checkpoints, verdicts and timing around the delayed feed."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from core.desk import followup as FU
from core.desk.models import DeskNote, Move, NoteDraft, NoteKind
from core.desk.service import DeskService, DeskSources
from core.desk.store import DeskStore
from core.desk.writer import WriteResult

# Tuesday Oct 20 2026 (EDT): 12:30 UTC is 08:30 ET, the cash close is 20:00 UTC.
RELEASE = datetime(2026, 10, 20, 12, 30, tzinfo=UTC)
CLOSE = datetime(2026, 10, 20, 20, 0, tzinfo=UTC)


def obs(ts, value):
    return SimpleNamespace(timestamp=ts, value=value)


def nq_move(change, minutes=15, start=RELEASE):
    return Move(symbol="NQ", label="NQ", start_value=100.0, end_value=100.0 + change,
                start_at=start, end_at=start + timedelta(minutes=minutes), change=change,
                change_unit="%", text=f"{change:+.2f}%")


def note(kind=NoteKind.RELEASE, *, event_at=RELEASE, moves=None, note_id="release:x"):
    return DeskNote(id=note_id, kind=kind, title="Consumer prices (CPI)",
                    created_at=event_at + timedelta(minutes=26), event_at=event_at,
                    moves=[nq_move(-0.4)] if moves is None else moves)


def history_for(points):
    def history(symbol, *, start=None, end=None, limit=500):
        return [o for o in points.get(symbol, [])
                if (start is None or o.timestamp >= start) and (end is None or o.timestamp <= end)]
    return history


def series(at_1h, at_close, *, start=RELEASE):
    """NQ quotes from a 30000 start: pct changes at the hour mark and at the close."""
    return {
        "NQ": [obs(start - timedelta(seconds=30), 30000.0),
               obs(start + timedelta(minutes=59), 30000.0 * (1 + at_1h / 100)),
               obs(CLOSE - timedelta(seconds=20), 30000.0 * (1 + at_close / 100))],
        "US10Y": [obs(start - timedelta(seconds=5), 4.20), obs(CLOSE - timedelta(minutes=1), 4.25)],
    }


# --- cash close --------------------------------------------------------------


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        # Summer time: 16:00 EDT is 20:00 UTC.
        (datetime(2026, 10, 20, 12, 30, tzinfo=UTC), datetime(2026, 10, 20, 20, 0, tzinfo=UTC)),
        # Winter time (after Nov 1): 16:00 EST is 21:00 UTC.
        (datetime(2026, 11, 10, 13, 30, tzinfo=UTC), datetime(2026, 11, 10, 21, 0, tzinfo=UTC)),
        # After Friday's close: Monday's close.
        (datetime(2026, 10, 23, 20, 5, tzinfo=UTC), datetime(2026, 10, 26, 20, 0, tzinfo=UTC)),
        # Thanksgiving is closed; the next day closes early at 13:00 EST.
        (datetime(2026, 11, 26, 14, 0, tzinfo=UTC), datetime(2026, 11, 27, 18, 0, tzinfo=UTC)),
        # Exactly at the close: the next one.
        (datetime(2026, 10, 20, 20, 0, tzinfo=UTC), datetime(2026, 10, 21, 20, 0, tzinfo=UTC)),
    ],
)
def test_cash_close_after(moment, expected):
    assert FU.cash_close_after(moment) == expected


# --- verdicts -------------------------------------------------------------


@pytest.mark.parametrize(
    ("first", "later", "verdict"),
    [
        (-0.40, -0.70, "grew"),
        (-0.40, -0.30, "held"),
        (-0.40, -0.10, "faded"),
        (-0.40, 0.05, "faded"),  # crossed zero, but not by a real move
        (-0.40, 0.60, "reversed"),
        (0.50, -0.20, "reversed"),
        (0.05, 0.60, "quiet"),
        (None, 0.6, None),
        (0.4, None, None),
    ],
)
def test_classify(first, later, verdict):
    assert FU.classify(first, later) == verdict


# --- planned checkpoints -----------------------------------------------------


def test_release_gets_the_hour_mark_and_the_close():
    hour = RELEASE + timedelta(hours=1)
    assert FU.planned_checkpoints(note()) == [("1h", hour), ("close", CLOSE)]


def test_press_conference_already_measured_an_hour_so_only_the_close():
    fomc = datetime(2026, 10, 28, 18, 30, tzinfo=UTC)  # 14:30 ET
    plans = FU.planned_checkpoints(note(NoteKind.FED, event_at=fomc,
                                        moves=[nq_move(0.3, minutes=60, start=fomc)]))
    assert plans == [("close", datetime(2026, 10, 28, 20, 0, tzinfo=UTC))]


def test_after_close_earnings_skip_the_maintenance_break_and_use_the_next_close():
    report = datetime(2026, 10, 28, 20, 5, tzinfo=UTC)  # 16:05 ET; +1h is in the break
    plans = FU.planned_checkpoints(note(NoteKind.EARNINGS, event_at=report))
    assert plans == [("close", datetime(2026, 10, 29, 20, 0, tzinfo=UTC))]


def test_other_kinds_and_failed_notes_get_no_follow_up():
    assert FU.planned_checkpoints(note(NoteKind.PREMARKET)) == []
    failed = note().model_copy(update={"status": "failed"})
    assert FU.planned_checkpoints(failed) == []


# --- measuring around the delayed feed ----------------------------------------


def test_checkpoints_wait_for_the_delayed_feed_then_read_plainly():
    history = history_for(series(at_1h=-0.15, at_close=0.6))
    n = note()
    hour = RELEASE + timedelta(hours=1)
    assert FU.update(n, None, history, now=hour + timedelta(minutes=10)) is None  # still arriving
    partial = FU.update(n, None, history, now=hour + timedelta(minutes=11))
    assert partial is not None and not partial.final
    assert partial.text == ("First 15 min: NQ −0.40% · after 1 hour: −0.15%"
                            " — the first reaction is fading so far.")
    assert FU.update(n, partial, history, now=CLOSE + timedelta(minutes=5)) is None
    final = FU.update(n, partial, history, now=CLOSE + timedelta(minutes=11))
    assert final.final and final.verdict == "reversed"
    assert final.text == ("First 15 min: NQ −0.40% · after 1 hour: −0.15% · by the close: +0.60%"
                          " — the first reaction reversed.")
    close = final.checkpoints[-1]
    assert {m.symbol for m in close.moves} == {"NQ", "US10Y"}
    assert next(m for m in close.moves if m.symbol == "US10Y").change == 5.0  # basis points
    # Final: nothing more to do.
    assert FU.update(n, final, history, now=CLOSE + timedelta(hours=3)) is None


def test_missing_nq_waits_then_is_recorded_as_not_available():
    history = history_for({"US10Y": series(0, 0)["US10Y"]})
    hour = RELEASE + timedelta(hours=1)
    assert FU.update(note(), None, history, now=hour + timedelta(minutes=30)) is None
    late = FU.update(note(), None, history, now=hour + timedelta(hours=2, minutes=12))
    assert late is not None and late.verdict is None
    assert late.text == "First 15 min: NQ −0.40% · after 1 hour: not available"


def test_an_old_quote_is_not_used_for_a_checkpoint():
    # The last NQ quote is 40 minutes before the hour mark (feed outage).
    stale = {"NQ": [obs(RELEASE - timedelta(seconds=30), 30000.0),
                    obs(RELEASE + timedelta(minutes=20), 29900.0)]}
    hour = RELEASE + timedelta(hours=1)
    result = FU.update(note(), None, history_for(stale), now=hour + timedelta(hours=2, minutes=12))
    assert result.text.endswith("after 1 hour: not available")


def test_note_without_a_first_reaction_names_nq_on_the_first_number():
    history = history_for(series(at_1h=0.3, at_close=0.3))
    result = FU.update(note(moves=[]), None, history,
                       now=RELEASE + timedelta(hours=1, minutes=11))
    assert result.text == "after 1 hour: NQ +0.30%"


def test_old_notes_get_no_new_follow_up():
    history = history_for(series(0.1, 0.1))
    assert FU.update(note(), None, history, now=RELEASE + timedelta(days=5)) is None


# --- service and storage ---------------------------------------------------------


async def test_tick_stores_follow_up_beside_the_unchanged_note():
    class Writer:
        ready = True
        detail = None
        model = "fake"

        async def write(self, kind, facts):
            return WriteResult(NoteDraft(headline="Inflation ran hotter", summary="Sum.",
                                         what_it_means="Means.", market_reaction="React.",
                                         rates_read="Yields.", learn="Learn.", nq_tone="negative"),
                               "fake", 0.01)

    clock = {"now": CLOSE + timedelta(minutes=12)}
    store = DeskStore(":memory:")
    original = note().model_copy(update={"draft": (await Writer().write(None, None)).draft})
    store.save(original)
    service = DeskService(
        sources=DeskSources(market_state=lambda: SimpleNamespace(instruments={}),
                            history=history_for(series(-0.3, -0.5)),
                            calendar_events=lambda s, e: [], news_events=lambda: []),
        store=store, writer=Writer(), clock=lambda: clock["now"],
    )
    assert await service.update_follow_ups(clock["now"]) == 1
    stored = store.get("release:x")
    assert stored.follow_up.final and stored.follow_up.verdict == "held"
    assert stored.draft == original.draft and stored.moves == original.moves
    assert store.recent()[0].follow_up == stored.follow_up
    assert await service.update_follow_ups(clock["now"] + timedelta(hours=1)) == 0
