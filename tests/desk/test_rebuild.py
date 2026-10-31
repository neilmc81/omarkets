"""Phase 1 slice 4 gate: v8 comparison cases with full facts, rebuilt offline and frozen."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from core.desk.evaluation import cases as C
from core.desk.evaluation import rebuild as RB
from core.desk.models import DeskNote, NoteKind
from core.desk.prompts import render
from core.desk.store import DeskStore
from tests.desk.test_service import draft, make

V8 = Path(__file__).parents[1] / "fixtures" / "eval" / "v8-rebuilt"
AT = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)


def test_frozen_v8_cases_are_complete_and_render():
    cases = C.load(V8)
    assert sorted(c.kind for c in cases.cases) == [
        "ask", "move", "move", "premarket", "premarket", "release", "review"]
    for case in cases.cases:
        assert case.facts_complete and case.baseline["prompt_version"] == "desk:v8"
        assert case.envelope["reconstructed"]["limits"] == list(RB.LIMITS)
        prompt = render(NoteKind(case.kind), C.case_instrument(case), case.facts)
        assert prompt.version == "desk:v8" and prompt.facts_text
        for key, value in case.facts.items():
            if "headlines" in key:
                assert len(value) <= 12
        if case.kind != "review":
            assert {"nq_vs_es", "megacap_participation"} <= case.facts.keys()
            assert all("status" in row for row in case.facts["markets_today"])
        if case.kind == "move":
            assert "events" in case.facts["events_in_move_window"]


def test_history_seam_hides_quotes_received_after_the_moment():
    minutes = timedelta(minutes=1)
    rows = [SimpleNamespace(timestamp=AT - 12 * minutes, received_at=AT - 2 * minutes),
            SimpleNamespace(timestamp=AT - 5 * minutes, received_at=AT + 5 * minutes)]
    seen = {}

    def history(symbol, *, start=None, end=None, limit=500):
        seen["end"] = end
        return rows

    read = RB._before(history, AT)
    assert read("NQ", end=AT + timedelta(hours=1), limit=1) == rows[:1]
    assert seen["end"] == AT


def test_cut_store_drops_later_notes_and_alerts(tmp_path):
    db = tmp_path / "desk.sqlite3"
    store = DeskStore(db)
    hour = timedelta(hours=1)
    for note_id, at in (("ask:old", AT - hour), ("ask:later", AT + hour)):
        store.save(DeskNote(id=note_id, kind=NoteKind.ASK, title="Now", created_at=at,
                            draft=draft()))
    store.close()
    (tmp_path / "work").mkdir()
    cut = RB._cut_store(db, AT, tmp_path / "work")
    try:
        assert [n.id for n in cut.recent()] == ["ask:old"]
    finally:
        cut.close()
    assert DeskStore(db).get("ask:later") is not None  # the source is untouched


def test_rebuild_an_ask_note_records_its_limits(tmp_path):
    db = tmp_path / "desk.sqlite3"
    store = DeskStore(db)
    store.save(DeskNote(id="ask:20261020T150000", kind=NoteKind.ASK, title="Market right now",
                        created_at=AT, draft=draft(), model="m · desk:v8"))
    store.close()
    service, _, _ = make(AT)
    [case] = RB.rebuild(["ask:20261020T150000"], desk_db=db,
                        sources_at=lambda moment: service.sources)
    assert case.facts_complete and case.envelope["instrument"] == "NQ"
    assert case.envelope["reconstructed"]["built_at"] == AT.isoformat()
    assert case.baseline == {"prompt_version": "desk:v8", "model": "m",
                             "draft": draft().model_dump(mode="json")}
    assert {"markets_today", "nq_vs_es", "recent_headlines"} <= case.facts.keys()
