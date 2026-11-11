"""desk:v12: reports are weighed by when they were published against the move, a
"likely" headline must hedge, and ask notes keep a change today apart from a
same-window figure. Nothing changes for desk:v9, v10 or v11."""

import json
import logging
import random
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from core.config.settings import Settings
from core.desk import event as EVT
from core.desk import evidence as EV
from core.desk import identity as ID
from core.desk import prompts as P
from core.desk.models import Move, NoteDraft, NoteKind, WhyRead
from core.desk.service import DeskService
from core.desk.service import et as service_et
from core.desk.writer import HEDGE_INSTRUCTION, DeskWriter, check_draft
from core.domain.sessions import active_session
from tests.desk.test_service import FakeWriter, make

START = datetime(2026, 11, 10, 13, 25, tzinfo=UTC)  # Tue Nov 10, 08:25 ET
END = datetime(2026, 11, 10, 14, 30, tzinfo=UTC)
NOW = datetime(2026, 11, 10, 14, 40, tzinfo=UTC)
WINDOW = EV.Window(START, END)
TEXT = "Dow falls as inflation data lifts Fed rate worries"


def et(moment):
    return moment.strftime("%H:%M")


def cand(headline=TEXT, *, at, source="Reuters", approximate=False):
    return EV.Candidate(source=source, headline=headline, published_at=at, url=None,
                        time_approximate=approximate)


# ------------------------------------------------------------------ timing classes


@pytest.mark.parametrize(("published", "expected"), [
    (START + timedelta(minutes=10), EV.Timing.DURING),
    (END + timedelta(minutes=5), EV.Timing.DURING),  # up to now
    (START - EV.DURING_LEAD, EV.Timing.DURING),  # the boundary is inclusive
    (START - EV.DURING_LEAD - timedelta(seconds=1), EV.Timing.SHORTLY_BEFORE),
    (START - EV.SHORTLY_BEFORE, EV.Timing.SHORTLY_BEFORE),
    (START - EV.SHORTLY_BEFORE - timedelta(seconds=1), EV.Timing.EARLIER),
    (START - timedelta(hours=20), EV.Timing.EARLIER),
])
def test_timing_classes_and_their_boundaries(published, expected):
    assert EV.timing_of(cand(at=published), WINDOW) is expected


def test_an_approximate_time_is_unknown_wherever_it_falls():
    for at in (START + timedelta(minutes=5), START - timedelta(hours=9)):
        assert EV.timing_of(cand(at=at, approximate=True), WINDOW) is EV.Timing.UNKNOWN


def test_the_constants_are_named():
    assert EV.DURING_LEAD == timedelta(minutes=30) and EV.SHORTLY_BEFORE == timedelta(hours=3)


def test_timing_in_words():
    def say(at, **kw):
        return EV.timing_text(cand(at=at, **kw), WINDOW)
    assert say(START + timedelta(minutes=5)) == "during the move"
    assert say(START - timedelta(minutes=20)) == \
        "during the move (about 20 minutes before it began)"
    assert say(END + timedelta(minutes=5)) == "after the move ended"
    assert say(START - timedelta(minutes=90)) == "about 2 hours before the move began"
    assert say(START - timedelta(hours=4, minutes=19)) == "about 4 hours before the move began"
    assert say(START - timedelta(hours=26)) == "about a day before the move began"
    assert say(START - timedelta(hours=3), approximate=True) == \
        "time not known: may be an older story"


# ------------------------------------------------------------------ ranking


def test_during_beats_shortly_before_beats_earlier_all_else_equal():
    scores = [EV.score(cand("Gold rises as dollar eases", at=at), "GOLD", 1, NOW, WINDOW)
              for at in (START + timedelta(minutes=5), START - timedelta(hours=1),
                         START - timedelta(hours=8))]
    assert scores[0] > scores[1] > scores[2]


