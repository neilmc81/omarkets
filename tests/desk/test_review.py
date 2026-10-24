"""Weekly review: which week, when it is due, and the deterministic facts."""

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from core.desk import review as R
from core.desk.models import Checkpoint, DeskNote, FollowUp, Move, NoteDraft, NoteKind
from tests.desk.test_service import FakeWriter, make


def utc(*args):
    return datetime(*args, tzinfo=UTC)


@pytest.mark.parametrize(
    ("now", "first", "last", "previous_close", "last_close"),
    [
        # Ordinary week (EDT): Friday 16:00 ET is 20:00 UTC.
        (utc(2026, 10, 23, 21), date(2026, 10, 19), date(2026, 10, 23),
         utc(2026, 10, 16, 20), utc(2026, 10, 23, 20)),
        # Daylight saving ends Nov 1: last week's close was EDT, this one is EST.
        (utc(2026, 11, 6, 22), date(2026, 11, 2), date(2026, 11, 6),
         utc(2026, 10, 30, 20), utc(2026, 11, 6, 21)),
        # Thanksgiving: Thursday closed, Friday closes early at 13:00 EST.
        (utc(2026, 11, 28, 1), date(2026, 11, 23), date(2026, 11, 27),
         utc(2026, 11, 20, 21), utc(2026, 11, 27, 18)),
        # Christmas Friday is a holiday: the week ends with Thursday's early close.
        (utc(2026, 12, 24, 19), date(2026, 12, 21), date(2026, 12, 24),
         utc(2026, 12, 18, 21), utc(2026, 12, 24, 18)),
        # Good Friday 2027 (EDT): the review follows Thursday's close.
        (utc(2027, 3, 26, 12), date(2027, 3, 22), date(2027, 3, 25),
         utc(2027, 3, 19, 20), utc(2027, 3, 25, 20)),
        # Labor Day Monday: the week starts Tuesday; changes start at Friday's close.
        (utc(2026, 9, 11, 21), date(2026, 9, 8), date(2026, 9, 11),
         utc(2026, 9, 4, 20), utc(2026, 9, 11, 20)),
    ],
)
def test_review_week(now, first, last, previous_close, last_close):
    week = R.review_week(now)
    assert (week.trading_days[0], week.trading_days[-1]) == (first, last)
    assert (week.previous_close, week.last_close) == (previous_close, last_close)
    assert week.note_id == f"review:{week.monday.isoformat()}"


def test_review_waits_for_the_delayed_closing_quotes_and_catches_up_on_the_weekend():
    close = utc(2026, 10, 23, 20)
    assert R.review_due(close + timedelta(minutes=14)) is None
    assert R.review_due(close + timedelta(minutes=15)).note_id == "review:2026-10-19"
    assert R.review_due(utc(2026, 10, 24, 15)).note_id == "review:2026-10-19"  # Saturday
    assert R.review_due(close + timedelta(days=2)) is None  # Sunday afternoon: too late
    assert R.review_due(utc(2026, 10, 22, 21)) is None  # Thursday: not the last day


def _note(title, change, *, at, close_change=None, kind=NoteKind.RELEASE):
    move = Move(symbol="NQ", label="NQ", start_value=100, end_value=100 + change, start_at=at,
                end_at=at + timedelta(minutes=15), change=change, change_unit="%",
                text=f"{change:+.2f}%")
    follow = None
    if close_change is not None:
        close = move.model_copy(update={"change": close_change, "text": f"{close_change:+.2f}%"})
        follow = FollowUp(checkpoints=[Checkpoint(label="close", at=at + timedelta(hours=7),
                                                  moves=[close])],
                          verdict="held", final=True, text=f"{title} line")
    draft = NoteDraft(headline=f"{title} headline", summary="Sum.", what_it_means="Means.",
                      market_reaction="React.", rates_read="Yields.", learn="Learn.",
                      nq_tone="neutral")
    return DeskNote(id=f"{kind}:{title}", kind=kind, title=title, created_at=at, event_at=at,
                    moves=[move], draft=draft, follow_up=follow)


def test_events_are_ranked_by_nq_move_and_limited_to_the_week():
    week = R.review_week(utc(2026, 10, 23, 21))
    notes = [
        _note("CPI", -0.3, at=utc(2026, 10, 20, 12, 30), close_change=-1.2),
        _note("Claims", 0.5, at=utc(2026, 10, 22, 12, 30)),
        _note("Old", 2.0, at=utc(2026, 10, 15, 12, 30)),  # last week
        _note("Premarket", 3.0, at=utc(2026, 10, 21, 13), kind=NoteKind.PREMARKET),
    ]
    events = R.ranked_events(notes, week)
    assert [e["event"] for e in events] == ["CPI", "Claims"]
    assert events[0]["nq_move"] == "-1.20% by the close"
    assert events[0]["did_it_hold"] == "CPI line"
    assert events[1]["nq_move"] == "+0.50% in the first 15 min"


def test_week_moves_and_daily_changes_are_close_to_close():
    week = R.review_week(utc(2026, 10, 23, 21))
    closes = [week.previous_close] + [R.cash_close(d) for d in week.trading_days]
    values = [100.0, 101.0, 100.0, 102.0, 102.0, 103.0]
    nq = [SimpleNamespace(timestamp=c - timedelta(seconds=30), value=v)
          for c, v in zip(closes, values, strict=True)]

    def history(symbol, *, start=None, end=None, limit=500):
        return [o for o in nq if symbol == "NQ" and start <= o.timestamp <= end]

    moves = R.week_moves(history, week)
    assert [(m.symbol, m.text) for m in moves] == [("NQ", "+3.00%")]
    daily = R.daily_nq(history, week)
    assert [d["day"] for d in daily] == ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    assert daily[0]["nq_change"] == "+1.00%" and daily[3]["nq_change"] == "+0.00%"


async def test_review_is_written_once_after_the_friday_close():
    writer = FakeWriter()
    service, clock, _ = make(utc(2026, 10, 23, 20, 16), writer=writer)
    produced = await service.tick()
    reviews = [n for n in produced if n.kind is NoteKind.REVIEW]
    assert [n.id for n in reviews] == ["review:2026-10-19"]
    assert reviews[0].title == "Week in review · Oct 19–Oct 23"
    facts = next(f for kind, f in writer.calls if kind is NoteKind.REVIEW)
    assert {"week", "week_moves", "nq_each_day", "events_by_nq_move", "next_week"} <= set(facts)
    clock["now"] = utc(2026, 10, 24, 10)
    assert not [n for n in await service.tick() if n.kind is NoteKind.REVIEW]
