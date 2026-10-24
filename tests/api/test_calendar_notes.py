"""Calendar events carry the desk note for their release, matched in the core."""

import pytest
from starlette.testclient import TestClient

from core.api.app import create_app
from core.api.economics import CalendarResponse, with_note_ids
from core.desk.links import ReleaseNotes
from core.providers.fake import FakeMarketDataProvider
from core.services.economics import EconomicService
from core.services.market_state import MarketStateService
from core.storage.duckdb_repo import DuckDBObservationRepository
from core.storage.economics import SQLiteEconomicRepository
from tests.services.test_economics import T0, FakeClock, batch, event, registry

# T0 is 2026-09-17 12:00 UTC; the fixture event is a CPI release at T0.
CPI_NOTE = "release:20260917T1200:CPI+CORE_CPI"


class FakeDesk:
    def __init__(self, note_ids):
        self.note_ids = note_ids

    async def start(self):
        pass

    async def stop(self):
        pass

    def release_notes(self):
        return ReleaseNotes(self.note_ids)


def client_with(tmp_path, desk):
    economics = EconomicService(
        repository=SQLiteEconomicRepository(tmp_path / "economic.sqlite3", "live"),
        registry=registry(), mode="live", clock=FakeClock(),
    )
    market = MarketStateService(provider=FakeMarketDataProvider(update_interval=1000),
                                repository=DuckDBObservationRepository(":memory:"))
    return TestClient(create_app(service=market, economic_service=economics,
                                 desk_service=desk)), economics


@pytest.mark.parametrize("path", ["/calendar", "/calendar/today", "/calendar/week"])
def test_calendar_events_carry_the_note_id(tmp_path, path):
    http, economics = client_with(tmp_path, FakeDesk([CPI_NOTE, "premarket:2026-09-17"]))
    with http:
        http.portal.call(economics.accept_batch, batch())
        body = http.get("/api/v1" + path).json()
    CalendarResponse.model_validate(body)
    [cpi] = [e for e in body["events"] if e["id"] == "cpi-release-2026-09"]
    assert cpi["note_id"] == CPI_NOTE


def test_note_id_is_null_without_a_note_or_without_the_desk(tmp_path):
    for desk in (FakeDesk(["release:20260917T1230:CPI"]), None):
        http, economics = client_with(tmp_path / str(id(desk)), desk)
        with http:
            http.portal.call(economics.accept_batch, batch())
            body = http.get("/api/v1/calendar").json()
        assert body["events"] and all(e["note_id"] is None for e in body["events"])


def test_matching_needs_the_same_minute_code_and_country():
    notes = ReleaseNotes([CPI_NOTE])
    events = [event(), event(id="ppi", event_code="PPI"), event(id="uk", country="GB"),
              event(id="later", scheduled_at=T0.replace(minute=1)),
              event(id="unscheduled", scheduled_at=None)]
    linked = {e.id: e.note_id for e in with_note_ids(events, notes)}
    assert linked == {"cpi-release-2026-09": CPI_NOTE, "ppi": None, "uk": None,
                      "later": None, "unscheduled": None}
    assert all(e.note_id is None for e in with_note_ids(events, None))
