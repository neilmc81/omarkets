"""API contract for the rating and scorecard endpoints."""

from datetime import timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.api import desk as desk_api
from core.desk.models import DeskNote, NoteList
from core.desk.scorecard import Scorecard
from tests.desk.test_scorecard import NOW, seeded
from tests.desk.test_service import make


def client(store=None):
    service, _, _ = make(NOW)
    if store is not None:
        service.store = store
    app = FastAPI()
    app.include_router(desk_api.router)
    app.state.desk = service
    return TestClient(app), service


def test_rate_change_and_clear_a_note():
    http, service = client(seeded())
    url = "/api/v1/desk/notes/move:NQ:a/rating"
    http.delete(url)  # seeded as useful; start from unrated
    assert http.get("/api/v1/desk/notes/move:NQ:a").json()["rating"] is None
    reply = http.put(url, json={"rating": "useful"})
    assert reply.status_code == 200
    assert reply.json() == {"note_id": "move:NQ:a", "rating": "useful",
                            "rated_at": reply.json()["rated_at"]}
    assert reply.json()["rated_at"].endswith("Z") or "+00:00" in reply.json()["rated_at"]
    assert http.put(url, json={"rating": "not_useful"}).json()["rating"] == "not_useful"
    note = DeskNote.model_validate(http.get("/api/v1/desk/notes/move:NQ:a").json())
    assert note.rating == "not_useful"
    listed = NoteList.model_validate(http.get("/api/v1/desk/notes", params={
        "from": (NOW - timedelta(days=30)).isoformat(), "to": NOW.isoformat()}).json())
    assert {n.id: n.rating for n in listed.notes}["move:NQ:a"] == "not_useful"
    assert http.delete(url).json() == {"note_id": "move:NQ:a", "rating": None, "rated_at": None}
    assert http.get("/api/v1/desk/notes/move:NQ:a").json()["rating"] is None
    assert service.store.get("move:NQ:a").rating is None


def test_rating_is_validated():
    http, _ = client(seeded())
    url = "/api/v1/desk/notes/move:NQ:a/rating"
    assert http.put(url, json={"rating": "love"}).status_code == 422
    assert http.put(url, json={}).status_code == 422
    assert http.put(url, json={"rating": "useful", "extra": 1}).status_code == 422
    assert http.put("/api/v1/desk/notes/nope/rating", json={"rating": "useful"}).status_code == 404
    assert http.delete("/api/v1/desk/notes/nope/rating").status_code == 404
    assert http.put("/api/v1/desk/notes/move:VIX:e/rating",
                    json={"rating": "useful"}).status_code == 404  # a failed note
    assert http.get("/api/v1/desk/notes/move:NQ:a").json()["rating"] == "useful"  # as seeded


def test_desk_view_notes_carry_the_rating():
    store = seeded()
    http, service = client(store)
    service.store.set_rating("move:NQ:a", "useful", NOW)
    notes = {n.id: n.rating for n in service.store.recent(50)}
    assert notes["move:NQ:a"] == "useful" and notes["move:ES:b"] == "not_useful"


def test_scorecard_default_is_the_last_week_and_the_week_before():
    http, _ = client(seeded())
    reply = http.get("/api/v1/desk/scorecard")
    assert reply.status_code == 200
    card = Scorecard.model_validate(reply.json())
    assert card.current.end == NOW and card.current.start == NOW - timedelta(days=7)
    assert card.previous.end == card.current.start
    assert card.previous.start == NOW - timedelta(days=14)
    assert (card.current.written, card.current.failed) == (7, 1)
    assert (card.previous.written, card.previous.failed) == (2, 0)


def test_scorecard_range_and_compare_are_validated_and_honoured():
    http, _ = client(seeded())
    day = timedelta(days=1)
    reply = http.get("/api/v1/desk/scorecard", params={
        "from": (NOW - 3 * day).isoformat(), "to": NOW.isoformat(), "compare": "false"})
    card = Scorecard.model_validate(reply.json())
    assert card.previous is None and card.current.written == 6  # the notes made in the last 3 days
    bad = http.get("/api/v1/desk/scorecard", params={
        "from": NOW.isoformat(), "to": (NOW - day).isoformat()})
    assert bad.status_code == 422
    naive = http.get("/api/v1/desk/scorecard", params={"from": "2026-10-01T00:00:00"})
    assert naive.status_code == 422  # a bound needs a UTC offset


def test_scorecard_on_an_empty_store_is_all_zeros_not_an_error():
    http, _ = client()
    card = Scorecard.model_validate(http.get("/api/v1/desk/scorecard").json())
    assert card.current.written == 0 and card.current.groups == [] and card.previous.groups == []


def test_scorecard_without_the_desk_is_503():
    app = FastAPI()
    app.include_router(desk_api.router)
    assert TestClient(app).get("/api/v1/desk/scorecard").status_code == 503
    assert TestClient(app).put("/api/v1/desk/notes/x/rating",
                               json={"rating": "useful"}).status_code == 503
