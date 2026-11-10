"""Move notes' "did it hold?": the note's own instrument, per unit, session and gap rules."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from core.desk import followup as FU
from core.desk.models import DeskNote, Move, NoteDraft, NoteKind
from core.desk.service import DeskService, DeskSources
from core.desk.store import DeskStore
from core.desk.writer import WriteResult

# Tuesday Oct 20 2026 (EDT): 14:00 UTC is 10:00 ET; the cash close is 20:00 UTC.
START = datetime(2026, 10, 20, 14, 0, tzinfo=UTC)
CLOSE = datetime(2026, 10, 20, 20, 0, tzinfo=UTC)


def obs(ts, value):
    return SimpleNamespace(timestamp=ts, value=value)


def history_for(points):
    def history(symbol, *, start=None, end=None, limit=500):
        return [o for o in points.get(symbol, [])
                if (start is None or o.timestamp >= start) and (end is None or o.timestamp <= end)]
    return history


def mv(symbol, change, unit="%", start=START, minutes=60, label=None):
    return Move(symbol=symbol, label=label or symbol, start_value=100.0, end_value=100.0 + change,
                start_at=start, end_at=start + timedelta(minutes=minutes), change=change,
                change_unit=unit, text=f"{change:+.2f}")


def move_note(symbol="GOLD", change=1.0, unit="%", start=START, minutes=60, *, moves=None,
              members=(), kind=NoteKind.MOVE, label=None):
    return DeskNote(
        id=f"move:{symbol}:x", kind=kind, title="A move", created_at=start + timedelta(minutes=80),
        event_at=start, instrument=symbol, event_members=list(members),
        moves=[mv(symbol, change, unit, start, minutes, label)] if moves is None else moves)


def quotes(symbol, v0, *marks, start=START):
    """Quotes: ``v0`` just before ``start`` and one value just before each checkpoint time."""
    return {symbol: [obs(start - timedelta(seconds=30), v0),
                     *(obs(at - timedelta(seconds=20), value) for at, value in marks)]}


def measured(note, points, *, now):
    return FU.update(note, None, history_for(points), now=now)


# --- the verdict per unit ----------------------------------------------------


@pytest.mark.parametrize(
    ("symbol", "unit", "first", "later", "verdict"),
    [
        # percent: flat is 0.10
        ("GOLD", "%", 1.0, 1.6, "grew"),
        ("GOLD", "%", 1.0, 0.6, "held"),
        ("GOLD", "%", 1.0, 0.05, "faded"),
        ("GOLD", "%", 1.0, -0.2, "reversed"),
        ("GOLD", "%", 0.05, 0.3, "quiet"),
        # basis points: flat is 1 bp, so a 0.5 bp wobble is not a reversal
        ("US10Y", "bp", 6.0, 10.0, "grew"),
        ("US10Y", "bp", 6.0, 3.0, "held"),
        ("US10Y", "bp", 6.0, -0.5, "faded"),
        ("US10Y", "bp", 6.0, -1.5, "reversed"),
        ("US10Y", "bp", 0.5, 4.0, "quiet"),
        # volatility index (percent of its level): flat is 1%
        ("VIX", "%", 8.0, 13.0, "grew"),
        ("VIX", "%", 8.0, 4.5, "held"),
        ("VIX", "%", 8.0, -0.9, "faded"),
        ("VIX", "%", 8.0, -1.5, "reversed"),
        ("VIX", "%", 0.8, 5.0, "quiet"),
    ],
)
def test_verdict_uses_the_flat_size_of_the_unit(symbol, unit, first, later, verdict):
    assert FU.classify(first, later, FU.flat_size(symbol, unit)) == verdict


def test_flat_sizes_per_unit():
    assert FU.flat_size("NQ", "%") == FU.FLAT_PCT == 0.10
    assert FU.flat_size("US2Y", "bp") == FU.FLAT_BP == 1.0
    assert FU.flat_size("VIX", "%") == FU.FLAT_VOL_PCT == 1.0


def test_percent_move_is_measured_from_the_move_start_to_both_checkpoints():
    note = move_note("GOLD", 1.0)
    one_hour = START + timedelta(hours=2)  # an hour after the move ended
    points = quotes("GOLD", 2000.0, (one_hour, 2008.0), (CLOSE, 2030.0))
    assert FU.planned_checkpoints(note) == [("1h", one_hour), ("close", CLOSE)]
    follow = measured(note, points, now=CLOSE + timedelta(minutes=12))
    assert follow.final and follow.verdict == "grew"
    assert [c.label for c in follow.checkpoints] == ["1h", "close"]
    assert [m.symbol for c in follow.checkpoints for m in c.moves] == ["GOLD", "GOLD"]
    assert follow.text == ("The move over 60 min: GOLD +1.00% · after 1 hour: +0.40% · "
                           "by the close: +1.50% — the move grew.")


def test_yield_move_reads_in_basis_points():
    note = move_note("US10Y", 6.0, "bp")
    one_hour = START + timedelta(hours=2)
    points = quotes("US10Y", 4.20, (one_hour, 4.23), (CLOSE, 4.19))
    follow = measured(note, points, now=CLOSE + timedelta(minutes=12))
    assert follow.verdict == "reversed"
    assert "after 1 hour: +3.0 bp" in follow.text and "by the close: −1.0 bp" in follow.text
    assert follow.text.startswith("The move over 60 min: US10Y +6.0 bp")


def test_volatility_index_move_is_measured_in_cboe_hours():
    note = move_note("VIX", 8.0, label="VIX fear gauge")
    one_hour = START + timedelta(hours=2)  # 12:00 ET
    points = quotes("VIX", 20.0, (one_hour, 21.0), (CLOSE, 20.2))  # 16:00 ET: Cboe is open
    follow = measured(note, points, now=CLOSE + timedelta(minutes=12))
    assert follow.verdict == "faded"  # +1% then +1% against +8%: the move has gone
    assert follow.text.startswith("The move over 60 min: VIX fear gauge +8.00%")


# --- shut markets are "not available", never a guess ------------------------------


def test_volatility_index_after_cboe_hours_is_not_available_for_the_hour_mark():
    start = datetime(2026, 10, 20, 19, 15, tzinfo=UTC)  # 15:15 ET: the move ends at 16:15
    note = move_note("VIX", 6.0, start=start)
    one_hour = start + timedelta(hours=2)  # 17:15 ET: Cboe is shut
    # Quotes exist (a stale last close), but must not be used.
    next_close = datetime(2026, 10, 21, 20, 0, tzinfo=UTC)
    points = quotes("VIX", 20.0, (one_hour, 21.0), (next_close, 22.0), start=start)
    follow = FU.update(note, None, history_for(points), now=one_hour)
    assert [c.label for c in follow.checkpoints] == ["1h"]
    assert follow.checkpoints[0].moves == [] and not follow.final
    assert follow.text == ("The move over 60 min: VIX +6.00% · after 1 hour: not available")
    assert follow.verdict is None


def test_volatility_index_is_shut_from_cboes_close_with_no_grace():
    assert FU.market_open("VIX", datetime(2026, 10, 20, 20, 15, tzinfo=UTC))  # 16:15 ET
    assert not FU.market_open("VIX", datetime(2026, 10, 20, 20, 15, 1, tzinfo=UTC))
    # A move ending 15:20 ET: its hour mark, 16:20, is after the close, so the 16:15
    # closing quote must not be read as that checkpoint.
    start = datetime(2026, 10, 20, 18, 20, tzinfo=UTC)
    note = move_note("VIX", 6.0, start=start)
    mark = start + timedelta(hours=2)
    points = {"VIX": [obs(start - timedelta(seconds=30), 20.0),
                      obs(datetime(2026, 10, 20, 20, 15, tzinfo=UTC), 23.0)]}
    follow = FU.update(note, None, history_for(points), now=mark + timedelta(minutes=12))
    assert follow.checkpoints[0].moves == [] and follow.verdict is None


def test_a_close_after_a_long_weekend_is_still_measured():
    # Thursday Apr 2 2026, 15:30-16:30 ET; Good Friday is a holiday, so the close is
    # Monday Apr 6 16:00 ET, more than four days after the move began.
    start = datetime(2026, 4, 2, 19, 30, tzinfo=UTC)
    note = move_note("ES", 0.9, start=start)
    close = datetime(2026, 4, 6, 20, 0, tzinfo=UTC)
    assert FU.planned_checkpoints(note)[-1] == ("close", close)
    points = quotes("ES", 100.0, (close, 101.0), start=start)
    follow = FU.update(note, None, history_for(points), now=close + timedelta(minutes=12))
    assert follow is not None and follow.final and follow.verdict == "held"
    # And it does stop: long after the last checkpoint nothing is measured.
    assert FU.update(note, None, history_for(points), now=close + timedelta(days=5)) is None
    assert not FU.market_open("VIX", datetime(2026, 10, 24, 15, 0, tzinfo=UTC))  # Saturday


def test_future_after_the_session_ends_for_the_weekend_is_not_available():
    start = datetime(2026, 10, 23, 19, 30, tzinfo=UTC)  # Friday 15:30 ET
    note = move_note("ES", 0.9, start=start, moves=[mv("ES", 0.9, start=start, minutes=60)])
    one_hour = start + timedelta(hours=2)  # Friday 17:30 ET: the week is over
    monday_close = datetime(2026, 10, 26, 20, 0, tzinfo=UTC)
    assert FU.planned_checkpoints(note) == [("1h", one_hour), ("close", monday_close)]
    follow = FU.update(note, None, history_for({}), now=one_hour)
    assert follow.checkpoints[0].moves == []
    assert "after 1 hour: not available" in follow.text
    assert not FU.market_open("ES", one_hour)


def test_checkpoints_across_a_holiday_and_an_early_close():
    # Wednesday before Thanksgiving, 16:30 ET move end: the hour mark is 18:30 ET and the
    # close is the half day after Thanksgiving (13:00 EST = 18:00 UTC).
    start = datetime(2026, 11, 25, 20, 30, tzinfo=UTC)
    note = move_note("NQ", 1.0, start=start - timedelta(minutes=60), minutes=60)
    assert FU.planned_checkpoints(note) == [
        ("1h", start + timedelta(hours=1)),
        ("close", datetime(2026, 11, 27, 18, 0, tzinfo=UTC))]
    # A winter-time move: 16:00 EST is 21:00 UTC.
    winter = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
    assert FU.planned_checkpoints(move_note("GOLD", 1.0, start=winter))[1][1] == datetime(
        2026, 11, 10, 21, 0, tzinfo=UTC)


def test_future_in_the_daily_break_is_not_available_and_overnight_is_measured():
    assert FU.market_open("GOLD", datetime(2026, 10, 20, 2, 0, tzinfo=UTC))  # overnight
    assert not FU.market_open("GOLD", datetime(2026, 10, 20, 21, 30, tzinfo=UTC))  # 17:30 ET break


# --- feed catch-up, quote gaps, giving up ------------------------------------------


def test_a_checkpoint_waits_for_the_delayed_feed():
    note = move_note("GOLD", 1.0)
    one_hour = START + timedelta(hours=2)
    points = quotes("GOLD", 2000.0, (one_hour, 2010.0))
    assert measured(note, points, now=one_hour + timedelta(minutes=10)) is None
    follow = measured(note, points, now=one_hour + FU.FEED_CATCH_UP)
    assert [c.label for c in follow.checkpoints] == ["1h"] and not follow.final
    assert "so far" in follow.text


def test_missing_quote_waits_then_is_recorded_as_not_available():
    note = move_note("GOLD", 1.0)
    one_hour = START + timedelta(hours=2)
    points = quotes("GOLD", 2000.0)  # nothing near the hour mark
    assert measured(note, points, now=one_hour + timedelta(hours=1)) is None
    follow = measured(note, points, now=one_hour + FU.FEED_CATCH_UP + FU.GIVE_UP_AFTER)
    assert follow.checkpoints[0].moves == []
    assert "after 1 hour: not available" in follow.text and follow.verdict is None


def test_an_old_quote_is_not_used():
    note = move_note("GOLD", 1.0)
    one_hour = START + timedelta(hours=2)
    points = quotes("GOLD", 2000.0, (one_hour - timedelta(minutes=20), 2010.0))
    follow = measured(note, points, now=one_hour + FU.FEED_CATCH_UP + FU.GIVE_UP_AFTER)
    assert follow.checkpoints[0].moves == []


def test_another_instruments_quote_is_never_substituted():
    note = move_note("GOLD", 1.0)
    one_hour = START + timedelta(hours=2)
    points = {**quotes("NQ", 30000.0, (one_hour, 30100.0)), "GOLD": []}
    follow = measured(note, points, now=one_hour + FU.FEED_CATCH_UP + FU.GIVE_UP_AFTER)
    assert follow.checkpoints[0].moves == []


# --- which notes, which instrument -------------------------------------------------


def test_event_note_is_measured_on_its_lead_not_nq():
    lead = mv("ES", 0.9, label="S&P 500 futures")
    note = move_note("ES", moves=[lead, mv("NQ", 1.4)], members=["NQ"])
    assert FU.subject_symbol(note) == "ES" and FU.first_reaction(note) == lead
    one_hour = START + timedelta(hours=2)
    points = {**quotes("ES", 5000.0, (one_hour, 5040.0), (CLOSE, 5075.0)),
              **quotes("NQ", 30000.0, (one_hour, 29000.0), (CLOSE, 29000.0))}
    follow = measured(note, points, now=CLOSE + timedelta(minutes=12))
    assert {m.symbol for c in follow.checkpoints for m in c.moves} == {"ES"}
    assert follow.verdict == "grew"
    assert "NQ" not in follow.text and "S&P 500 futures" in follow.text


def test_nq_move_note_measures_nq_and_only_nq():
    note = move_note("NQ", -1.0)
    one_hour = START + timedelta(hours=2)
    points = {**quotes("NQ", 30000.0, (one_hour, 29850.0), (CLOSE, 29700.0)),
              **quotes("ES", 5000.0, (one_hour, 5500.0))}
    follow = measured(note, points, now=CLOSE + timedelta(minutes=12))
    assert follow.verdict == "held"
    assert {m.symbol for c in follow.checkpoints for m in c.moves} == {"NQ"}


def test_ask_primer_failed_and_moveless_notes_get_nothing():
    assert FU.planned_checkpoints(move_note("NQ", 1.0, kind=NoteKind.ASK)) == []
    assert FU.planned_checkpoints(move_note("NQ", 1.0, kind=NoteKind.PREMARKET)) == []
    failed = move_note("NQ", 1.0).model_copy(update={"status": "failed"})
    assert FU.planned_checkpoints(failed) == []
    assert FU.planned_checkpoints(move_note("NQ", moves=[mv("ES", 1.0)])) == []
    assert FU.planned_checkpoints(move_note("NQ", moves=[])) == []


def test_old_move_notes_get_no_new_follow_up():
    note = move_note("GOLD", 1.0)
    assert measured(note, quotes("GOLD", 2000.0), now=START + timedelta(days=5)) is None


def test_release_follow_ups_are_unchanged():
    release = DeskNote(id="release:x", kind=NoteKind.RELEASE, title="CPI",
                       created_at=START, event_at=START, moves=[mv("NQ", -0.4, minutes=15)])
    one_hour = START + timedelta(hours=1)
    assert FU.planned_checkpoints(release) == [("1h", one_hour), ("close", CLOSE)]
    points = quotes("NQ", 30000.0, (one_hour, 29940.0), (CLOSE, 30180.0))
    follow = measured(release, points, now=CLOSE + timedelta(minutes=12))
    assert follow.text == ("First 15 min: NQ −0.40% · after 1 hour: −0.20% · "
                           "by the close: +0.60% — the first reaction reversed.")


# --- the service stores it beside the unchanged note -------------------------------


async def test_tick_stores_a_move_follow_up_for_the_lead_beside_the_unchanged_note():
    class Writer:
        ready = True
        detail = None
        model = "fake"

        def envelope(self, kind, result, *, instrument):
            return {}

        async def write(self, kind, facts, *, instrument):
            return WriteResult(None, "fake", 0.0)

    draft = NoteDraft(headline="Gold jumped", summary="Sum.", what_it_means="Means.",
                      market_reaction="React.", learn="Learn.", nq_tone="mixed")
    store = DeskStore(":memory:")
    original = move_note("GOLD", 1.0).model_copy(update={"draft": draft})
    store.save(original)
    one_hour = START + timedelta(hours=2)
    points = quotes("GOLD", 2000.0, (one_hour, 2020.0), (CLOSE, 2020.0))
    now = CLOSE + timedelta(minutes=12)
    service = DeskService(
        sources=DeskSources(market_state=lambda: SimpleNamespace(instruments={}),
                            history=history_for(points),
                            calendar_events=lambda s, e: [], news_events=lambda: []),
        store=store, writer=Writer(), clock=lambda: now,
    )
    assert await service.update_follow_ups(now) == 1
    stored = store.get(original.id)
    assert stored.follow_up.final and stored.follow_up.verdict == "held"
    assert stored.draft == original.draft and stored.moves == original.moves
    assert await service.update_follow_ups(now + timedelta(hours=1)) == 0