def test_an_older_report_that_says_this_market_moved_cannot_outrank_a_during_one():
    old = cand("Dow falls as CPI lifts Fed rate worries", at=START - timedelta(hours=8))
    fresh = cand("Dow slides after hot inflation print stokes rate-hike fears",
                 at=START + timedelta(minutes=5))
    unknown = cand("Dow drops on Treasury yield jump after Fed comments",
                   at=START + timedelta(minutes=6), approximate=True)
    ranked = EV.rank([old, unknown, fresh], "YM", direction=-1, now=NOW, window=WINDOW)
    assert ranked[0] is fresh and set(ranked) == {fresh, old, unknown}


def test_the_penalty_orders_but_never_drops():
    # Content alone is 6 (named + moved): below the floor if the penalty counted.
    only = cand("Dow falls", at=START - timedelta(hours=8), source="Obscure Daily")
    assert EV.rank([only], "YM", direction=-1, now=NOW, window=WINDOW) == [only]


def test_a_report_after_now_is_still_excluded_and_a_day_old_one_too():
    future = cand(at=NOW + timedelta(seconds=1))
    old = cand(at=NOW - EV.LOOKBACK - timedelta(seconds=1))
    assert EV.rank([future, old], "YM", direction=-1, now=NOW, window=WINDOW) == []


def test_ranking_does_not_depend_on_input_order_with_a_window():
    items = [cand(f"Dow falls as report {n} lifts Fed rate worries",
                  at=START + timedelta(minutes=n * 37 - 200), source=f"S{n % 4}")
             for n in range(14)]
    want = EV.rank(items, "YM", direction=-1, now=NOW, window=WINDOW)
    for seed in range(5):
        shuffled = items[:]
        random.Random(seed).shuffle(shuffled)
        assert EV.rank(shuffled, "YM", direction=-1, now=NOW, window=WINDOW) == want


def test_without_a_window_nothing_changed():
    item = cand("Dow falls as inflation data lifts Fed rate worries",
                at=NOW - timedelta(minutes=30))
    assert EV.score(item, "YM", -1, NOW) == EV.score(item, "YM", -1, NOW, None)
    # the old flat credit for the last 3 hours
    older = cand(item.headline, at=NOW - timedelta(hours=5))
    assert EV.score(item, "YM", -1, NOW) == EV.score(older, "YM", -1, NOW) + 1


def test_rank_event_takes_the_window():
    during = cand("Dow slides as inflation data lifts Fed rate worries",
                  at=START + timedelta(minutes=5))
    old = cand("Gold falls as dollar firms", at=START - timedelta(hours=8))
    got = EV.rank_event([old, during], [("YM", -1), ("GOLD", -1)], now=NOW, window=WINDOW)
    assert got[0] is during and old in got


# ------------------------------------------------------------------ the real examples


GOLD_START = datetime(2026, 11, 8, 23, 39, tzinfo=UTC)  # Sun Nov 08, 18:39 ET
GOLD_END = datetime(2026, 11, 9, 0, 44, tzinfo=UTC)
GOLD_NOW = datetime(2026, 11, 9, 0, 54, tzinfo=UTC)
GOLD_WINDOW = EV.Window(GOLD_START, GOLD_END)
WSJ = cand("Gold Climbs on Softer Dollar, Falling Treasury Yields After U.S. Midterms",
           at=datetime(2026, 11, 8, 22, 3, tzinfo=UTC), source="WSJ", approximate=True)
LIVE = cand("Gold jumps as dollar slides, yields fall", at=GOLD_START + timedelta(minutes=30),
            source="Kitco")


def test_example_a_a_stale_story_ranks_below_a_report_of_the_move():
    ranked = EV.rank([WSJ, LIVE], "GOLD", direction=1, now=GOLD_NOW, window=GOLD_WINDOW)
    assert ranked == [LIVE, WSJ]


async def facts_for(items, window, now, symbol):
    return await EV.evidence_facts(items, symbol=symbol, unavailable=[], article_text=None,
                                   when=et, now=now, window=window)


