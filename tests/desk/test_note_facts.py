"""Phase 1 slice 1: captured facts, the compatibility reader, and backup/restore."""

import json
from datetime import UTC, datetime, timedelta

from core.desk.models import DeskNote, FollowUp, NoteDraft, NoteKind
from core.desk.store import DeskStore
from core.desk.writer import DeskWriter, WriteResult
from tests.desk.test_service import RELEASE, cpi_rows, draft, make

AT = datetime(2026, 10, 20, 13, 0, tzinfo=UTC)
V8 = {"headline": "Hot CPI", "summary": "Sum.", "what_it_means": "Means.",
      "market_reaction": "React.", "rates_read": "Yields.", "watch_next": [],
      "learn": "Learn.", "nq_tone": "negative"}


def note(note_id="release:x", status="ok", **draft_fields):
    body = {**V8, **draft_fields}
    return DeskNote(id=note_id, kind=NoteKind.RELEASE, title="CPI", created_at=AT,
                    draft=NoteDraft.model_validate(body) if status == "ok" else None,
                    status=status)


def test_reader_accepts_v8_and_v9_drafts_and_stores_the_v8_name():
    v9 = {k: v for k, v in V8.items() if k not in {"nq_tone", "rates_read"}} | {"tone": "mixed"}
    parsed = NoteDraft.model_validate(v9)
    assert parsed.nq_tone == "mixed" and parsed.rates_read is None
    stored = json.loads(parsed.model_dump_json())
    assert stored["nq_tone"] == "mixed" and "tone" not in stored
    assert NoteDraft.model_validate(V8).model_dump() == {**V8}


def test_unreadable_payload_is_skipped_not_blanking_the_list():
    store = DeskStore(":memory:")
    store.save(note("release:good"))
    store._db.execute(
        "INSERT INTO desk_notes (id, kind, status, created_at, payload) VALUES (?,?,?,?,?)",
        ("release:bad", "release", "ok", AT.isoformat(), '{"id": "release:bad", "kind": "nope"}'))
    assert [n.id for n in store.recent()] == ["release:good"]
    window = store.readable(AT - timedelta(1), AT + timedelta(1), 10)
    assert [n.id for n in window] == ["release:good"]
    assert len(store.learn_entries()) == 1


def test_facts_are_kept_for_the_written_note_only():
    store = DeskStore(":memory:")
    store.save(note(status="failed"), {"try": 1}, {"model": "a"})
    assert store.facts("release:x") == ({"try": 1}, {"model": "a"})
    store.save(note(), {"try": 2}, {"model": "b"})  # replaces the failed attempt
    assert store.facts("release:x") == ({"try": 2}, {"model": "b"})
    store.save(note(), {"try": 3}, {"model": "c"})  # an ok note is never overwritten
    assert store.facts("release:x") == ({"try": 2}, {"model": "b"})
    assert store.facts("missing") is None


async def test_service_captures_the_exact_facts_and_envelope():
    service, clock, _ = make(RELEASE + timedelta(minutes=27), rows=cpi_rows())
    calls = service.writer.calls
    await service.tick()
    facts, envelope = service.store.facts("release:20261020T1230:CPI")
    sent = next(f for k, f in calls if k is NoteKind.RELEASE)
    assert facts == json.loads(json.dumps(sent, default=str))
    assert envelope == {"model": "fake"}


def test_writer_envelope_names_prompt_template_and_routing():
    writer = DeskWriter(api_key="k", model="m1", fallback_model="m2",
                        model_params={"m1": {"provider": {"order": ["x"]}}})
    env = writer.envelope(NoteKind.RELEASE, WriteResult(draft(), "m2"))
    assert env["prompt_version"].startswith("desk:") and len(env["system_sha256"]) == 64
    assert len(env["task_sha256"]) == 64 and env["temperature"] == 0.3
    assert env["primary_model"] == "m1" and env["model"] == "m2"
    assert env["fallback_model"] == "m2" and env["model_params"] == {}
    env = writer.envelope(NoteKind.RELEASE, WriteResult(draft(), "m1"))
    assert env["model_params"] == {"provider": {"order": ["x"]}}


def test_v8_writer_still_rejects_a_draft_without_rates_read():
    from core.desk.writer import check_draft
    body = {k: v for k, v in V8.items() if k != "rates_read"}
    assert check_draft(NoteDraft.model_validate(body)) == "model output was not valid"
    assert check_draft(NoteDraft.model_validate(V8)) is None


def test_malformed_json_row_does_not_break_list_queries():
    store = DeskStore(":memory:")
    store.save(note("release:good"))
    store._db.execute(
        "INSERT INTO desk_notes (id, kind, status, created_at, payload) VALUES (?,?,?,?,?)",
        ("release:broken", "release", "ok", AT.isoformat(), "{bad"))
    window = store.readable(AT - timedelta(1), AT + timedelta(1), 10)
    assert [n.id for n in window] == ["release:good"]
    assert store.release_note_ids() == ["release:good"]
    assert [n.id for n in store.recent()] == ["release:good"]


def test_backup_restores_attempts_alerts_once_keys_and_follow_ups(tmp_path):
    live = DeskStore(tmp_path / "desk.sqlite3")
    live.save(note("release:failed", status="failed"))
    live.save(note("release:failed", status="failed"))
    assert live.claim_failure_alert("release:failed")
    live.save(note("release:ok"), {"a": 1}, {"m": "x"})
    live.save_follow_up("release:ok", FollowUp(text="Held.", final=True, verdict="held"))
    assert live.claim_once("plan:2026-10-20", AT)
    live.backup(tmp_path / "copy.sqlite3")
    live.close()

    restored = DeskStore(tmp_path / "copy.sqlite3")
    assert restored.attempts("release:failed") == 2
    assert not restored.claim_failure_alert("release:failed")
    assert not restored.claim_once("plan:2026-10-20", AT)
    assert restored.get("release:ok").follow_up.verdict == "held"
    assert restored.facts("release:ok") == ({"a": 1}, {"m": "x"})


def test_unserialisable_facts_never_lose_the_note():
    store = DeskStore(":memory:")
    store.save(note(), {"bad": object.__new__(BadJSON)}, {})
    assert store.get("release:x").status == "ok" and store.facts("release:x") is None


class BadJSON:
    def __str__(self):
        raise ValueError("no text form")
