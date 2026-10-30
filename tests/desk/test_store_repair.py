"""Phase 1 slice 3: the instrument column lands with its back-fill, and every
open repairs pre-market, move and ask rows that have none (a crash between the
two steps, or rows written by the rollback release)."""

import sqlite3
from datetime import timedelta

import pytest

from core.desk import store as S
from core.desk.models import NoteKind
from core.desk.store import DeskStore
from tests.desk.test_identity import MOVE_AT, falling
from tests.desk.test_note_facts import note
from tests.desk.test_service import make

ROWS = [("premarket:2026-10-20", NoteKind.PREMARKET), ("move:20261020T10", NoteKind.MOVE),
        ("ask:20261020T150000", NoteKind.ASK),
        ("release:2026-10-20T12:30:00+00:00", NoteKind.RELEASE)]


def columns(path) -> set[str]:
    db = sqlite3.connect(path)
    try:
        return {row[1] for row in db.execute("PRAGMA table_info(desk_notes)")}
    finally:
        db.close()


def instruments(path) -> dict[str, str | None]:
    db = sqlite3.connect(path)
    try:
        return dict(db.execute("SELECT id, instrument FROM desk_notes"))
    finally:
        db.close()


def pre_slice_3(path, rows=ROWS) -> None:
    """A database as the release before this one left it: no instrument column."""
    store = DeskStore(path)
    for nid, kind in rows:
        store.save(note(nid).model_copy(update={"kind": kind}))
    store.close()
    db = sqlite3.connect(path)
    db.execute("ALTER TABLE desk_notes DROP COLUMN instrument")
    db.commit()
    db.close()


def rollback_save(path, nid: str, kind: NoteKind, at=None) -> None:
    """Insert a note the way the slice-1 ``save`` does: it never sets ``instrument``."""
    n = note(nid).model_copy(update={"kind": kind, **({"created_at": at} if at else {})})
    db = sqlite3.connect(path)
    db.execute("INSERT INTO desk_notes (id, kind, status, created_at, payload) VALUES (?,?,?,?,?)",
               (n.id, n.kind.value, n.status, n.created_at.isoformat(),
                n.model_dump_json(exclude={"follow_up", "instrument"})))
    db.commit()
    db.close()


EXPECTED = {"premarket:2026-10-20": "NQ", "move:20261020T10": "NQ",
            "ask:20261020T150000": "NQ", "release:2026-10-20T12:30:00+00:00": None}


def test_a_crash_after_the_column_before_the_back_fill_is_repaired_on_reopen(tmp_path):
    path = tmp_path / "desk.db"
    pre_slice_3(path)
    # The old two-step upgrade: the ALTER committed on its own, then the process died.
    db = sqlite3.connect(path)
    db.execute("ALTER TABLE desk_notes ADD COLUMN instrument TEXT")
    db.commit()
    db.close()
    assert set(instruments(path).values()) == {None}
    store = DeskStore(path)
    assert instruments(path) == EXPECTED
    assert store.get("move:20261020T10").instrument == "NQ"
    assert store.get("release:2026-10-20T12:30:00+00:00").instrument is None


async def test_rows_from_the_rollback_release_are_repaired_and_counted(tmp_path):
    path = tmp_path / "desk.db"
    DeskStore(path).close()
    # Rolled back to slice 1: it writes NQ move and ask notes without the column.
    rollback_save(path, "move:20261020T10", NoteKind.MOVE, MOVE_AT - timedelta(hours=1))
    rollback_save(path, "ask:20261020T145500", NoteKind.ASK, MOVE_AT - timedelta(minutes=5))
    assert set(instruments(path).values()) == {None}
    # Rolled forward: the reopen repairs them.
    store = DeskStore(path)
    assert instruments(path) == {"move:20261020T10": "NQ", "ask:20261020T145500": "NQ"}
    since = (MOVE_AT - timedelta(hours=3)).isoformat()
    assert store.count_since("move", since, instrument="NQ") == 1
    assert store.latest_ok("ask", since, "NQ").id == "ask:20261020T145500"
    # So the 3-hour move cooldown holds and the fresh ask is reused, not paid for again.
    service, _, _ = make(MOVE_AT, series={"NQ": falling(30000.0)})
    service.store = store
    assert NoteKind.MOVE not in [job[1] for job in await service.due_jobs(MOVE_AT)]
    assert (await service.ask()).id == "ask:20261020T145500"
    assert service.writer.calls == []


def test_opening_twice_changes_nothing(tmp_path):
    path = tmp_path / "desk.db"
    pre_slice_3(path)
    DeskStore(path).close()
    db = sqlite3.connect(path)
    before = db.execute("SELECT * FROM desk_notes ORDER BY id").fetchall()
    db.close()
    DeskStore(path).close()
    db = sqlite3.connect(path)
    assert db.execute("SELECT * FROM desk_notes ORDER BY id").fetchall() == before
    db.close()
    assert instruments(path) == EXPECTED


def test_the_repair_reads_the_instrument_from_the_id(tmp_path):
    path = tmp_path / "desk.db"
    DeskStore(path).close()
    rollback_save(path, "move:ES:20261020T10", NoteKind.MOVE)
    rollback_save(path, "premarket:2026-10-21", NoteKind.PREMARKET)
    rollback_save(path, "auction:2026-10-20:912797XX1", NoteKind.AUCTION)
    DeskStore(path).close()
    assert instruments(path) == {"move:ES:20261020T10": "ES", "premarket:2026-10-21": "NQ",
                                 "auction:2026-10-20:912797XX1": None}


def test_a_failed_upgrade_leaves_no_column_and_a_later_open_recovers(tmp_path, monkeypatch):
    path = tmp_path / "desk.db"
    pre_slice_3(path)

    def broken(note_id):
        raise RuntimeError("disk trouble mid-upgrade")

    monkeypatch.setattr(S, "instrument_of", broken)
    with pytest.raises(RuntimeError, match="mid-upgrade"):
        DeskStore(path)
    assert "instrument" not in columns(path)
    monkeypatch.undo()
    DeskStore(path).close()
    assert "instrument" in columns(path) and instruments(path) == EXPECTED