async def test_example_a_alone_it_is_labelled_so_the_writer_cannot_call_it_fresh():
    chosen = EV.rank([WSJ], "GOLD", direction=1, now=GOLD_NOW, window=GOLD_WINDOW)
    assert chosen == [WSJ]
    fact = await facts_for(chosen, GOLD_WINDOW, GOLD_NOW, "GOLD")
    item = fact["items"][0]
    assert item["timing"] == "time not known: may be an older story"
    assert item["time_is_approximate"] is True and item["time"] == "22:03"
    assert (fact["move_from"], fact["move_to"]) == ("23:39", "00:44")


async def test_example_b_a_live_page_with_an_old_stamp_survives_labelled_as_before_the_move():
    page = cand("Stock market today: Dow, S&P 500, Nasdaq sink after CPI data raises Fed "
                "rate-hike bets", at=datetime(2026, 11, 10, 9, 6, tzinfo=UTC),
                source="Yahoo Finance")
    monday = cand("US stocks fall after worse-than-expected report on inflation",
                  at=datetime(2026, 11, 10, 3, 44, tzinfo=UTC), source="Yahoo Finance")
    release = cand("Stock Market Today: Dow Falls On Surprise Inflation Data; AI Giant "
                   "Anthropic Sets IPO Range (Live Coverage)",
                   at=datetime(2026, 11, 10, 14, 5, tzinfo=UTC), source="Yahoo Finance")
    chosen = EV.rank([monday, page, release], "YM", direction=-1, now=NOW, window=WINDOW)
    assert chosen[0] is release and set(chosen) == {release, page, monday}
    timings = {i["headline"].split(":")[0]: i["timing"]
               for i in (await facts_for(chosen, WINDOW, NOW, "YM"))["items"]}
    assert timings["Stock Market Today"] == "during the move"
    assert timings["Stock market today"] == "about 4 hours before the move began"
    assert timings["US stocks fall after worse-than-expected report on inflation"] == \
        "about 10 hours before the move began"


async def test_evidence_without_a_window_has_no_timing_fields():
    fact = await facts_for([], None, NOW, "YM")
    assert "move_from" not in fact and "timing" not in json.dumps(
        await facts_for([LIVE], None, NOW, "GOLD"))


# ------------------------------------------------------------------ the service


class VWriter(FakeWriter):
    def __init__(self, version):
        super().__init__()
        self.prompt_version = version

    async def write(self, kind, facts, *, instrument):
        self.calls.append((kind, facts))
        return SimpleNamespace(
            draft=NoteDraft(headline="Headline", summary="Summary.", what_it_means="Means.",
                            market_reaction="Reaction.", rates_read="Yields.", watch_next=[],
                            learn="Lesson.", nq_tone="negative"),
            model="fake", cost_usd=0.001, error=None, unverified_numbers=(), prompt=None)


SNOW = datetime(2026, 11, 5, 15, 16, tzinfo=UTC)
ES = ID.Instrument.of("ES")


def stored(headline, ago):
    return SimpleNamespace(id=headline, source="cnbc_tech", headline=headline, summary=None,
                           source_url="https://example.com/x", received_at=SNOW - ago,
                           published_at=SNOW - ago)


def desk(version, instruments=("NQ", "WTI")):
    base, _, _ = make(SNOW, news=[stored("Nasdaq falls as chip stocks tumble",
                                         timedelta(minutes=50)),
                                  stored("Qualcomm slides, leading Nasdaq lower after Fed comments",
                                         timedelta(hours=9))])
    sources = SimpleNamespace(**{**vars(base.sources), "external_reports": None,
                                 "article_text": None})
    return DeskService(sources=sources, store=base.store, writer=VWriter(version),
                       clock=lambda: SNOW, instruments=instruments)


def nq_move():
    return Move(symbol="NQ", label="Nasdaq 100 futures", start_value=28000.0, end_value=27400.0,
                start_at=SNOW - timedelta(hours=1), end_at=SNOW - timedelta(minutes=10),
                change=-2.14, change_unit="%", delayed=True, text="-2.14%")


