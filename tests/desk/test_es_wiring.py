"""Phase 2 slice 1: ES can be switched on (``desk_instruments``) without
changing the production default (NQ only, desk:v8)."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.api import desk as desk_api
from core.desk import identity as ID
from core.desk.models import NoteKind
from tests.desk.test_identity import MOVE_AT, falling
from tests.desk.test_service import FakeWriter, make

ES = ID.Instrument.of("ES")


class V9Writer(FakeWriter):
    """desk:v9 has an ES profile; desk:v8 is NQ's text only."""

    prompt_version = "desk:v9"


def desk(now, series=None):
    return make(now, series=series, writer=V9Writer(), instruments=("NQ", "ES"))


def flat(value, now):
    return falling(value, now, step=0.0)


def test_nq_gets_every_instrument_kind_and_others_move_and_ask_only():
    assert ID.instrument_kinds(ID.NQ) == ID.INSTRUMENT_KINDS
    assert ID.instrument_kinds("ES") == {NoteKind.MOVE, NoteKind.ASK}
    assert ID.instrument_kinds("YM") == ID.SECONDARY_KINDS


async def test_es_move_fires_when_nq_is_flat():
    service, _, _ = desk(MOVE_AT, {"NQ": flat(30000.0, MOVE_AT), "ES": falling(6000.0)})
    [note] = await service.tick()
    assert note.id == "move:ES:20261020T10" and note.instrument == "ES"
    assert note.title.startswith("ES down")


async def test_nq_move_fires_when_es_is_flat():
    service, _, _ = desk(MOVE_AT, {"NQ": falling(30000.0), "ES": flat(6000.0, MOVE_AT)})
    [note] = await service.tick()
    assert note.id == "move:20261020T10" and note.instrument == "NQ"


async def test_each_instrument_has_its_own_three_hour_move_limit():
    series = {"NQ": flat(30000.0, MOVE_AT), "ES": falling(6000.0)}
    service, clock, _ = desk(MOVE_AT, series)
    assert [n.id for n in await service.tick()] == ["move:ES:20261020T10"]

    # An hour on both fall: ES is inside its own 3 hours, NQ has not used its.
    later = MOVE_AT + timedelta(hours=1)
    series.update(NQ=falling(30000.0, later), ES=falling(6000.0, later))
    clock["now"] = later
    assert [n.id for n in await service.tick()] == ["move:20261020T11"]

    # Past ES's 3 hours but inside NQ's: only ES writes again.
    last = MOVE_AT + timedelta(hours=3, minutes=5)
    series.update(NQ=falling(30000.0, last), ES=falling(6000.0, last))
    clock["now"] = last
    [note] = await service.tick()
    assert note.id.startswith("move:ES:") and note.id != "move:ES:20261020T10"
    assert service.store.count_since("move", MOVE_AT.isoformat(), instrument="ES") == 2
    assert service.store.count_since("move", MOVE_AT.isoformat(), instrument="NQ") == 1


async def test_no_es_premarket_is_scheduled():
    # Tuesday 09:05 ET: inside the pre-market window.
    now = datetime(2026, 10, 20, 13, 5, tzinfo=UTC)
    service, _, _ = desk(now)
    jobs = await service.due_jobs(now)
    assert [j[0] for j in jobs if j[1] is NoteKind.PREMARKET] == ["premarket:2026-10-20"]
    await service.tick()
    pre = [n for n in service.store.recent(include_failed=True) if n.kind is NoteKind.PREMARKET]
    assert [(n.id, n.instrument) for n in pre] == [("premarket:2026-10-20", "NQ")]


# --------------------------------------------------------------------------- POST /desk/ask


def client(service) -> TestClient:
    app = FastAPI()
    app.include_router(desk_api.router)
    app.state.desk = service
    return TestClient(app)


def test_ask_defaults_to_nq():
    service, _, _ = make(MOVE_AT)
    response = client(service).post("/api/v1/desk/ask")
    assert response.status_code == 200
    assert response.json()["id"].startswith("ask:2026") and response.json()["instrument"] == "NQ"


def test_ask_refuses_an_instrument_that_is_not_enabled_before_any_call():
    service, _, _ = make(MOVE_AT)  # production default: NQ only
    response = client(service).post("/api/v1/desk/ask", params={"instrument": "ES"})
    assert response.status_code == 400
    assert response.json()["detail"] == "The desk does not write for ES"
    assert service.writer.calls == [] and service.store.recent(include_failed=True) == []


@pytest.mark.parametrize("bad", ["es", "N Q", "TOOLONGSYMBOL"])
def test_ask_rejects_a_malformed_instrument(bad):
    service, _, _ = make(MOVE_AT)
    assert client(service).post("/api/v1/desk/ask",
                                params={"instrument": bad}).status_code == 422
    assert service.writer.calls == []


def test_ask_writes_for_an_enabled_instrument():
    service, _, _ = desk(MOVE_AT)
    response = client(service).post("/api/v1/desk/ask", params={"instrument": "ES"})
    assert response.status_code == 200
    assert response.json()["id"].startswith("ask:ES:") and response.json()["instrument"] == "ES"
    [(kind, _)] = service.writer.calls
    assert kind is NoteKind.ASK
