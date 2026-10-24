"""Breaking-news alerts: rules, quietness, dedupe, rate limit and persistence."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from core.desk import breaking as B
from tests.desk.test_service import FakeWriter, make

NOW = datetime(2026, 10, 21, 14, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("headline", "source", "rule"),
    [
        ("Fed announces emergency rate cut of half a point", "cnbc_top", "fed_emergency"),
        ("Federal Reserve Board announces unscheduled meeting", "fed_press_all", "fed_emergency"),
        ("Trump imposes 50% tariffs on European Union imports", "cnbc_top", "tariffs"),
        ("China retaliates with new tariffs on US farm goods", "bbc_world", "tariffs"),
        ("White House raises tariffs to 60% on Chinese chips", "whitehouse_actions", "tariffs"),
        ("US imposes sweeping sanctions on Chinese chipmakers", "cnbc_top", "sanctions"),
        ("China invades Taiwan's outlying islands, state media says", "bbc_world", "conflict"),
        ("Iran strikes Israel with ballistic missiles", "cnbc_top", "conflict"),
        ("Missile attacks on Israel from Iran trigger sirens", "bbc_world", "conflict"),
        ("Argentina defaults on its debt again", "cnbc_top", "default"),
        ("Government shutdown begins after Congress misses deadline", "cnbc_top", "default"),
        ("NYSE halts trading after technical outage", "cnbc_markets", "halt"),
        ("Circuit breakers triggered as S&P 500 plunges 7%", "cnbc_top", "halt"),
    ],
)
def test_market_moving_headlines_match(headline, source, rule):
    assert B.match_rule(headline, source).key == rule


@pytest.mark.parametrize(
    ("headline", "source"),
    [
        # Real false positives seen while calibrating (Sep 20 - Oct 20, 2026).
        ("Levi Strauss hikes profit guidance after tariff refunds, but its sales outlook is "
         "less optimistic", "cnbc_top"),
        ("U.S. strikes Greenland deal at UN to expand military presence at Arctic outposts",
         "cnbc_top"),
        ("Iran reportedly fortifies nuclear sites as it prepares for possible U.S. strikes after "
         "midterms", "cnbc_top"),
        ("U.S. to send third carrier group to Mideast as Trump warns of new Iran strikes",
         "cnbc_top"),
        ("Why Canada is preparing for a (long shot) US invasion", "bbc_world"),
        ("Russia launches largest attack on Ukraine energy infrastructure since spring",
         "bbc_world"),
        # Routine coverage and untrusted sources.
        ("What new tariffs would mean for your shopping?", "cnbc_top"),
        ("Fed officials see emergency of AI investment", "cnbc_top"),  # no rate action
        ("Trump imposes 50% tariffs on European Union imports", "guardian_world"),
        ("Treasury imposes sanctions on cartel money launderers", "ofac_actions"),
    ],
)
def test_routine_or_untrusted_headlines_do_not_match(headline, source):
    assert B.match_rule(headline, source) is None


def test_repeats_and_the_same_story_still_developing_are_suppressed():
    sent = [B.SentAlert("tariffs", "Trump imposes 50% tariffs on EU imports",
                        NOW - timedelta(minutes=40))]
    assert B.is_repeat("Trump imposes 50% tariffs on European Union imports", "tariffs", sent,
                       now=NOW)
    assert B.is_repeat("EU vows response after US raises tariffs", "tariffs", sent, now=NOW)
    assert not B.is_repeat("Iran strikes Israel with missiles", "conflict", sent, now=NOW)
    old = [B.SentAlert("tariffs", "Trump imposes 50% tariffs on EU imports",
                       NOW - timedelta(hours=13))]
    assert not B.is_repeat("Trump imposes 50% tariffs on EU imports", "tariffs", old, now=NOW)


def test_rate_limit_keeps_alerts_rare():
    assert B.rate_allows([], now=NOW)
    assert not B.rate_allows([B.SentAlert("x", "a", NOW - timedelta(minutes=10))], now=NOW)
    four = [B.SentAlert("x", str(i), NOW - timedelta(hours=1 + i)) for i in range(4)]
    assert not B.rate_allows(four, now=NOW)
    assert B.rate_allows(four[:3], now=NOW)


def event(eid, headline, *, source="cnbc_top", minutes_ago=5):
    at = NOW - timedelta(minutes=minutes_ago)
    return SimpleNamespace(id=eid, headline=headline, source=source, published_at=at,
                           received_at=at, source_url=f"https://example.test/{eid}")


async def test_service_alerts_once_notifies_and_feeds_the_next_note():
    news = [
        event("e1", "Trump imposes 50% tariffs on European Union imports"),
        event("e2", "Trump imposes 50% tariffs on EU goods, markets slide", minutes_ago=3),
        event("e3", "Stocks are mixed as investors await earnings"),
        event("e4", "Iran strikes Israel with ballistic missiles", minutes_ago=200),  # stale
    ]
    writer = FakeWriter()
    service, clock, notes = make(NOW, news=news, writer=writer)
    alerts = await service.scan_breaking(NOW)
    assert [a.id for a in alerts] == ["e1"]
    assert notes[-1] == ("Omarkets alert: New tariffs",
                         "Trump imposes 50% tariffs on European Union imports — CNBC")
    assert await service.scan_breaking(NOW + timedelta(minutes=1)) == []  # persisted, deduped
    view = await service.view()
    assert [a.headline for a in view.alerts] == ["Trump imposes 50% tariffs on European Union "
                                                  "imports"]
    note = await service.ask()
    facts = next(f for _, f in writer.calls)
    assert facts["breaking_alerts"][0]["what"] == "New tariffs"
    assert note.status == "ok"


async def test_a_second_kind_of_alert_waits_for_the_gap():
    news = [event("e1", "Trump imposes 50% tariffs on European Union imports"),
            event("e2", "Iran strikes Israel with ballistic missiles", minutes_ago=2)]
    service, _, notes = make(NOW, news=news)
    assert [a.id for a in await service.scan_breaking(NOW)] == ["e1"]
    assert [a.id for a in await service.scan_breaking(NOW + timedelta(minutes=31))] == ["e2"]
