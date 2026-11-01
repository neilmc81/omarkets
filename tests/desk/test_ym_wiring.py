"""Phase 2: YM (Dow futures) is wired through the desk like ES.

YM is in the production default on desk:v9. YM gets move and ask notes
with its own 3-hour move limit, never a pre-market, and its notes are written
from its own prompt and say they are about YM, never NQ."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.api import desk as desk_api
from core.config.settings import Settings
from core.desk import facts as F
from core.desk import identity as ID
from core.desk import prompts as P
from core.desk.models import NoteKind
from core.desk.service import DeskService
from core.domain.workspace import FocusUpdate
from core.storage.workspace import WorkspaceStore
from tests.desk.test_complete_inputs import inst, state
from tests.desk.test_identity import MOVE_AT, falling
from tests.desk.test_service import FakeWriter, make

YM = ID.Instrument.of("YM")


class V9Writer(FakeWriter):
    prompt_version = "desk:v9"


class V8Writer(FakeWriter):
    prompt_version = "desk:v8"


def desk(now, series=None, instruments=("NQ", "ES", "YM")):
    return make(now, series=series, writer=V9Writer(), instruments=instruments)


def flat(value, now):
    return falling(value, now, step=0.0)


# --------------------------------------------------------------------------- defaults


def test_the_default_desk_includes_ym_on_desk_v9():
    assert ID.PRODUCTION_INSTRUMENTS[:3] == ("NQ", "ES", "YM")
    settings = Settings(_env_file=None)
    assert settings.desk_instruments[:3] == ["NQ", "ES", "YM"]
    assert settings.desk_prompt_version == "desk:v9"
    assert all(P.has_profile(s, version=P.V9) for s in settings.desk_instruments)


def test_ym_gets_move_and_ask_only():
    assert ID.instrument_kinds(YM) == {NoteKind.MOVE, NoteKind.ASK}
    assert NoteKind.PREMARKET not in ID.instrument_kinds("YM")
    assert ID.note_id(NoteKind.MOVE, "20261020T10", YM) == "move:YM:20261020T10"
    assert ID.parse_note_id("ask:YM:20261020T100000").instrument == "YM"


def test_ym_on_desk_v8_is_refused_before_anything_runs():
    base, _, _ = make(MOVE_AT)
    with pytest.raises(ValueError, match="no desk:v8 prompt for YM"):
        DeskService(sources=base.sources, store=base.store, writer=V8Writer(),
                    instruments=("NQ", "YM"))
    assert not P.has_profile("YM", version=P.V8)
    # Taking YM out leaves a startable desk:v8 NQ-only desk.
    DeskService(sources=base.sources, store=base.store, writer=V8Writer(), instruments=("NQ",))


# --------------------------------------------------------------------------- prompt


@pytest.mark.parametrize("kind", [NoteKind.MOVE, NoteKind.ASK])
def test_ym_notes_use_ym_s_own_v9_prompt(kind):
    prompt = P.render(kind, YM, {})
    profile = P.PROMPTS[P.V9]["YM"]
    assert prompt.instrument == "YM" and prompt.system == profile.system
    assert prompt.system != P.PROMPTS[P.V9]["NQ"].system
    assert prompt.system != P.PROMPTS[P.V9]["ES"].system
    assert "Dow futures (YM)" in prompt.system and "Dow Jones Industrial Average" in prompt.system
    assert "ym_price_location" in prompt.system and '"ym_is"' in prompt.system
    assert "nq_price_location" not in prompt.system
    assert "supportive or negative for YM" in prompt.system
    assert "YM" in prompt.task and "NQ has" not in prompt.task


def test_ym_prompt_says_borrowed_facts_are_about_other_markets():
    system = P.PROMPTS[P.V9]["YM"].system
    # nq_vs_es and for_nq are NQ/ES facts; the VIX is the S&P 500's gauge.
    assert "say it is about NQ and ES, not the Dow" in system
    assert "say it is about NQ, not YM" in system
    assert "say it is the S&P 500's gauge" in system and "not the Dow's" in system
    assert P.PROMPTS[P.V9]["YM"].rates_read_required


def test_ym_profile_has_no_premarket_wording_in_use_and_no_event_tasks():
    # The profile has every per-instrument task (like ES); the service never
    # schedules YM's pre-market, and event notes stay on NQ's profile.
    assert set(P.PROMPTS[P.V9]["YM"].tasks) == ID.INSTRUMENT_KINDS
    assert P.render(NoteKind.FED, None, {}).system == P.PROMPTS[P.V9]["NQ"].system


# --------------------------------------------------------------------------- facts


def test_ym_reads_the_vix_and_gets_nq_es_context():
    assert F.VOL_INDEXES["YM"] == ("VIX", None)
    assert set(F.participation(state(), "YM")) == {"nq_vs_es", "megacap_participation"}


def test_a_ym_note_s_markets_today_includes_ym():
    assert "YM" not in F.STRIP_SYMBOLS
    service, _, _ = desk(MOVE_AT)
    service.sources.market_state().instruments.update(
        NQ=inst("NQ", 29_000.0, at=MOVE_AT), YM=inst("YM", 47_100.0, at=MOVE_AT))
    ym_facts, _ = service._common(MOVE_AT, YM)
    nq_facts, _ = service._common(MOVE_AT)
    assert "Dow futures" in [row["market"] for row in ym_facts["markets_today"]]
    assert "Dow futures" not in [row["market"] for row in nq_facts["markets_today"]]


# --------------------------------------------------------------------------- moves


async def test_ym_move_fires_when_nq_and_es_are_flat():
    series = {"NQ": flat(30000.0, MOVE_AT), "ES": flat(6000.0, MOVE_AT),
              "YM": falling(47000.0)}
    service, _, _ = desk(MOVE_AT, series)
    [note] = await service.tick()
    assert note.id == "move:YM:20261020T10" and note.instrument == "YM"
    assert note.title.startswith("YM down") and note.tone_for == "YM"
    [(kind, facts)] = service.writer.calls
    assert kind is NoteKind.MOVE
    assert facts["the_move"]["market"] == "Dow futures"
    assert "Dow futures" in [m["market"] for m in facts["other_markets_same_window"]]


async def test_nq_move_fires_when_ym_is_flat():
    series = {"NQ": falling(30000.0), "YM": flat(47000.0, MOVE_AT)}
    service, _, _ = desk(MOVE_AT, series)
    [note] = await service.tick()
    assert note.id == "move:20261020T10" and note.instrument == "NQ"


async def test_ym_has_its_own_three_hour_move_limit():
    series = {"NQ": flat(30000.0, MOVE_AT), "ES": flat(6000.0, MOVE_AT),
              "YM": falling(47000.0)}
    service, clock, _ = desk(MOVE_AT, series)
    assert [n.id for n in await service.tick()] == ["move:YM:20261020T10"]

    # An hour on, all three fall: YM is inside its 3 hours; NQ and ES write.
    later = MOVE_AT + timedelta(hours=1)
    series.update(NQ=falling(30000.0, later), ES=falling(6000.0, later),
                  YM=falling(47000.0, later))
    clock["now"] = later
    assert sorted(n.id for n in await service.tick()) == [
        "move:20261020T11", "move:ES:20261020T11"]

    # Past YM's 3 hours but inside NQ's and ES's: only YM writes again.
    last = MOVE_AT + timedelta(hours=3, minutes=5)
    series.update(NQ=falling(30000.0, last), ES=falling(6000.0, last),
                  YM=falling(47000.0, last))
    clock["now"] = last
    [note] = await service.tick()
    assert note.id.startswith("move:YM:") and note.id != "move:YM:20261020T10"
    assert service.store.count_since("move", MOVE_AT.isoformat(), instrument="YM") == 2
    assert service.store.count_since("move", MOVE_AT.isoformat(), instrument="ES") == 1
    assert service.store.count_since("move", MOVE_AT.isoformat(), instrument="NQ") == 1


async def test_ym_move_is_not_written_when_ym_is_not_enabled():
    series = {"NQ": flat(30000.0, MOVE_AT), "YM": falling(47000.0)}
    service, _, _ = desk(MOVE_AT, series, instruments=("NQ", "ES"))
    assert await service.tick() == []
    assert service.writer.calls == []


async def test_no_ym_premarket_is_scheduled():
    # Tuesday 09:05 ET: inside the pre-market window.
    now = datetime(2026, 10, 20, 13, 5, tzinfo=UTC)
    service, _, _ = desk(now)
    jobs = await service.due_jobs(now)
    assert [j[0] for j in jobs if j[1] is NoteKind.PREMARKET] == ["premarket:2026-10-20"]
    await service.tick()
    pre = [n for n in service.store.recent(include_failed=True) if n.kind is NoteKind.PREMARKET]
    assert [(n.id, n.instrument) for n in pre] == [("premarket:2026-10-20", "NQ")]


# --------------------------------------------------------------------------- POST /desk/ask


def client(service, focus: str | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(desk_api.router)
    app.state.desk = service
    if focus is not None:
        store = WorkspaceStore(":memory:")
        store.set_focus(FocusUpdate(symbol=focus))
        app.state.workspace = store
    return TestClient(app)


def test_ask_writes_for_ym_when_enabled():
    service, _, _ = desk(MOVE_AT)
    response = client(service).post("/api/v1/desk/ask", params={"instrument": "YM"})
    assert response.status_code == 200
    body = response.json()
    assert body["id"].startswith("ask:YM:") and body["instrument"] == "YM"
    assert body["tone_for"] == "YM"
    [(kind, _)] = service.writer.calls
    assert kind is NoteKind.ASK


def test_ask_on_a_ym_focus_writes_ym_not_nq():
    service, _, _ = desk(MOVE_AT)
    response = client(service, focus="YM").post("/api/v1/desk/ask")
    assert response.status_code == 200
    assert response.json()["id"].startswith("ask:YM:") and response.json()["instrument"] == "YM"


def test_ask_for_ym_includes_ym_in_the_last_two_hours():
    # Quotes every minute over the last three hours: the ask measures two.
    series = {"YM": [SimpleNamespace(timestamp=MOVE_AT - timedelta(minutes=180 - i),
                                     value=47000.0 - i) for i in range(180)]}
    service, _, _ = desk(MOVE_AT, series)
    assert client(service).post("/api/v1/desk/ask",
                                params={"instrument": "YM"}).status_code == 200
    [(_, facts)] = service.writer.calls
    assert "Dow futures" in [m["market"] for m in facts["last_two_hours"]]


def test_ask_refuses_ym_when_the_desk_does_not_write_for_it():
    service, _, _ = desk(MOVE_AT, instruments=("NQ", "ES"))
    response = client(service, focus="YM").post("/api/v1/desk/ask")
    assert response.status_code == 400
    assert response.json()["detail"] == "The desk does not write for YM"
    assert service.writer.calls == [] and service.store.recent(include_failed=True) == []

