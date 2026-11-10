"""The reader's mark on a note: its own table, set / change / clear, payload untouched."""

import sqlite3
from datetime import UTC, datetime, timedelta

from core.desk.models import DeskNote, NoteKind
from core.desk.store import DeskStore
from tests.desk.test_service import draft

AT = datetime(2026, 10, 20, 14, 0, tzinfo=UTC)


def note(note_id="move:GOLD:x", status="ok"):
    return DeskNote(id=note_id, kind=NoteKind.MOVE, title="Gold", created_at=AT, status=status,
                    draft=draft("Gold jumped") if status == "ok" else None)


def payload(store, note_id):
    return store._db.execute("SELECT payload FROM desk_notes WHERE id=?", (note_id,)).fetchone()[0]


def test_set_change_and_clear():
    store = DeskStore(":memory:")
    store.save(note())
    assert store.get("move:GOLD:x").rating is None
    state = store.set_rating("move:GOLD:x", "useful", AT)
    assert (state.rating, state.rated_at) == ("useful", AT)
    assert store.get("move:GOLD:x").rating == "useful"
    later = AT + timedelta(minutes=5)
    assert store.set_rating("move:GOLD:x", "not_useful", later).rating == "not_useful"
    stored = store.get("move:GOLD:x")
    assert (stored.rating, stored.rated_at) == ("not_useful", later)
    assert store.clear_rating("move:GOLD:x") is True
    assert store.get("move:GOLD:x").rating is None and store.get("move:GOLD:x").rated_at is None
    assert store.clear_rating("move:GOLD:x") is True  # clearing an unrated note is fine


def test_the_stored_note_payload_is_never_modified():
    store = DeskStore(":memory:")
    store.save(note())
    before = payload(store, "move:GOLD:x")
    store.set_rating("move:GOLD:x", "useful", AT)
    store.set_rating("move:GOLD:x", "not_useful", AT)
    assert payload(store, "move:GOLD:x") == before
    assert '"rating"' not in payload(store, "move:GOLD:x")
    store.clear_rating("move:GOLD:x")
    assert payload(store, "move:GOLD:x") == before


def test_unknown_and_failed_notes_cannot_be_rated():
    store = DeskStore(":memory:")
    store.save(note("move:NQ:failed", status="failed"))
    assert store.set_rating("nope", "useful", AT) is None
    assert store.set_rating("move:NQ:failed", "useful", AT) is None
    assert store.clear_rating("nope") is False
    assert store._db.execute("SELECT COUNT(*) FROM desk_ratings").fetchone()[0] == 0


def test_the_table_is_created_idempotently_and_survives_reopening(tmp_path):
    path = tmp_path / "desk.sqlite3"
    store = DeskStore(path)
    store.save(note())
    store.set_rating("move:GOLD:x", "useful", AT)
    store.close()
    reopened = DeskStore(path)  # CREATE TABLE IF NOT EXISTS again, nothing lost
    assert reopened.get("move:GOLD:x").rating == "useful"
    reopened.close()
    db = sqlite3.connect(path)
    assert db.execute("PRAGMA table_info(desk_ratings)").fetchall()
    db.close()


def test_the_table_rejects_a_value_that_is_not_a_mark():
    store = DeskStore(":memory:")
    store.save(note())
    try:
        store._db.execute("INSERT INTO desk_ratings VALUES (?,?,?)", ("move:GOLD:x", "love", "x"))
    except sqlite3.IntegrityError:
        return
    raise AssertionError("an unknown rating was accepted")


def test_every_read_path_carries_the_mark():
    store = DeskStore(":memory:")
    store.save(note())
    store.set_rating("move:GOLD:x", "useful", AT)
    assert store.recent()[0].rating == "useful"
    hour = timedelta(hours=1)
    assert store.readable(AT - hour, AT + hour, 10)[0].rating == "useful"
    assert store.latest_ok("move", (AT - timedelta(hours=1)).isoformat()).rating == "useful"
    assert store.between(AT, AT + timedelta(seconds=1))[0].rating == "useful"
