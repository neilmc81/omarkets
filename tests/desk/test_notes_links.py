"""Notes by time (`GET /desk/notes`) and the deterministic release → note links."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.api import desk as desk_api
from core.desk import playbook as P
from core.desk.links import ReleaseNotes, parse_release_id, release_stamp
from core.desk.models import DeskNote, NoteKind, NoteList
from tests.desk.test_playbook import DAYS, NOW, merged, reaction, row, store
from tests.desk.test_service import RELEASE, draft, make


def note(note_id, at, *, kind=NoteKind.RELEASE, status="ok", written=True):
    return DeskNote(id=note_id, kind=kind, title=f"Title {note_id}", created_at=at,
                    status=status, draft=draft(f"Headline {note_id}") if written else None)


def api(service):
    app = FastAPI()
    app.include_router(desk_api.router)
    app.state.desk = service
    return TestClient(app)


# ------------------------------------------------------------------ note ids


def test_release_ids_are_parsed_and_other_ids_ignored():
    assert parse_release_id("release:20261022T1230:INITIAL_CLAIMS+CONTINUING_CLAIMS") == (
        "20261022T1230", frozenset({"INITIAL_CLAIMS", "CONTINUING_CLAIMS"}))
    for other in ("premarket:2026-10-22", "move:20261022T10", "release:2026102:CPI",
                  "release:20261022T1230", "release:20261022T1230:", "release:2026AB22T1230:CPI"):
        assert parse_release_id(other) is None


def test_release_stamp_is_utc_whatever_the_offset():
    eastern = timezone(timedelta(hours=-4))
    assert release_stamp(datetime(2026, 10, 22, 8, 30, tzinfo=eastern)) == "20261022T1230"
    assert release_stamp(datetime(2026, 10, 22, 12, 30, tzinfo=UTC)) == "20261022T1230"


def test_events_link_on_exact_minute_and_code():
    notes = ReleaseNotes(["release:20261022T1230:CONTINUING_CLAIMS+INITIAL_CLAIMS",
                          "premarket:2026-10-22"])
    at = datetime(2026, 10, 22, 12, 30, tzinfo=UTC)
    assert notes.for_event(at, "INITIAL_CLAIMS") == (
        "release:20261022T1230:CONTINUING_CLAIMS+INITIAL_CLAIMS")
    assert notes.for_event(at, "CPI") is None  # same minute, different release
    assert notes.for_event(at + timedelta(minutes=1), "INITIAL_CLAIMS") is None
    assert notes.for_event(None, "INITIAL_CLAIMS") is None


def test_slot_prefers_the_note_naming_most_codes_then_the_first_id():
    at = datetime(2026, 10, 22, 12, 30, tzinfo=UTC)
    notes = ReleaseNotes(["release:20261022T1230:INITIAL_CLAIMS",
                          "release:20261022T1230:CONTINUING_CLAIMS+INITIAL_CLAIMS"])
    assert notes.for_slot(at, ["INITIAL_CLAIMS", "CONTINUING_CLAIMS"]) == (
        "release:20261022T1230:CONTINUING_CLAIMS+INITIAL_CLAIMS")
    assert notes.for_slot(at, ["INITIAL_CLAIMS"]) == (
        "release:20261022T1230:CONTINUING_CLAIMS+INITIAL_CLAIMS")  # tie: first id
    assert notes.for_slot(at, ["CPI"]) is None


def test_store_links_only_written_release_notes():
    service, _, _ = make(RELEASE)
    service.store.save(note("release:20261020T1230:CPI", RELEASE))
    service.store.save(note("release:20261020T1400:ISM_MFG", RELEASE, status="failed",
                            written=False))
    service.store.save(note("premarket:2026-10-20", RELEASE, kind=NoteKind.PREMARKET))
    assert service.store.release_note_ids() == ["release:20261020T1230:CPI"]
    assert service.release_notes().for_event(RELEASE, "CPI") == "release:20261020T1230:CPI"


def test_ok_note_without_a_draft_is_not_linked_or_listed():
    service, _, _ = make(RELEASE)
    service.store.save(note("release:20261020T1230:CPI", RELEASE, written=False))
    assert service.store.release_note_ids() == []
    assert service.store.readable(RELEASE - timedelta(hours=1), RELEASE + timedelta(hours=1),
                                  10) == []


# ------------------------------------------------------------------ playbook


def test_playbook_releases_carry_their_note_id_when_one_exists():
    series = merged(reaction(DAYS[0], -0.4, +0.6), reaction(DAYS[1], -0.3, -0.5))
    notes = ReleaseNotes([f"release:{release_stamp(DAYS[1])}:INITIAL_CLAIMS"])
    book = P.build([row(DAYS[0]), row(DAYS[1])], store(series), now=NOW, notes=notes)
    by_day = {r.at: r.note_id for r in book.indicators[0].releases}
    assert by_day == {DAYS[1]: "release:20261008T1230:INITIAL_CLAIMS", DAYS[0]: None}
    plain = P.build([row(DAYS[0])], store(series), now=NOW)
    assert plain.indicators[0].releases[0].note_id is None


async def test_playbook_api_links_stored_notes():
    rows = [row(DAYS[1])]
    service, _, _ = make(NOW, rows=rows, series=merged(reaction(DAYS[1], -0.3, -0.5)))
    service.store.save(note("release:20261008T1230:INITIAL_CLAIMS",
                            DAYS[1] + timedelta(minutes=26)))
    body = api(service).get("/api/v1/desk/playbook").json()
    assert body["indicators"][0]["releases"][0]["note_id"] == "release:20261008T1230:INITIAL_CLAIMS"


# ------------------------------------------------------------------ notes by time


def seeded():
    service, clock, _ = make(RELEASE + timedelta(hours=2))
    store = service.store
    store.save(note("a", RELEASE))
    store.save(note("b", RELEASE + timedelta(minutes=30)))
    store.save(note("c", RELEASE + timedelta(hours=1)))
    store.save(note("failed", RELEASE + timedelta(minutes=10), status="failed", written=False))
    store.save(note("old", RELEASE - timedelta(days=8)))
    return service, clock


def test_notes_window_is_half_open_and_newest_first():
    service, _ = seeded()
    client = api(service)
    body = client.get("/api/v1/desk/notes", params={
        "from": RELEASE.isoformat(), "to": (RELEASE + timedelta(hours=1)).isoformat()}).json()
    assert [n["id"] for n in body["notes"]] == ["b", "a"]  # "c" sits exactly on `to`
    assert body["truncated"] is False
    assert body["notes"][0] == {
        "id": "b", "kind": "release", "at": "2026-10-20T13:00:00Z",
        "headline": "Headline b", "summary": "Bottom line.", "tone": "negative",
            "tone_for": "NQ"}
    NoteList.model_validate(body)


def test_notes_default_window_is_the_last_seven_days_and_excludes_failed():
    service, _ = seeded()
    body = api(service).get("/api/v1/desk/notes").json()
    assert [n["id"] for n in body["notes"]] == ["c", "b", "a"]
    assert body["to"] == "2026-10-20T14:30:00Z"
    assert body["from"] == "2026-10-13T14:30:00Z"
    assert body["limit"] == 100


def test_notes_limit_reports_truncation():
    service, _ = seeded()
    body = api(service).get("/api/v1/desk/notes", params={"limit": 2}).json()
    assert [n["id"] for n in body["notes"]] == ["c", "b"]
    assert body["truncated"] is True


def test_notes_empty_window():
    service, _ = seeded()
    body = api(service).get("/api/v1/desk/notes", params={
        "from": "2026-10-01T00:00:00Z", "to": "2026-10-02T00:00:00Z"}).json()
    assert body["notes"] == [] and body["truncated"] is False


@pytest.mark.parametrize("params", [
    {"from": "2026-10-20T12:00:00"},  # no UTC offset
    {"to": "yesterday"},
    {"from": "2026-10-20T13:00:00Z", "to": "2026-10-20T13:00:00Z"},  # empty range
    {"from": "2026-10-21T00:00:00Z", "to": "2026-10-20T00:00:00Z"},  # reversed
    {"limit": 0},
    {"limit": 501},
])
def test_notes_rejects_bad_bounds(params):
    service, _ = seeded()
    assert api(service).get("/api/v1/desk/notes", params=params).status_code == 422


def test_offset_bounds_are_converted_to_utc_across_the_dst_change():
    # US clocks fall back at 06:00 UTC on Sun Nov 1 2026 (EDT -4 -> EST -5).
    service, clock, _ = make(datetime(2026, 11, 2, tzinfo=UTC))
    before = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)  # 01:30 EDT
    after = datetime(2026, 11, 1, 6, 30, tzinfo=UTC)  # 01:30 EST, an hour later
    service.store.save(note("edt", before))
    service.store.save(note("est", after))
    client = api(service)
    # New York midnight Nov 1 (EDT) to midnight Nov 2 (EST) is a 25-hour day.
    body = client.get("/api/v1/desk/notes", params={
        "from": "2026-11-01T00:00:00-04:00", "to": "2026-11-02T00:00:00-05:00"}).json()
    assert [n["id"] for n in body["notes"]] == ["est", "edt"]
    assert body["from"] == "2026-11-01T04:00:00Z" and body["to"] == "2026-11-02T05:00:00Z"
    # The repeated 01:00-02:00 hour: only the EST copy is in this window.
    body = client.get("/api/v1/desk/notes", params={
        "from": "2026-11-01T01:00:00-05:00", "to": "2026-11-01T02:00:00-05:00"}).json()
    assert [n["id"] for n in body["notes"]] == ["est"]


def test_notes_route_does_not_shadow_one_note():
    service, _ = seeded()
    client = api(service)
    assert client.get("/api/v1/desk/notes/a").json()["id"] == "a"
    assert client.get("/api/v1/desk/notes/missing").status_code == 404
