"""The join window: an instrument that passes its trigger within 60 minutes of a
written move note joins it silently; NQ coming in later escalates to one NQ-led event.

The two replays use the real move-end times of Nov 4 and Nov 5 2026."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.api import desk as desk_api
from core.desk import event as EVT
from core.desk import prompts as P
from core.desk.models import DeskNote, EventJoin, Move, NoteKind
from core.desk.service import et
from core.desk.store import DeskStore
from core.desk.writer import WriteResult
from tests.desk.test_event import mover
from tests.desk.test_service import FakeWriter, draft, make

ALL = ("NQ", "ES", "YM", "GOLD", "WTI", "DXY", "US2Y", "US10Y", "VIX")
BASE = {"NQ": 30000.0, "ES": 6000.0, "YM": 44000.0, "GOLD": 4000.0, "US2Y": 4.0,
        "US10Y": 4.3, "VIX": 20.0}


def utc(day, hour, minute):
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC)


class KindWriter(FakeWriter):
    """Headlines name the kind, so move notifications can be told from the rest."""

    def __init__(self, version):
        super().__init__()
        self.prompt_version = version

    async def write(self, kind, facts, *, instrument):
        self.calls.append((kind, facts))
        if self.results and kind is NoteKind.MOVE:
            return self.results.pop(0)
        return WriteResult(draft(f"{kind.value} note"), "fake", 0.01)


def series_for(symbol, ramps, start, end):
    """One quote a minute from ``start`` to ``end``. Each ramp drops the level over
    the two minutes after its start: 1% (a stock index or gold), 8bp (a yield) or 6%
    (VIX), more than the default trigger, so it crosses two minutes after its start."""
    out = []
    t = start
    while t <= end:
        progress = sum(min(max((t - r) / timedelta(minutes=2), 0.0), 1.0) for r in ramps)
        if symbol.startswith("US"):
            value = BASE[symbol] - 0.08 * progress
        else:
            value = BASE[symbol] * (1 - (0.06 if symbol == "VIX" else 0.01) * progress)
        out.append(SimpleNamespace(timestamp=t, value=value))
        t += timedelta(minutes=1)
    return out


async def replay(version, first, last, triggers, extra_ticks=()):
    """Tick every minute from ``first`` to ``last``. ``triggers`` is symbol -> the
    minutes at which each crosses its trigger."""
    series = {s: series_for(s, [t - timedelta(minutes=2) for t in ts],
                            first - timedelta(hours=3), last)
              for s, ts in triggers.items()}
    service, clock, sent = make(first, series=series, writer=KindWriter(version),
                                instruments=ALL)
    log = []
    now = first
    while now <= last:
        clock["now"] = now
        log += [(now, n) for n in await service.tick() if n.kind is NoteKind.MOVE]
        now += timedelta(minutes=1)
    moves = [(at, n) for at, n in log]
    calls = [f for k, f in service.writer.calls if k is NoteKind.MOVE]
    pushes = [s for s, _ in sent if s == "move note"]
    return service, moves, calls, pushes


NOV5 = {"VIX": [utc(5, 14, 25)], "US2Y": [utc(5, 14, 42)], "YM": [utc(5, 14, 44)],
        "US10Y": [utc(5, 14, 46)], "NQ": [utc(5, 14, 53)], "ES": [utc(5, 14, 53)]}
NOV4 = {"US10Y": [utc(4, 4, 49)], "US2Y": [utc(4, 4, 49)], "NQ": [utc(4, 5, 5)],
        "ES": [utc(4, 5, 12)], "GOLD": [utc(4, 5, 34)]}


async def test_nov_5_is_two_notes_and_the_joins_are_silent():
    service, moves, calls, pushes = await replay(P.V11, utc(5, 14, 20), utc(5, 15, 10), NOV5)
    assert [n.id for _, n in moves] == ["move:VIX:20261105T09", "move:20261105T09"]
    assert len(calls) == 2 and len(pushes) == 2  # one model call and one push per note
    vix_at, vix = moves[0]
    assert vix.event_members == [] and vix_at == utc(5, 14, 25)
    # US2Y, YM and US10Y joined the VIX note, each at its own minute, with no call.
    joined = service.store.get(vix.id).event_joins
    assert [(j.instrument, j.joined_at) for j in joined] == [
        ("US2Y", utc(5, 14, 42)), ("YM", utc(5, 14, 44)), ("US10Y", utc(5, 14, 46))]
    assert all(j.move.symbol == j.instrument for j in joined)
    nq_at, nq = moves[1]
    assert nq_at == utc(5, 14, 53) and nq.instrument == "NQ"
    assert set(nq.event_members) == {"ES", "VIX", "US2Y", "YM", "US10Y"}
    assert nq.title.startswith("NQ, ") and nq.title.endswith("moved in the same event")
    rows = {r["market"]: r for r in calls[1]["the_event"]["markets_that_moved"]}
    assert len(rows) == 6 and "triggered_earlier_at" not in rows["Nasdaq 100 futures"]
    assert "triggered_earlier_at" not in rows["S&P 500 futures"]  # ES: same pass
    assert rows["VIX fear gauge"]["triggered_earlier_at"] == et(utc(5, 14, 25))
    assert rows["2-year yield"]["triggered_earlier_at"] == et(utc(5, 14, 42))
    assert rows["Dow futures"]["triggered_earlier_at"] == et(utc(5, 14, 44))
    assert rows["10-year yield"]["triggered_earlier_at"] == et(utc(5, 14, 46))
    # Re-measured now, with a real times-trigger, not the move they triggered on.
    assert rows["VIX fear gauge"]["times_its_trigger"] == "1.2"
    # The first note was not rewritten by the escalation.
    assert service.store.get(vix.id).event_members == []


async def test_nov_4_is_a_rates_event_then_an_nq_event_that_es_and_gold_join():
    service, moves, calls, pushes = await replay(P.V11, utc(4, 4, 40), utc(4, 5, 50), NOV4)
    assert len(moves) == 2 and len(calls) == 2 and len(pushes) == 2
    rates, nq = moves[0][1], moves[1][1]
    assert calls[0]["the_event"]["scope"] == "rates"
    assert rates.event_members == ["US10Y"] and rates.title.endswith(
        "yields moved together in the last hour")
    assert nq.instrument == "NQ" and nq.event_members == ["US2Y", "US10Y"]
    rows = calls[1]["the_event"]["markets_that_moved"]
    assert [bool(r.get("triggered_earlier_at")) for r in rows] == [False, True, True]
    assert calls[1]["the_event"]["scope"] == "cross_market"
    joined = service.store.get(nq.id).event_joins
    assert [(j.instrument, j.joined_at) for j in joined] == [
        ("ES", utc(4, 5, 12)), ("GOLD", utc(4, 5, 34))]
    assert [i.symbol for i in service.store.get(nq.id).event_instruments] == [
        "NQ", "US2Y", "US10Y", "ES", "GOLD"]
    assert service.store.get(rates.id).event_joins == []  # newest note takes the joiners


@pytest.mark.parametrize(("triggers", "first", "last", "expected"),
                         [(NOV5, utc(5, 14, 20), utc(5, 15, 10), 6),
                          (NOV4, utc(4, 4, 40), utc(4, 5, 50), 5)])
@pytest.mark.parametrize("version", [P.V9, P.V10])
async def test_v10_and_earlier_still_write_one_note_per_instrument(
        version, triggers, first, last, expected):
    service, moves, calls, pushes = await replay(version, first, last, triggers)
    assert len(moves) == len(calls) == len(pushes) == expected
    assert all(n.event_members == [] and n.event_joins == [] for _, n in moves)
    assert all("the_event" not in f for f in calls)
    assert service.store._db.execute("SELECT COUNT(*) FROM desk_event_joins").fetchone()[0] == 0


async def test_a_join_counts_against_the_joiners_own_three_hours():
    triggers = {**NOV5, "US2Y": [utc(5, 14, 42), utc(5, 15, 55), utc(5, 17, 55)]}
    service, moves, calls, _ = await replay(P.V11, utc(5, 14, 20), utc(5, 17, 59), triggers)
    # US2Y joined at 14:42, was then a member of the NQ note from 14:53, so its 3 hours
    # run to 17:53: nothing at 15:55, its own single note at 17:55.
    assert [n.id for _, n in moves][:2] == ["move:VIX:20261105T09", "move:20261105T09"]
    assert [(at, n.id) for at, n in moves[2:]] == [(utc(5, 17, 55), "move:US2Y:20261105T12")]
    assert moves[2][1].event_members == []
    # And in the store: the join counts for the joiner, from the join time.
    store = DeskStore(":memory:")
    store.save(written())
    store.add_join(join("US2Y", at=utc(5, 14, 42)))
    assert store.count_since("move", utc(5, 14, 40).isoformat(), instrument="US2Y",
                             with_members=True) == 1
    assert store.count_since("move", utc(5, 14, 43).isoformat(), instrument="US2Y",
                             with_members=True) == 0
    assert store.count_since("move", utc(5, 14, 40).isoformat(), instrument="US2Y") == 0


async def test_a_note_older_than_the_window_attracts_no_joiners():
    # GOLD crosses at 15:55, 62 minutes after the NQ note: a note of its own.
    triggers = {**NOV5, "GOLD": [utc(5, 15, 55)]}
    service, moves, _, _ = await replay(P.V11, utc(5, 14, 20), utc(5, 15, 57), triggers)
    assert [n.id for _, n in moves][2:] == ["move:GOLD:20261105T10"]
    assert service.store.get("move:20261105T09").event_joins == []
    # At 15:50, 57 minutes after, it joins instead.
    triggers = {**NOV5, "GOLD": [utc(5, 15, 50)]}
    service, moves, _, _ = await replay(P.V11, utc(5, 14, 20), utc(5, 15, 52), triggers)
    assert len(moves) == 2
    assert [j.instrument for j in service.store.get("move:20261105T09").event_joins] == ["GOLD"]


async def test_a_failed_note_is_never_joined_and_blocks_nothing():
    series = {"ES": series_for("ES", [utc(5, 14, 23)], utc(5, 11, 0), utc(5, 15, 0)),
              "GOLD": series_for("GOLD", [utc(5, 14, 33)], utc(5, 11, 0), utc(5, 15, 0))}
    writer = KindWriter(P.V11)
    writer.results = [WriteResult(None, "m", error="model output was not valid")]
    service, clock, _ = make(utc(5, 14, 25), series=series, writer=writer, instruments=ALL)
    clock["now"] = utc(5, 14, 25)
    assert [n.status for n in await service.tick() if n.kind is NoteKind.MOVE] == ["failed"]
    # GOLD crosses 10 minutes later, inside the backoff and the join window: no written
    # note exists, so nothing is joined; it writes the way it always would.
    clock["now"] = utc(5, 14, 35)
    produced = [n for n in await service.tick() if n.kind is NoteKind.MOVE]
    assert {n.status for n in produced} == {"ok"}
    assert service.store._db.execute("SELECT COUNT(*) FROM desk_event_joins").fetchone()[0] == 0


# ------------------------------------------------------------------ routing, pure


def test_routing_without_a_note_is_the_same_pass_grouping():
    movers = [mover("ES", -1, 0.8, 1), mover("GOLD", 1, 0.8, 3)]
    routed = EVT.route(movers, None)
    assert routed.grouped == tuple(movers) and not routed.joins and not routed.escalate
    assert EVT.route([], {"VIX"}) == EVT.Routing()


def test_routing_with_a_note_joins_everyone_unless_nq_is_new_to_it():
    movers = [mover("ES", -1, 0.8, 1), mover("GOLD", 1, 0.8, 3)]
    assert EVT.route(movers, {"VIX"}).joins == tuple(movers)
    with_nq = [mover("NQ", -1, 0.8, 0), *movers]
    escalated = EVT.route(with_nq, {"VIX", "US2Y"})
    assert escalated.escalate and escalated.grouped == tuple(with_nq) and not escalated.joins
    assert EVT.route(with_nq, {"NQ", "VIX"}).joins == tuple(with_nq)


# ------------------------------------------------------------------ store and API


def written(note_id="move:VIX:20261105T09", at=None, **kwargs):
    return DeskNote(id=note_id, kind=NoteKind.MOVE, title="t",
                    created_at=at or utc(5, 14, 28), instrument="VIX", draft=draft("Headline"),
                    **kwargs)


def join(instrument="US2Y", note_id="move:VIX:20261105T09", at=None):
    at = at or utc(5, 14, 42)
    move = Move(symbol=instrument, label=instrument, start_value=1.0, end_value=0.9,
                start_at=at - timedelta(minutes=60), end_at=at, change=-1.2, change_unit="%",
                text="-1.20%")
    return EventJoin(note_id=note_id, instrument=instrument, joined_at=at, move=move)


def test_the_join_table_is_created_idempotently_and_rows_are_unique(tmp_path):
    path = tmp_path / "desk.sqlite3"
    first = DeskStore(path)
    first.save(written())
    assert first.add_join(join()) is True
    assert first.add_join(join(at=utc(5, 14, 50))) is False  # one row per note and instrument
    first.close()
    again = DeskStore(path)  # reopening re-runs the schema step
    assert again.add_join(join("YM", at=utc(5, 14, 44))) is True
    assert [j.instrument for j in again.get("move:VIX:20261105T09").event_joins] == [
        "US2Y", "YM"]
    assert again.get("move:VIX:20261105T09").event_joins[0].joined_at == utc(5, 14, 42)


def test_only_a_written_note_can_be_joined():
    store = DeskStore(":memory:")
    assert store.add_join(join()) is False  # no such note
    store.save(DeskNote(id="move:VIX:20261105T09", kind=NoteKind.MOVE, title="t",
                        created_at=utc(5, 14, 28), status="failed"))
    assert store.add_join(join()) is False


def test_a_joined_notes_payload_is_unchanged_byte_for_byte():
    store = DeskStore(":memory:")
    store.save(written())
    before = store._db.execute("SELECT * FROM desk_notes").fetchall()
    store.add_join(join())
    store.add_join(join("YM"))
    assert store._db.execute("SELECT * FROM desk_notes").fetchall() == before
    assert "event_joins" not in json.loads(before[0][5])


def test_filtering_by_a_joined_instrument_returns_the_note_and_labels_it():
    service, _, _ = make(utc(5, 15, 0), writer=KindWriter(P.V11))
    service.store.save(written())
    service.store.add_join(join("US2Y"))
    client = TestClient(_app(service))
    window = {"from": utc(5, 0, 0).isoformat(), "to": utc(5, 16, 0).isoformat()}
    for symbol, hit in (("US2Y", True), ("VIX", True), ("ES", False)):
        ids = [n["id"] for n in client.get(
            "/api/v1/desk/notes", params={**window, "instrument": symbol}).json()["notes"]]
        assert ids == (["move:VIX:20261105T09"] if hit else [])
    [row] = client.get("/api/v1/desk/notes", params=window).json()["notes"]
    assert row["event_instruments"] == [
        {"symbol": "VIX", "label": "VIX fear gauge", "joined": False, "move_text": None,
         "joined_at": None},
        {"symbol": "US2Y", "label": "2-year yield", "joined": True, "move_text": "-1.20%",
         "joined_at": "2026-11-05T14:42:00Z"}]
    full = client.get("/api/v1/desk/notes/move:VIX:20261105T09").json()
    assert full["event_joins"][0]["instrument"] == "US2Y"


def test_a_note_with_no_joins_and_no_members_sends_neither_field():
    service, _, _ = make(utc(5, 15, 0), writer=KindWriter(P.V11))
    service.store.save(written())
    body = TestClient(_app(service)).get("/api/v1/desk/notes/move:VIX:20261105T09").json()
    assert "event_joins" not in body and "event_instruments" not in body


def _app(service):
    app = FastAPI()
    app.include_router(desk_api.router)
    app.state.desk = service
    return app
