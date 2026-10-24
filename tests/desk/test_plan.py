"""Daily plan notification: once per trading day at 07:00 ET, times in the reader's zone."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from core.desk.models import UpcomingItem
from core.desk.plan import plan_text
from core.desk.service import DeskService, DeskSources
from core.desk.store import DeskStore

TOKYO = ZoneInfo("Asia/Tokyo")
# Thursday Oct 22 2026, 07:00 ET = 11:00 UTC = 20:00 in Tokyo.
SEND = datetime(2026, 10, 22, 11, 0, tzinfo=UTC)


def item(title, at, importance="high", when_text=""):
    return UpcomingItem(kind="economic", title=title, at=at, when_text=when_text,
                        importance=importance)


def test_times_are_in_the_readers_zone_and_next_day_is_marked():
    items = [item("Jobless claims", SEND + timedelta(hours=1, minutes=30)),
             item("10-year auction", SEND + timedelta(hours=6), importance="medium")]
    premarket = SEND + timedelta(hours=2)
    title, body = plan_text(items, now=SEND, premarket_at=premarket, local=TOKYO)
    assert title == "Omarkets today: 2 events, 1 important"
    assert body.splitlines()[:3] == ["21:30  Jobless claims ★",
                                     "02:00 (Fri)  10-year auction",
                                     "Pre-market note at 22:00."]


def test_quiet_day_still_says_when_the_premarket_note_comes():
    title, body = plan_text([], now=SEND, premarket_at=SEND + timedelta(hours=2), local=TOKYO)
    assert title == "Omarkets: quiet day ahead"
    assert "Pre-market note at 22:00." in body


def make(now, rows=()):
    clock = {"now": now}
    sent = []

    async def notify(summary, body):
        sent.append((summary, body))

    def calendar(start, end):
        return [r for r in rows if start <= r.scheduled_at < end]

    service = DeskService(
        sources=DeskSources(market_state=lambda: SimpleNamespace(instruments={}),
                            history=lambda *a, **k: [], calendar_events=calendar,
                            news_events=lambda: []),
        store=DeskStore(":memory:"), writer=None, notify=notify,
        clock=lambda: clock["now"], local_tz=TOKYO,
    )
    return service, sent


async def test_sent_once_per_trading_day_inside_the_window():
    claims = SimpleNamespace(event_code="INITIAL_CLAIMS", name="Unemployment Claims",
                             scheduled_at=SEND + timedelta(hours=1, minutes=30), actual=None,
                             consensus=200_000.0, previous=199_000.0, unit="count",
                             importance="high", status="scheduled", country="US", statement=None)
    service, sent = make(SEND, rows=[claims])
    assert not await service.send_plan(SEND - timedelta(minutes=1))
    assert await service.send_plan(SEND)
    assert not await service.send_plan(SEND + timedelta(minutes=1))
    assert len(sent) == 1 and "21:30  Jobless claims ★" in sent[0][1]


async def test_not_sent_on_weekends_or_late():
    service, sent = make(SEND)
    saturday = SEND + timedelta(days=2)
    assert not await service.send_plan(saturday)
    assert not await service.send_plan(SEND + timedelta(hours=1, minutes=30))  # 08:30 ET
    assert sent == []
