"""News page API contract: GET /api/v1/desk/news."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from core.desk.models import NewsBoard, NewsItem
from tests.api.test_calendar_notes import client_with

NOW = datetime(2026, 10, 22, 16, 0, tzinfo=UTC)


class NewsDesk:
    def __init__(self):
        self.asked = []

    async def start(self):
        pass

    async def stop(self):
        pass

    async def news(self, hours):
        self.asked.append(hours)
        item = NewsItem(id="a", at=NOW, source="cnbc_top", source_label="CNBC",
                        headline="Fed holds rates", category="fed",
                        category_label="Fed & central banks", nq_15m_status="waiting")
        return NewsBoard(generated_at=NOW, start=NOW - timedelta(hours=hours), items=[item],
                         hidden=0)


def test_contract_and_window(tmp_path):
    desk = NewsDesk()
    http, _ = client_with(tmp_path, desk)
    with http:
        board = NewsBoard.model_validate(http.get("/api/v1/desk/news").json())
        assert http.get("/api/v1/desk/news?hours=48").status_code == 200
        assert http.get("/api/v1/desk/news?hours=0").status_code == 422
        assert http.get("/api/v1/desk/news?hours=73").status_code == 422
    assert board.items[0].category == "fed" and desk.asked == [24, 48]
