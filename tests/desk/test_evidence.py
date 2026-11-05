"""Choosing why_evidence: fixed rules, every instrument, no AI."""

import random
from datetime import UTC, datetime, timedelta

import pytest

from core.desk import evidence as EV
from core.desk.models import NoteDraft, WhyRead
from core.desk.prompts import SPECS
from core.providers.news.evidence import COVERAGE

NOW = datetime(2026, 11, 5, 15, 16, tzinfo=UTC)


def report(headline, *, ago=timedelta(minutes=30), source="Reuters", url=None, summary=None):
    return EV.Candidate(source=source, headline=headline, published_at=NOW - ago, url=url,
                        summary=summary)


def stamp(moment):
    return moment.strftime("%H:%M")


def test_every_instrument_has_rules_and_coverage():
    assert set(SPECS) == set(EV.TERMS) == set(COVERAGE)


@pytest.mark.parametrize(("symbol", "direction", "right", "wrong"), [
    ("NQ", -1, "Nasdaq falls at open as chip, data center stocks fall", "Yemen's president sacks"),
    ("ES", -1, "Wall Street slides after midterm rally fades", "Refineries shift production"),
    ("YM", 1, "Dow jumps 500 points after election result", "Australian voters and AI"),
    ("GOLD", 1, "Gold climbs as falling yields support bullion", "Nvidia earnings beat"),
    ("WTI", -1, "Oil drops as Iran talks ease supply fears", "Nasdaq rallies on chips"),
    ("DXY", 1, "Dollar index rises after hawkish Fed comments", "Gas prices in Europe"),
    ("US2Y", -1, "Treasury yields fall after soft jobs report", "Tesla factory opens"),
    ("US10Y", 1, "10-year yield jumps on weak auction demand", "Tesla factory opens"),
    ("VIX", 1, "Stocks slide on Wall Street; fear gauge jumps", "Olive oil company takeover"),
])
def test_keeps_reports_about_the_market_and_drops_the_rest(symbol, direction, right, wrong):
    chosen = EV.rank([report(right), report(wrong, source="BBC")], symbol,
                     direction=direction, now=NOW)
    assert [c.headline for c in chosen] == [right]


def test_a_report_saying_why_outranks_one_that_only_names_the_market():
    named = report("Nasdaq companies to hold annual meetings")
    why = report("Nasdaq drops as chip stocks tumble after Qualcomm forecast")
    chosen = EV.rank([named, why], "NQ", direction=-1, now=NOW)
    assert chosen[0] is why


def test_the_opposite_direction_ranks_below_the_same_direction():
    up = report("Nasdaq jumps on chip rally", source="CNBC")
    down = report("Nasdaq drops on chip selloff", source="WSJ")
    assert EV.score(down, "NQ", -1, NOW) > EV.score(up, "NQ", -1, NOW)
    # VIX usually moves against stocks: "stocks fall" isn't a contradiction of a rising VIX.
    stocks = report("Stocks fall on Wall Street as tech slides")
    vix = report("VIX falls as fear gauge eases")
    assert EV.score(stocks, "VIX", 1, NOW) > EV.score(vix, "VIX", 1, NOW)


def test_only_reports_from_the_last_day_and_never_after_the_note():
    text = "Nasdaq drops as chip stocks tumble"
    old = report(text, ago=EV.LOOKBACK + timedelta(minutes=1))
    future = report(text + " again", ago=-timedelta(minutes=1))
    edge = report(text + " today", ago=EV.LOOKBACK)
    assert EV.rank([old, future, edge], "NQ", direction=-1, now=NOW) == [edge]


def test_how_to_and_price_pages_are_not_news():
    for title in ("Gold prices today, Thursday November 5", "Should you buy gold after the rally?",
                  "Top 5 gold stocks to buy as bullion jumps"):
        assert EV.rank([report(title)], "GOLD", direction=1, now=NOW) == []


def test_near_repeats_count_once_and_one_source_cannot_fill_the_list():
    repeats = [report("Nasdaq falls at open as chip stocks fall", source="Reuters"),
               report("Nasdaq falls at open as chip stocks fall sharply", source="MSN")]
    assert len(EV.rank(repeats, "NQ", direction=-1, now=NOW)) == 1
    same = [report(f"Nasdaq falls as {name} tumbles", source="Reuters")
            for name in ("Nvidia", "Tesla", "Broadcom", "Arm", "Micron")]
    assert len(EV.rank(same, "NQ", direction=-1, now=NOW)) == EV.MAX_PER_SOURCE


