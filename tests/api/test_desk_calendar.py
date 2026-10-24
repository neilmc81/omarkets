"""Calendar page API contract: GET /api/v1/desk/calendar."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from core.desk.links import ReleaseNotes
from core.desk.models import CalendarBoard, CalendarRow
from tests.api.test_calendar_notes import client_with


def calendar_row(at, title="Jobless claims", importance="high"):
    return CalendarRow(at=at, title=title, codes=["INITIAL_CLAIMS"], importance=importance,
                       kind="economic", released=False, nq_15m_status="upcoming")


class CalendarDesk:
    """A desk stand-in: fixed clock and rows, recording the asked window."""

    local_tz = ZoneInfo("Asia/Tokyo")

    def __init__(self, now, rows=(), hidden=0):
        self.now, self.rows, self.hidden, self.asked = now, list(rows), hidden, []

    async def start(self):
        pass

    async def stop(self):
        pass

    def clock(self):
        return self.now

    def release_notes(self):
        return ReleaseNotes([])

    async def calendar(self, start, end, *, show_all):
        self.asked.append((start, end, show_all))
        return [r for r in self.rows if start <= r.at < end], self.hidden


def get(tmp_path, desk, query=""):
    http, _ = client_with(tmp_path, desk)
    with http:
        return http.get("/api/v1/desk/calendar" + query)


def test_contract_groups_by_the_reader_day(tmp_path):
    now = datetime(2026, 10, 22, 10, 0, tzinfo=UTC)
    rows = [calendar_row(datetime(2026, 10, 22, 12, 30, tzinfo=UTC)),
            calendar_row(datetime(2026, 10, 22, 18, 0, tzinfo=UTC), "Fed rate decision")]
    desk = CalendarDesk(now, rows, hidden=3)
    response = get(tmp_path, desk)
    assert response.status_code == 200
    board = CalendarBoard.model_validate(response.json())
    assert board.tz == "Asia/Tokyo" and board.week == "this" and not board.all
    assert board.hidden == 3 and board.coverage
    # 18:00 UTC Thursday is 03:00 Friday in Tokyo.
    assert [(d.date, d.label, [r.title for r in d.rows]) for d in board.days] == [
        ("2026-10-22", "Thursday, Oct 22", ["Jobless claims"]),
        ("2026-10-23", "Friday, Oct 23", ["Fed rate decision"])]
    ny = CalendarBoard.model_validate(get(tmp_path / "ny", CalendarDesk(now, rows),
                                          "?tz=America/New_York").json())
    assert [d.date for d in ny.days] == ["2026-10-22"]


def test_weeks_are_new_york_monday_to_monday_across_the_clock_change(tmp_path):
    # Friday Oct 30 2026; US clocks go back on Sunday Nov 1.
    desk = CalendarDesk(datetime(2026, 10, 30, 15, 0, tzinfo=UTC))
    assert get(tmp_path, desk, "?all=true").status_code == 200
    assert get(tmp_path / "next", desk, "?week=next").status_code == 200
    (this_start, this_end, this_all), (next_start, next_end, next_all) = desk.asked
    assert this_all and not next_all
    assert this_start == datetime(2026, 10, 26, 4, 0, tzinfo=UTC)  # Mon 00:00 EDT
    assert this_end == next_start == datetime(2026, 11, 2, 5, 0, tzinfo=UTC)  # Mon 00:00 EST
    assert next_end == datetime(2026, 11, 9, 5, 0, tzinfo=UTC)


def test_bad_parameters_and_no_desk(tmp_path):
    desk = CalendarDesk(datetime(2026, 10, 22, tzinfo=UTC))
    assert get(tmp_path, desk, "?tz=Not/AZone").status_code == 422
    assert get(tmp_path / "w", desk, "?week=last").status_code == 422
    assert get(tmp_path / "none", None).status_code == 503
