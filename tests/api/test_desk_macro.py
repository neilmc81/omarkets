"""Macro page API contract: GET /api/v1/desk/macro."""

from __future__ import annotations

from datetime import UTC, datetime

from core.desk import macro as M
from core.desk.models import MacroBoard
from tests.api.test_calendar_notes import client_with

NOW = datetime(2026, 10, 23, 15, 0, tzinfo=UTC)


class MacroDesk:
    """A desk stand-in that builds the board from the series it is given."""

    def __init__(self):
        self.seen = None

    async def start(self):
        pass

    async def stop(self):
        pass

    async def macro(self, series):
        self.seen = list(series)
        return M.board(self.seen, [], None, now=NOW)


def test_contract_passes_the_economic_series_to_the_desk(tmp_path):
    desk = MacroDesk()
    http, _ = client_with(tmp_path, desk)
    with http:
        response = http.get("/api/v1/desk/macro")
    assert response.status_code == 200
    board = MacroBoard.model_validate(response.json())
    assert [g.key for g in board.groups] == ["inflation", "jobs", "growth", "fed"]
    assert desk.seen is not None
    # Nothing stored yet: every card is unavailable, never zero.
    assert all(c.value is None and c.value_text == "—"
               for g in board.groups for c in g.cards)


def test_no_desk_is_503(tmp_path):
    http, _ = client_with(tmp_path, None)
    with http:
        assert http.get("/api/v1/desk/macro").status_code == 503