def test_a_known_publisher_ranks_ahead_of_an_unknown_site():
    known = report("Nasdaq drops as chip stocks tumble", source="Reuters")
    unknown = report("Nasdaq tumbles as semiconductors drop hard", source="Rich Picks Daily")
    assert EV.rank([unknown, known], "NQ", direction=-1, now=NOW)[0] is known


def test_order_never_depends_on_input_order():
    items = [report(f"Nasdaq falls as {name} sinks", source=f"Site {i}")
             for i, name in enumerate(("Nvidia", "Tesla", "Arm", "AMD", "Intel", "Micron"))]
    first = EV.rank(items, "NQ", direction=-1, now=NOW)
    shuffled = items[:]
    random.Random(7).shuffle(shuffled)
    assert EV.rank(shuffled, "NQ", direction=-1, now=NOW) == first


def test_excerpt_keeps_relevant_sentences_without_page_furniture():
    page = ("Oops, something went wrong TSLA -9.50% ^IXIC -4.42% "
            "Subscribe to our newsletter for the latest market news today. "
            "The company opened a new office in Dublin last spring. "
            "Chip stocks fell after Qualcomm cut its forecast on Wednesday. "
            "Nvidia dropped alongside them as investors sold the AI trade.")
    text = EV.excerpt(page, "NQ")
    assert text == ("Chip stocks fell after Qualcomm cut its forecast on Wednesday. "
                    "Nvidia dropped alongside them as investors sold the AI trade.")
    assert EV.excerpt(None, "NQ") is None
    assert EV.excerpt("Nothing here about anything at all, just a long sentence.", "NQ") is None


def test_excerpt_stays_within_its_limit():
    sentence = "Chip stocks fell after Qualcomm cut its forecast on Wednesday."
    assert len(EV.excerpt(" ".join([sentence] * 40), "NQ", limit=200)) <= 200


async def test_facts_number_reports_and_read_only_the_strongest_readable_pages():
    chosen = [report("Nasdaq drops as chips tumble", url="https://news.google.com/a"),
              report("Stocks slide after Qualcomm", url="https://ok/1", summary="Chips fell."),
              report("Tech selloff deepens", url="https://ok/2"),
              report("Chip rout spreads", url="https://ok/3"),
              report("Wall Street falls", url="https://ok/4")]
    read = []

    async def article_text(url):
        read.append(url)
        if url.endswith("/2"):
            raise RuntimeError("boom")
        return "Chip stocks fell after Qualcomm cut its forecast on Wednesday."

    fact = await EV.evidence_facts(chosen, symbol="NQ", unavailable=["Google News"],
                                   article_text=article_text,
                                   readable=lambda url: url.startswith("https://ok"),
                                   when=stamp, now=NOW)
    assert sorted(read) == ["https://ok/1", "https://ok/2", "https://ok/3"]
    assert [i["id"] for i in fact["items"]] == [1, 2, 3, 4, 5]
    assert "excerpt" not in fact["items"][0]  # headline-only: its page isn't readable
    assert fact["items"][1]["summary"] == "Chips fell."
    assert fact["items"][1]["excerpt"].startswith("Chip stocks fell")
    assert "excerpt" not in fact["items"][2]  # a failed page costs only its passage
    assert fact["feeds_unavailable"] == ["Google News"]
    assert fact["searched_to"] == "15:16" and fact["searched_from"] == "15:16"


async def test_facts_with_no_reports_say_so():
    fact = await EV.evidence_facts([], symbol="GOLD", unavailable=[], article_text=None,
                                   when=stamp, now=NOW)
    assert fact["items"] == []


def note(why):
    return NoteDraft(headline="Headline", summary="Summary", what_it_means="Means",
                     market_reaction="Reaction", watch_next=[], learn="Learn",
                     nq_tone="negative", why=why)


def test_cited_resolves_ids_to_the_reports_supplied():
    chosen = [report("Nasdaq drops", url="https://a"), report("Chips tumble", source="WSJ")]
    found = EV.cited(note(WhyRead(level="reported", text="Reuters says so.",
                                  evidence=[2, 1, 2, 9])), chosen)
    assert [h.title for h in found] == ["Chips tumble", "Nasdaq drops"]
    assert found[0].source == "WSJ" and found[1].url == "https://a"
    assert EV.cited(note(None), chosen) == [] and EV.cited(None, chosen) == []
