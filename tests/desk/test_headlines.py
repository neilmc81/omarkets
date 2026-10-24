"""The News page's view model: rule-based tags and NQ's move (no AI)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from core.desk import headlines as HL

NOW = datetime(2026, 10, 22, 16, 0, tzinfo=UTC)
CLAIMS = datetime(2026, 10, 22, 12, 30, tzinfo=UTC)


def news(headline, at, source="cnbc_top", id_=None):
    return SimpleNamespace(id=id_ or headline, headline=headline, source=source,
                           published_at=at, received_at=at, source_url="https://example.test")


def release(at, code="INITIAL_CLAIMS", importance="high"):
    return SimpleNamespace(event_code=code, name="Unemployment Claims", scheduled_at=at,
                           actual=197_000.0, consensus=201_000.0, previous=None, unit="count",
                           importance=importance, status="released", country="US")


def obs(ts, value):
    return SimpleNamespace(timestamp=ts, value=value)


def history_of(series):
    calls = []

    def history(symbol, *, start=None, end=None, limit=500):
        calls.append(symbol)
        return [o for o in series.get(symbol, []) if start <= o.timestamp <= end][-limit:]
    history.calls = calls
    return history


@pytest.mark.parametrize(("headline", "source", "expected"), [
    ("Fed's Goolsbee: policy remains in a good place", "cnbc_top", "fed"),
    ("Statement on monetary policy", "fed_press_monetary", "fed"),
    ("Initial jobless claims fall to 194,000", "cnbc_top", "economy"),
    ("SK Hynix shares reverse losses after chipmaker drops IPO review", "cnbc_top", "big_tech"),
    ("Brent crude oil tops $115, a five month high", "cnbc_top", "energy"),
    ("Russia and Ukraine agree to local ceasefire", "guardian_world", "geopolitics"),
    ("Prince William makes surprise appearance on chat show", "guardian_world", None),
])
def test_categories_come_from_fixed_rules(headline, source, expected):
    assert HL.categorise(headline, source) == expected


def test_items_tag_scheduled_measure_nq_and_hide_unclassified():
    series = {"NQ": [obs(CLAIMS - timedelta(seconds=20), 29_000.0),
                     obs(CLAIMS + timedelta(minutes=14), 28_982.6),
                     obs(NOW - timedelta(minutes=5), 29_100.0)]}
    history = history_of(series)
    found = HL.items(
        [news("Initial jobless claims fall to 194,000", CLAIMS, id_="a1"),
         news("Initial jobless claims fall to 194,000", CLAIMS),  # the same title again
         news("Oil jumps after reports of new Middle East tensions", NOW - timedelta(minutes=10)),
         news("Prince William on a chat show", NOW - timedelta(hours=2), "guardian_world"),
         news("Too old: Fed holds rates", NOW - timedelta(days=2))],
        [release(CLAIMS)], history, now=NOW, start=NOW - HL.WINDOW, alert_ids=["a1"])
    assert [i.headline[:7] for i in found] == ["Oil jum", "Prince ", "Initial"]
    oil, prince, claims = found
    assert claims.category == "economy" and claims.scheduled and claims.alert
    assert claims.nq_15m == -0.06 and claims.nq_15m_status == "measured"
    assert claims.source_label == "CNBC" and claims.category_label == "Economy"
    assert oil.category == "energy" and not oil.scheduled and oil.nq_15m_status == "waiting"
    assert prince.category is None and prince.nq_15m_status == "unmeasured"
    board = HL.board(found, [], now=NOW, start=NOW - HL.WINDOW)
    assert board.hidden == 1
    # NQ is read once for the whole window, not per headline.
    assert history.calls == ["NQ"]


def test_economy_headline_hours_after_a_release_is_unscheduled():
    found = HL.items([news("Retail sales jump", CLAIMS + timedelta(hours=3))],
                     [release(CLAIMS)], history_of({}), now=NOW, start=NOW - HL.WINDOW)
    assert not found[0].scheduled and found[0].nq_15m_status == "unavailable"


def test_preloaded_answers_like_the_store():
    rows = [obs(CLAIMS + timedelta(minutes=m), 100.0 + m) for m in range(0, 60, 5)]
    store = history_of({"NQ": rows})
    fast = HL.preloaded(store, "NQ", start=CLAIMS, end=CLAIMS + timedelta(hours=1))
    for start, end, limit in [(CLAIMS, CLAIMS + timedelta(minutes=12), 1),
                              (CLAIMS + timedelta(minutes=5), CLAIMS + timedelta(minutes=30), 3)]:
        assert list(fast("NQ", start=start, end=end, limit=limit)) == \
            list(store("NQ", start=start, end=end, limit=limit))
    # Outside the preloaded window it asks the store.
    before = len(store.calls)
    fast("NQ", start=CLAIMS - timedelta(hours=2), end=CLAIMS, limit=1)
    assert len(store.calls) == before + 1