async def test_a_v12_move_note_is_matched_against_the_moves_own_window():
    service = desk(P.V12)
    await service.build_move("move:x", nq_move(), SNOW)
    fact = service.writer.calls[0][1]["why_evidence"]
    assert (fact["move_from"], fact["move_to"]) == (
        service_et(SNOW - timedelta(hours=1)), service_et(SNOW - timedelta(minutes=10)))
    by = {i["headline"]: i["timing"] for i in fact["items"]}
    # published 50 minutes before now: after the move's measured end, still during
    assert by["Nasdaq falls as chip stocks tumble"] == "during the move"
    older = by["Qualcomm slides, leading Nasdaq lower after Fed comments"]
    assert older.endswith("before the move began")


@pytest.mark.parametrize("version", [P.V10, P.V11])
async def test_earlier_versions_facts_are_unchanged(version):
    service = desk(version)
    await service.build_move("move:x", nq_move(), SNOW)
    fact = service.writer.calls[0][1]["why_evidence"]
    assert set(fact) == {"searched_from", "searched_to", "items", "feeds_unavailable"}
    assert all("timing" not in i for i in fact["items"])


async def test_a_v12_ask_note_uses_the_session_so_far_as_its_window():
    service = desk(P.V12)
    await service.ask(ID.NQ)
    fact = service.writer.calls[0][1]["why_evidence"]
    assert fact["move_to"] and fact["move_from"] != fact["move_to"]
    assert fact["move_from"] == service_et(active_session(SNOW).opens_at)


async def test_a_v12_event_note_uses_the_leads_window():
    service = desk(P.V12, instruments=("NQ", "ES"))
    lead = EVT.Mover(ID.Instrument.of("NQ"), nq_move(), 0.8, 0)
    other_move = nq_move().model_copy(update={"symbol": "ES", "label": "S&P 500 futures"})
    group = EVT.group_moves([lead, EVT.Mover(ID.Instrument.of("ES"), other_move, 0.8, 1)],
                            events=True)[0]
    await service.build_move("move:x", nq_move(), SNOW, ID.NQ, event=group)
    fact = service.writer.calls[0][1]["why_evidence"]
    assert fact["move_from"] == service_et(SNOW - timedelta(hours=1))
    assert fact["move_to"] == service_et(SNOW - timedelta(minutes=10))


# ------------------------------------------------------------------ prompts


def test_v12_is_selectable_layered_on_v11_and_not_the_default():
    assert P.PROMPT_VERSION == P.V9
    assert Settings(_env_file=None, desk_prompt_version=P.V12).desk_prompt_version == P.V12
    assert P.V12 in P.WHY_VERSIONS and P.V12 in P.EVENT_VERSIONS
    assert P.FRESH_VERSIONS == {P.V12, P.V13} and P.HEDGE_VERSIONS == {P.V12, P.V13}
    assert set(P.PROMPTS[P.V12]) == set(P.PROMPTS[P.V11])


def test_earlier_versions_are_byte_for_byte_unchanged():
    # The v11 profile is built from the v10 text with only the event task added.
    for symbol in P.SPECS:
        v10, v11, v12 = (P.PROMPTS[v][symbol] for v in (P.V10, P.V11, P.V12))
        assert (v11.system, v11.tasks, v11.stated_facts) == (v10.system, v10.tasks,
                                                            v10.stated_facts)
        assert not v10.hedge_likely and not v11.hedge_likely and v12.hedge_likely
        assert v12.system != v11.system and v12.tasks[NoteKind.MOVE] != v11.tasks[NoteKind.MOVE]
        assert "timing" not in v11.system and "timing" in v12.system
        assert "{" not in v12.tasks[NoteKind.ASK] and v12.event_task and "{" not in v12.event_task


