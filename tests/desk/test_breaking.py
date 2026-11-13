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
        ("US attacks Iran and further escalation is unlikely", "bbc_world", "conflict"),
        ("US attacks Iran after Israel rules out invasion", "bbc_world", "conflict"),
        ("US attacks Iran not likely to withdraw", "bbc_world", "conflict"),
        ("US strikes on Iran not expected to cause an oil spike", "bbc_world", "conflict"),
        ("US attack on Iran not likely to be the last", "bbc_world", "conflict"),
        ("US rules out ceasefire as attacks on Iran continue", "bbc_world", "conflict"),
        ("US rules out ceasefire after attacks on Iran kill 20", "bbc_world", "conflict"),
        ("Pentagon rules out pause as strikes on Iran widen", "bbc_world", "conflict"),
        ("US attacks Iran, war not expected to end soon", "bbc_world", "conflict"),
        ("Iran denies attacks on Israel, launches new strikes on Israel", "bbc_world", "conflict"),
        ("Iran denies attacks on Israel. US attacks Iran", "bbc_world", "conflict"),
        ("US will not launch an attack on Iran but Israel attacks Iran", "bbc_world", "conflict"),
        ("Israel will not attack Iran, but Iran strikes Israel", "bbc_world", "conflict"),
        ("US attacks Iran: China denies invasion of Taiwan", "bbc_world", "conflict"),
        ("Iran strikes Israel denies invasion of Taiwan", "bbc_world", "conflict"),
        ("US launches new attacks on Iran, Pentagon confirms", "bbc_world", "conflict"),
        ("US attacks Iran, Pentagon denies civilian casualties", "bbc_world", "conflict"),
        ("US attacks Iran and won't stop attacks", "bbc_world", "conflict"),
        ("US attacks Iran and won\u2019t stop attacks", "bbc_world", "conflict"),
        ("US attacks Iran, unlikely to withdraw", "bbc_world", "conflict"),
        ("US attacks Iran as planned", "bbc_world", "conflict"),
        ("Iran rejects ceasefire deal, launches new attacks on Israel", "bbc_world", "conflict"),
        ("US will not halt strikes on Iran, officials say", "bbc_world", "conflict"),
        ("Iran won't stop attacks on Israel, commander vows", "bbc_world", "conflict"),
        ("Iran rules out talks after missile strikes on Israel", "bbc_world", "conflict"),
        ("US attacks Iran, no attackers injured", "bbc_world", "conflict"),
        ("No attack on Iran yet, but US strikes Syria", "bbc_world", "conflict"),
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
        # Denials and hedges: the headline says the attack is not happening.
        ("Trump says more US attacks on Iran 'highly unlikely'", "bbc_world"),
        ("Israel rules out strikes on Iran this year", "cnbc_top"),
        ("China rules out invasion of Taiwan", "bbc_world"),
        ("Trump says more US attacks on Iran not likely", "bbc_world"),
        ("US strikes on Iran not expected this week, officials say", "cnbc_top"),
        ("Iran won\u2019t attack Israel, foreign minister says", "cnbc_top"),
        ("Israel has no plans for an invasion of Iran", "bbc_world"),
        ("Iran says no attack on Israel is planned", "cnbc_top"),
        ("Iran denies attacks on Israel", "bbc_world"),
        ("China denies invasion of Taiwan", "bbc_world"),
        ("An invasion of Taiwan is unlikely to happen", "bbc_world"),
        ("Trump says more US attacks on Iran highly unlikely to happen", "bbc_world"),
        ("US attack on Iran unlikely to happen", "bbc_world"),
        ("US will not launch an attack on Iran", "bbc_world"),
        ("No attack on Iran expected this week", "bbc_world"),
        ("No attack on Iran planned this week", "bbc_world"),
        # Routine coverage and untrusted sources.
        ("What new tariffs would mean for your shopping?", "cnbc_top"),
        ("Fed officials see emergency of AI investment", "cnbc_top"),  # no rate action
        ("Trump imposes 50% tariffs on European Union imports", "guardian_world"),
        ("Treasury imposes sanctions on cartel money launderers", "ofac_actions"),
    ],
)
def test_routine_or_untrusted_headlines_do_not_match(headline, source):
    assert B.match_rule(headline, source) is None


DENIALS = [
    "Trump says more US attacks on Iran 'highly unlikely'",
    "Trump says more US attacks on Iran not likely",
    "US attack on Iran unlikely to happen",
    "An invasion of Taiwan is unlikely to happen",
    "Iran denies attacks on Israel",
    "China denies invasion of Taiwan",
    "Israel rules out strikes on Iran this year",
    "No attack on Iran expected this week",
    "US will not launch an attack on Iran",
    "Israel will not attack Iran",
    "Iran won't attack Israel",
    "Iran won\u2019t strike Israel",
]
BREAKS = [". ", "; ", ": ", " \u2014 ", " \u2013 ", " - ", ", ", " but ", ", but ",
          " while ", " as ", " after ", " and "]
ATTACKS = [f"{subject} {verb} {target}"
           for subject, target in [("US", "Iran"), ("Iran", "Israel"), ("Israel", "Iran"),
                                   ("China", "Taiwan"), ("Russia", "US")]
           for verb in ("strikes", "attacks", "bombs", "invades")]


@pytest.mark.parametrize("denial", DENIALS)
def test_a_denial_alone_is_quiet(denial):
    assert B.match_rule(denial, "bbc_world") is None


def test_a_denial_never_hides_an_attack_in_another_clause():
    missed = [
        headline
        for denial in DENIALS for brk in BREAKS for attack in ATTACKS
        for headline in (f"{denial}{brk}{attack}", f"{attack}{brk}{denial}")
        if getattr(B.match_rule(headline, "bbc_world"), "key", None) != "conflict"
    ]
    assert missed == []


def test_long_whitespace_runs_stay_fast():
    import time

    gap = " " * 50_000
    headlines = [f"Iran denies{gap}attacks on{gap}Israel{gap}. US attacks Iran",
                 f"US attacks on Iran{gap}", f"{gap}US will not{gap}attack Iran{gap}unlikely{gap}"]
    start = time.perf_counter()
    for headline in headlines:
        B.match_rule(headline, "bbc_world")
    assert time.perf_counter() - start < 1.0
    assert B.match_rule(headlines[0], "bbc_world").key == "conflict"


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
