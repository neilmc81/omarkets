"""Each note says which instrument its tone is about, so the UI never labels an
ES note's tone as NQ's. Derived on every read, never stored."""

import json

from core.desk.models import DeskNote, NoteKind
from core.desk.store import DeskStore
from tests.desk.test_note_facts import note


def test_an_instrument_note_tone_is_about_its_own_instrument():
    es = note("move:ES:20261020T10").model_copy(update={"kind": NoteKind.MOVE, "instrument": "ES"})
    assert es.tone_for == "ES"
    assert json.loads(es.model_dump_json())["tone_for"] == "ES"


def test_a_desk_wide_event_note_tone_is_about_nq():
    assert note("release:x").tone_for == "NQ"


def test_tone_for_is_never_stored_and_a_full_dump_still_loads(tmp_path):
    store = DeskStore(tmp_path / "desk.sqlite3")
    es = note("move:ES:20261020T10").model_copy(update={"kind": NoteKind.MOVE, "instrument": "ES"})
    store.save(es)
    payload = store._db.execute("SELECT payload FROM desk_notes").fetchone()[0]
    assert "tone_for" not in json.loads(payload)
    assert store.get(es.id).tone_for == "ES"
    assert DeskNote.model_validate_json(es.model_dump_json()).tone_for == "ES"