@pytest.mark.parametrize("symbol", sorted(P.SPECS))
def test_the_v12_rules_about_timing(symbol):
    profile = P.PROMPTS[P.V12][symbol]
    system = profile.system
    assert 'unless its timing is "during the move"' in system
    assert '"time not known"' in system and 'at most "likely"' in system
    for kind in (NoteKind.MOVE, NoteKind.ASK):
        task = profile.tasks.get(kind)
        if task is None:
            continue
        assert ('an item whose timing is "during the move" or "after the move ended" '
                'itself says') in task
        assert "the text must say the report predates the move" in task
    assert "predates the move" in profile.event_task


def test_the_ask_task_names_the_same_window_key_and_calls_a_change_today_that():
    task = P.PROMPTS[P.V12]["NQ"].tasks[NoteKind.ASK]
    assert "last_two_hours" in task and "never call a change_today the change over the same" in task
    assert "last_two_hours" not in P.PROMPTS[P.V11]["NQ"].tasks[NoteKind.ASK]


def test_the_evidence_fact_is_shared_by_v12_and_the_ask_facts_keep_their_keys():
    facts = {"why_evidence": {}}
    assert "why_evidence" in P.facts_at(P.V12, facts)


# ------------------------------------------------------------------ the hedge


@pytest.mark.parametrize(("headline", "ok"), [
    ("Tech-led selling drags ES lower", False),
    ("Tech-led selling likely drags ES lower", True),
    ("Gold appears to rise on lower yields", True),
    ("Gold may rise", True),
    ("Gold could rise", True),
    ("Gold seems firmer", True),
    ("Gold possibly firmer, probably on yields", True),
    ("Desk’s read: yields lift gold", True),
    ("Maybe gold rises", False),
    ("Gold mayhem", False),
    ("Gold rises on May inflation surprise", False),  # the month is not a hedge
])
def test_what_counts_as_a_hedge(headline, ok):
    assert P.hedged(headline) is ok


def draft_with(headline, level="likely"):
    why = WhyRead(level=level, text="Desk's read: yields.", evidence=[1])
    return NoteDraft(headline=headline, summary="Summary.", what_it_means="Means.",
                     market_reaction="Reaction.", rates_read="Yields.", watch_next=[],
                     learn="Lesson.", nq_tone="negative", why=why)


def test_only_a_likely_why_at_a_hedging_version_needs_a_hedge():
    kw = {"why_required": True, "evidence_ids": {1}}
    bare = draft_with("ES slides on tech selling")
    assert check_draft(bare, **kw, hedge_likely=True)
    assert check_draft(bare, **kw) is None  # earlier versions
    assert check_draft(draft_with("ES slides on tech selling", "reported"), **kw,
                       hedge_likely=True) is None
    assert check_draft(draft_with("ES likely slides on tech selling"), **kw,
                       hedge_likely=True) is None


def test_the_prompt_asks_for_the_hedge_only_where_the_version_does():
    facts = {"why_evidence": {"items": [{"id": 1}]}}
    assert P.render(NoteKind.MOVE, ID.NQ, facts, version=P.V12).hedge_likely
    assert not P.render(NoteKind.MOVE, ID.NQ, facts, version=P.V11).hedge_likely
    assert not P.render(NoteKind.PREMARKET, ID.NQ, {}, version=P.V12).hedge_likely


def reply(headline, level="likely"):
    why = {"level": level, "text": "Desk's read: yields.", "evidence": [1]}
    fields = {"headline": headline, "summary": "Summary.", "what_it_means": "Means.",
              "market_reaction": "Reaction.", "rates_read": "Yields.", "watch_next": [],
              "learn": "Lesson.", "tone": "negative", "why": why}
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(fields)}}],
                                     "usage": {"prompt_tokens": 10, "completion_tokens": 10}})


def writer_replying(*replies):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return replies[min(len(bodies) - 1, len(replies) - 1)]
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return DeskWriter(api_key="k", model="m", prompt_version=P.V12, client=client), bodies


FACTS = {"why_evidence": {"items": [{"id": 1}]}}


async def test_an_unhedged_likely_headline_is_regenerated_once_with_an_instruction():
    writer, bodies = writer_replying(reply("ES slides on tech selling"),
                                     reply("ES likely slides on tech selling"))
    result = await writer.write(NoteKind.MOVE, FACTS, instrument=ES)
    assert len(bodies) == 2
    assert result.draft.headline == "ES likely slides on tech selling" and result.error is None
    assert HEDGE_INSTRUCTION not in bodies[0]["messages"][1]["content"]
    assert HEDGE_INSTRUCTION in bodies[1]["messages"][1]["content"]
    assert bodies[1]["messages"][0] == bodies[0]["messages"][0]


async def test_a_headline_that_still_does_not_hedge_keeps_the_note_and_warns(caplog):
    writer, bodies = writer_replying(reply("ES slides on tech selling"))
    with caplog.at_level(logging.WARNING, logger="core.desk.writer"):
        result = await writer.write(NoteKind.MOVE, FACTS, instrument=ES)
    assert len(bodies) == 2  # never a second paid retry
    assert result.draft is not None and result.error is None
    assert result.draft.headline == "ES slides on tech selling" and not result.needs_hedge
    assert "unhedged headline" in caplog.text


async def test_a_hedge_rewrite_that_invents_a_number_is_not_taken():
    writer, bodies = writer_replying(reply("ES slides on tech selling"),
                                     reply("ES likely slides 7% on tech selling"))
    result = await writer.write(NoteKind.MOVE, FACTS, instrument=ES)
    assert len(bodies) == 2  # the clean first note needs no number rewrite
    assert result.draft.headline == "ES slides on tech selling"
    assert result.unverified_numbers == () and not result.needs_hedge


async def test_a_hedge_rewrite_that_swaps_one_invented_number_for_another_is_not_taken():
    writer, bodies = writer_replying(reply("ES slides 737% on tech selling"),
                                     reply("ES likely slides 919% on tech selling"),
                                     reply("ES slides 737% on tech selling"))
    result = await writer.write(NoteKind.MOVE, FACTS, instrument=ES)
    assert len(bodies) == 3  # first note, hedge rewrite, one number rewrite
    assert "919" not in result.draft.headline
    assert result.unverified_numbers == ("737",)


async def test_a_failed_regeneration_keeps_the_first_note():
    writer, bodies = writer_replying(reply("ES slides on tech selling"),
                                     httpx.Response(500))
    result = await writer.write(NoteKind.MOVE, FACTS, instrument=ES)
    assert len(bodies) == 2 and result.draft.headline == "ES slides on tech selling"


async def test_a_hedged_or_reported_note_is_not_regenerated():
    for r in (reply("ES likely slides"), reply("ES slides on tech selling", "reported")):
        writer, bodies = writer_replying(r)
        result = await writer.write(NoteKind.MOVE, FACTS, instrument=ES)
        assert len(bodies) == 1 and result.draft is not None


async def test_the_hedge_shares_the_single_why_regeneration():
    # A bad citation uses the one regeneration; its answer is kept unhedged, not retried again.
    bad = reply("ES slides on tech selling")
    bad_body = json.loads(bad.content)
    content = json.loads(bad_body["choices"][0]["message"]["content"])
    content["why"]["evidence"] = [9]
    bad = httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content)}}],
                                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    writer, bodies = writer_replying(bad, reply("ES slides on tech selling"))
    result = await writer.write(NoteKind.MOVE, FACTS, instrument=ES)
    assert len(bodies) == 2 and result.draft.why.evidence == [1]


async def test_earlier_versions_never_ask_for_a_hedge():
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: reply("ES slides on tech selling")))
    writer = DeskWriter(api_key="k", model="m", prompt_version=P.V11, client=client)
    result = await writer.write(NoteKind.MOVE, FACTS, instrument=ES)
    assert result.draft is not None and result.cost_usd is not None
