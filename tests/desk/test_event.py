"""One note per event: instruments passing their move trigger in the same pass are
written once (desk:v11); older versions write one note each, as ever."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.api import desk as desk_api
from core.desk import event as EVT
from core.desk import prompts as P
from core.desk.identity import Instrument
from core.desk.models import DeskNote, Move, NoteDraft, NoteKind
from core.desk.writer import WriteResult, invented_numbers
from tests.desk.test_identity import MOVE_AT, falling
from tests.desk.test_service import FakeWriter, draft, make

ALL = ("NQ", "ES", "YM", "GOLD", "WTI", "DXY", "US2Y", "US10Y", "VIX")


class V11Writer(FakeWriter):
    prompt_version = P.V11


class V10Writer(FakeWriter):
    prompt_version = P.V10


def inst(symbol):
    return Instrument.of(symbol)


def move(symbol, change, unit="%", at=MOVE_AT):
    return Move(symbol=symbol, label=symbol, start_value=1.0, end_value=1.0,
                start_at=at - timedelta(minutes=60), end_at=at, change=change,
                change_unit=unit, text=f"{change:+.2f}{unit}")


def mover(symbol, change, trigger, order, unit="%"):
    return EVT.Mover(inst(symbol), move(symbol, change, unit), trigger, order)


def yields(start, step_bp, now=MOVE_AT):
    return [SimpleNamespace(timestamp=now - timedelta(minutes=80) + timedelta(minutes=i),
                            value=start + step_bp * i / 100) for i in range(80)]


def flat(value, now=MOVE_AT):
    return falling(value, now, step=0.0)


def desk(series, writer=None, now=MOVE_AT, instruments=ALL):
    return make(now, series=series, writer=writer or V11Writer(), instruments=instruments)


# ------------------------------------------------------------------ pure grouping


def test_two_or_more_movers_are_one_event_and_one_alone_is_not():
    alone = EVT.group_moves([mover("ES", -1.0, 0.8, 1)], events=True)
    assert [g.is_event for g in alone] == [False]
    pair = EVT.group_moves([mover("ES", -1.0, 0.8, 1), mover("GOLD", 1.0, 0.8, 3)], events=True)
    assert len(pair) == 1 and pair[0].is_event
    assert EVT.group_moves([], events=True) == []


def test_without_events_every_mover_is_its_own_group_in_desk_order():
    groups = EVT.group_moves([mover("GOLD", 1.0, 0.8, 3), mover("ES", -1.0, 0.8, 1)],
                             events=False)
    assert [g.lead.instrument.symbol for g in groups] == ["ES", "GOLD"]
    assert not any(g.is_event for g in groups)


def test_nq_leads_whenever_it_is_in_the_group():
    [group] = EVT.group_moves([mover("GOLD", 5.0, 0.8, 3), mover("NQ", -0.9, 0.8, 0),
                               mover("ES", -4.0, 0.8, 1)], events=True)
    assert group.lead.instrument.symbol == "NQ"
    assert [m.instrument.symbol for m in group.members] == ["NQ", "GOLD", "ES"]


def test_without_nq_the_largest_move_against_its_own_trigger_leads():
    # GOLD moved 1.5x its trigger, WTI 2.0x: WTI leads although GOLD's move is larger.
    [group] = EVT.group_moves([mover("GOLD", 1.2, 0.8, 3), mover("WTI", 2.0, 1.0, 4),
                               mover("ES", -0.9, 0.8, 1)], events=True)
    assert group.lead.instrument.symbol == "WTI"
    assert [m.instrument.symbol for m in group.members] == ["WTI", "GOLD", "ES"]


def test_a_tie_goes_to_the_earlier_desk_instrument():
    [group] = EVT.group_moves([mover("GOLD", -1.6, 0.8, 3), mover("ES", 1.6, 0.8, 1)],
                              events=True)
    assert [m.instrument.symbol for m in group.members] == ["ES", "GOLD"]


def test_a_yield_leads_by_basis_points_against_its_own_trigger():
    [group] = EVT.group_moves([mover("US10Y", 8.0, 4.0, 7, "bp"), mover("ES", 1.0, 0.8, 1)],
                              events=True)
    assert group.lead.instrument.symbol == "US10Y"  # 2.0x against 1.25x
    assert group.scope == EVT.CROSS_MARKET


def test_rates_only_pair_is_a_rates_event():
    [group] = EVT.group_moves([mover("US10Y", 6.0, 4.0, 7, "bp"),
                               mover("US2Y", 5.0, 4.0, 6, "bp")], events=True)
    assert group.scope == EVT.RATES and group.is_event
    assert EVT.event_title(group) == "US10Y and US2Y yields moved together in the last hour"
    assert group.lead.instrument.symbol == "US10Y"


def test_event_title_names_every_market():
    [group] = EVT.group_moves([mover("NQ", -1, 0.8, 0), mover("ES", -1, 0.8, 1),
                               mover("GOLD", 1, 0.8, 3)], events=True)
    assert EVT.event_title(group) == "NQ, ES and GOLD moved together in the last hour"


def test_event_facts_are_formatted_text_and_name_the_markets_that_stayed_put():
    [group] = EVT.group_moves([mover("NQ", -1.6, 0.8, 0), mover("US10Y", 6.0, 4.0, 7, "bp")],
                              events=True)
    desk_markets = [inst(s) for s in ("NQ", "ES", "US10Y")]
    facts = EVT.event_facts(group, desk_markets, when=lambda t: "T",
                            locations={"US10Y": {"us10y_location": {"levels": []}}})
    assert facts["scope"] == EVT.CROSS_MARKET and facts["lead_market"] == "Nasdaq 100 futures"
    nq, ten = facts["markets_that_moved"]
    assert nq["change"] == "-1.60%" and nq["times_its_trigger"] == "2.0"
    assert nq["its_trigger"] == "0.80% in one hour"
    assert ten["times_its_trigger"] == "1.5" and ten["its_trigger"] == "4.00bp in one hour"
    assert ten["us10y_location"] == {"levels": []} and "nq_price_location" not in nq
    assert facts["markets_that_did_not_pass_trigger"] == ["S&P 500 futures"]
    json.dumps(facts)


# ------------------------------------------------------------------ the service


async def test_two_instruments_in_one_pass_write_one_note_and_one_notification():
    service, _, sent = desk({"NQ": falling(30000.0), "ES": falling(6000.0, step=0.0003),
                             "GOLD": flat(4000.0)})
    notes = await service.tick()
    assert [n.id for n in notes] == ["move:20261020T10"]  # NQ leads; id shape unchanged
    [note] = notes
    assert note.kind is NoteKind.MOVE and note.instrument == "NQ"
    assert note.event_members == ["ES"]
    assert [i.symbol for i in note.event_instruments] == ["NQ", "ES"]
    assert note.title == "NQ and ES moved together in the last hour"
    assert [m.symbol for m in note.moves[:2]] == ["NQ", "ES"]
    assert len(sent) == 1
    facts, _ = service.store.facts(note.id)
    assert "the_move" not in facts
    event = facts["the_event"]
    assert [r["market"] for r in event["markets_that_moved"]] == [
        "Nasdaq 100 futures", "S&P 500 futures"]
    assert "US dollar" in event["markets_that_did_not_pass_trigger"]
    assert "Nasdaq 100 futures" not in event["markets_that_did_not_pass_trigger"]


async def test_a_lone_mover_still_gets_its_own_note_on_v11():
    service, _, sent = desk({"NQ": flat(30000.0), "ES": falling(6000.0)})
    [note] = await service.tick()
    assert note.id == "move:ES:20261020T10" and note.event_members == []
    assert note.event_instruments == [] and note.title.startswith("ES down")
    assert len(sent) == 1
    assert "the_event" not in service.store.facts(note.id)[0]


async def test_event_note_is_filed_under_the_lead_when_nq_is_absent():
    service, _, _ = desk({"NQ": flat(30000.0), "ES": falling(6000.0, step=0.0003),
                          "GOLD": falling(4000.0, step=0.0002)})
    [note] = await service.tick()
    assert note.id == "move:ES:20261020T10" and note.event_members == ["GOLD"]


async def test_rates_only_pair_writes_one_rates_note():
    service, _, _ = desk({"NQ": flat(30000.0), "US2Y": yields(4.0, -0.2),
                          "US10Y": yields(4.3, -0.25)})
    [note] = await service.tick()
    assert note.id == "move:US10Y:20261020T10" and note.event_members == ["US2Y"]
    assert note.title == "US10Y and US2Y yields moved together in the last hour"
    assert service.store.facts(note.id)[0]["the_event"]["scope"] == "rates"


async def test_the_event_counts_against_every_members_three_hours():
    series = {"NQ": falling(30000.0), "ES": falling(6000.0), "GOLD": flat(4000.0)}
    service, clock, _ = desk(series)
    [event] = await service.tick()
    assert event.event_members == ["ES"]
    # Forty minutes on, ES (a member, not the lead) moves alone: still inside its 3 hours.
    later = MOVE_AT + timedelta(minutes=40)
    series.update(NQ=flat(30000.0, later), ES=falling(6000.0, later), GOLD=flat(4000.0, later))
    clock["now"] = later
    assert await service.due_jobs(later) == []
    # GOLD was never in the event, so it still gets its own note.
    series["GOLD"] = falling(4000.0, later)
    [job] = await service.due_jobs(later)
    assert job[0] == "move:GOLD:20261020T11"
    # After three hours the members are free again.
    last = MOVE_AT + timedelta(hours=3, minutes=5)
    series.update(NQ=falling(30000.0, last), ES=falling(6000.0, last), GOLD=flat(4000.0, last))
    clock["now"] = last
    [again] = await service.tick()
    assert again.event_members == ["ES"] and again.id != event.id


async def test_a_late_joiner_does_not_reopen_or_rewrite_the_event():
    series = {"NQ": falling(30000.0), "ES": falling(6000.0), "GOLD": flat(4000.0)}
    service, clock, sent = desk(series)
    [event] = await service.tick()
    before = service.store.get(event.id).model_dump_json()
    later = MOVE_AT + timedelta(minutes=2)
    series.update(NQ=falling(30000.0, later), ES=falling(6000.0, later),
                  GOLD=falling(4000.0, later))
    clock["now"] = later
    produced = await service.tick()
    assert [n.id for n in produced] == ["move:GOLD:20261020T11"]  # alone, its own note
    assert service.store.get(event.id).model_dump_json() == before
    assert len(sent) == 2 and len(service.writer.calls) == 2


async def test_a_failed_event_note_does_not_block_its_own_retry():
    writer = V11Writer([WriteResult(None, "m", error="model output was not valid")])
    service, clock, _ = desk({"NQ": falling(30000.0), "ES": falling(6000.0)}, writer=writer)
    assert [n.status for n in await service.tick()] == ["failed"]
    assert service.store.count_since("move", MOVE_AT.isoformat(), instrument="ES",
                                     written_only=True, with_members=True) == 0
    clock["now"] = MOVE_AT + timedelta(minutes=6)
    jobs = await service.due_jobs(clock["now"])
    assert [j[0] for j in jobs] == ["move:20261020T10"]  # one retry, not two single notes


async def test_a_backoff_after_a_failed_event_does_not_split_it_into_single_notes():
    writer = V11Writer([WriteResult(None, "m", error="model output was not valid")])
    service, clock, _ = desk({"NQ": falling(30000.0), "ES": falling(6000.0)}, writer=writer)
    await service.tick()
    clock["now"] = MOVE_AT + timedelta(minutes=1)  # inside the five-minute backoff
    assert await service.due_jobs(clock["now"]) == []


@pytest.mark.parametrize("writer_cls", [V10Writer, FakeWriter])
async def test_v10_and_earlier_still_write_one_note_per_instrument(writer_cls):
    service, _, sent = desk({"NQ": falling(30000.0), "ES": falling(6000.0)},
                            writer=writer_cls())
    notes = await service.tick()
    assert [n.id for n in notes] == ["move:20261020T10", "move:ES:20261020T10"]
    assert all(n.event_members == [] for n in notes) and len(sent) == 2
    for _, facts in service.writer.calls:
        assert "the_event" not in facts and "the_move" in facts


async def test_the_move_facts_of_a_single_note_keep_their_key_order():
    service, _, _ = desk({"NQ": falling(30000.0), "ES": flat(6000.0)})
    await service.tick()
    keys = list(service.writer.calls[0][1])
    assert keys.index("the_move") == keys.index("events_in_move_window") - 1


async def test_the_event_prompt_is_used_and_its_numbers_pass_the_invented_number_check():
    service, _, _ = desk({"NQ": falling(30000.0), "ES": falling(6000.0, step=0.0003)})
    await service.tick()
    kind, facts = service.writer.calls[0]
    prompt = P.render(kind, Instrument.of("NQ"), facts, version=P.V11)
    assert prompt.task == P.PROMPTS[P.V11]["NQ"].event_task
    assert prompt.why_required
    event = facts["the_event"]
    stated = " ".join(f"{r['change']} {r['times_its_trigger']}x {r['its_trigger']}"
                      for r in event["markets_that_moved"])
    quoting = NoteDraft(headline="Both fell", summary=stated, what_it_means="Means.",
                        market_reaction="Reaction.", rates_read="Yields.", watch_next=[],
                        learn="Lesson.", nq_tone="negative")
    assert invented_numbers(quoting, prompt.number_sources) == []


async def test_event_evidence_uses_the_other_members_reports_after_the_leads():
    at = MOVE_AT - timedelta(minutes=20)

    def item(source, title):
        return SimpleNamespace(id=title, source=source, headline=title, published_at=at,
                               received_at=at, source_url=None)

    service, _, _ = make(MOVE_AT, series={"NQ": falling(30000.0), "ES": flat(6000.0),
                                          "GOLD": falling(4000.0)},
                         writer=V11Writer(), instruments=("NQ", "ES", "GOLD"),
                         news=[item("cnbc_markets", "Nasdaq falls as chip stocks slide"),
                               item("kitco", "Gold falls as the dollar firms")])
    [note] = await service.tick()
    items = service.store.facts(note.id)[0]["why_evidence"]["items"]
    assert [i["headline"] for i in items] == ["Nasdaq falls as chip stocks slide",
                                              "Gold falls as the dollar firms"]


# ------------------------------------------------------------------ prompts


def test_v11_is_v10_plus_an_event_task_and_is_not_the_default():
    assert P.PROMPT_VERSION == P.V9 and P.EVENT_VERSIONS == {P.V11}
    for symbol in P.SPECS:
        v10, v11 = P.PROMPTS[P.V10][symbol], P.PROMPTS[P.V11][symbol]
        assert (v11.system, v11.tasks, v11.stated_facts) == (v10.system, v10.tasks,
                                                            v10.stated_facts)
        assert v10.event_task is None and v11.event_task
        assert f"led by {symbol}" in v11.event_task and "{" not in v11.event_task


def test_a_version_without_an_event_task_refuses_event_facts():
    with pytest.raises(P.NoPromptProfile):
        P.render(NoteKind.MOVE, inst("ES"), {"the_event": {"scope": "rates"}}, version=P.V10)


def test_the_event_fact_exists_only_at_event_versions():
    facts = {"the_event": {"x": 1}, "the_move": {"y": 2}}
    assert "the_event" not in P.facts_at(P.V10, facts)
    assert "the_event" in P.facts_at(P.V11, facts)


def test_the_event_task_covers_rates_and_cross_market_wording():
    task = P.PROMPTS[P.V11]["ES"].event_task
    assert "ONE note" in task and "rates" in task and "cross-market" in task
    assert "times_its_trigger" in task and "markets_that_did_not_pass_trigger" in task
    assert '"why"' in task


@pytest.mark.parametrize(("name", "series", "lead"), [
    ("v11_nq_event.txt", {"NQ": falling(30000.0), "ES": falling(6000.0, step=0.0003),
                          "GOLD": flat(4000.0)}, "NQ"),
    ("v11_us10y_rates_event.txt", {"NQ": flat(30000.0), "US2Y": yields(4.0, -0.2),
                                   "US10Y": yields(4.3, -0.25)}, "US10Y"),
])
async def test_event_prompt_snapshots(name, series, lead):
    from tests.desk.test_prompt_snapshot import check

    service, _, _ = desk(series)
    await service.tick()
    kind, facts = service.writer.calls[0]
    prompt = P.render(kind, Instrument.of(lead), facts, version=P.V11)
    check(name, f"SYSTEM\n{prompt.system}\n\nUSER\n{prompt.user}\n")


# ------------------------------------------------------------------ store and API


def stored(service, note_id, symbol, members, at):
    service.store.save(DeskNote(
        id=note_id, kind=NoteKind.MOVE, title="t", created_at=at, instrument=symbol,
        event_members=members, draft=draft(note_id)))


def api(service):
    app = FastAPI()
    app.state.desk = service
    app.include_router(desk_api.router)
    return TestClient(app)


def test_notes_filtered_by_any_member_instrument_return_the_event_once():
    service, _, _ = make(MOVE_AT, writer=V11Writer())
    stored(service, "move:20261020T10", "NQ", ["ES", "GOLD"], MOVE_AT - timedelta(minutes=30))
    stored(service, "move:YM:20261020T08", "YM", [], MOVE_AT - timedelta(hours=2))
    service.store.save(DeskNote(id="release:x", kind=NoteKind.RELEASE, title="r",
                                created_at=MOVE_AT - timedelta(hours=3), draft=draft("rel")))
    client = api(service)
    window = {"from": (MOVE_AT - timedelta(days=1)).isoformat(), "to": MOVE_AT.isoformat()}
    everything = client.get("/api/v1/desk/notes", params=window).json()["notes"]
    assert [n["id"] for n in everything] == ["move:20261020T10", "move:YM:20261020T08",
                                             "release:x"]
    event = everything[0]
    assert event["event_instruments"] == [
        {"symbol": "NQ", "label": "Nasdaq 100 futures"},
        {"symbol": "ES", "label": "S&P 500 futures"},
        {"symbol": "GOLD", "label": "Gold"}]
    assert everything[1]["event_instruments"] == []
    for symbol in ("NQ", "ES", "GOLD"):
        ids = [n["id"] for n in client.get(
            "/api/v1/desk/notes", params={**window, "instrument": symbol}).json()["notes"]]
        assert ids.count("move:20261020T10") == 1 and "move:YM:20261020T08" not in ids
    yms = client.get("/api/v1/desk/notes", params={**window, "instrument": "YM"}).json()
    assert "move:20261020T10" not in [n["id"] for n in yms["notes"]]
    assert client.get("/api/v1/desk/notes", params={"instrument": "nq"}).status_code == 422


def test_the_stored_payload_carries_members_but_never_the_derived_tags():
    service, _, _ = make(MOVE_AT, writer=V11Writer())
    stored(service, "move:20261020T10", "NQ", ["ES"], MOVE_AT)
    stored(service, "move:ES:20261020T10", "ES", [], MOVE_AT)
    payloads = dict(service.store._db.execute("SELECT id, payload FROM desk_notes"))
    event = json.loads(payloads["move:20261020T10"])
    assert event["event_members"] == ["ES"] and "event_instruments" not in event
    assert "event_members" not in json.loads(payloads["move:ES:20261020T10"])
    got = service.store.get("move:20261020T10")
    assert got.event_members == ["ES"] and len(got.event_instruments) == 2
    assert DeskNote.model_validate_json(got.model_dump_json()).event_members == ["ES"]
    body = api(service).get("/api/v1/desk/notes/move:20261020T10").json()
    assert body["event_members"] == ["ES"] and len(body["event_instruments"]) == 2


def test_counting_a_members_notes_is_opt_in():
    service, _, _ = make(MOVE_AT, writer=V11Writer())
    stored(service, "move:20261020T10", "NQ", ["ES"], MOVE_AT)
    since = (MOVE_AT - timedelta(hours=1)).isoformat()
    assert service.store.count_since("move", since, instrument="ES") == 0
    assert service.store.count_since("move", since, instrument="ES", with_members=True) == 1
    assert service.store.count_since("move", since, instrument="NQ") == 1


# ------------------------------------------------------------------ replay


def replay_mover(symbol, now, desk_order):
    if symbol in ("US2Y", "US10Y"):
        return EVT.Mover(inst(symbol), move(symbol, -6.0, "bp", now), 4.0,
                         desk_order.index(symbol))
    change = 1.0 if symbol == "GOLD" else -1.0
    return EVT.Mover(inst(symbol), move(symbol, change, "%", now), 0.8,
                     desk_order.index(symbol))


def test_replay_of_the_november_4_pattern_gives_one_note_per_event_pass():
    """Synthetic passes shaped like Nov 4 05:00 UTC (US10Y, US2Y, NQ, ES, GOLD together),
    a lone mover later, and a late joiner. Replays the grouping and the 3-hour
    limit the way ``due_jobs`` applies them."""
    desk_order = ["NQ", "ES", "YM", "GOLD", "WTI", "DXY", "US2Y", "US10Y", "VIX"]
    base = datetime(2026, 11, 3, 5, 0, tzinfo=UTC)
    passes = [  # (minutes after base, symbols past their trigger in that pass)
        (0, ["US10Y", "US2Y", "NQ", "ES", "GOLD"]),
        (1, ["GOLD", "WTI"]),                       # GOLD blocked; WTI alone
        (60, ["ES", "NQ"]),                         # both blocked by the event
        (130, ["US10Y"]),                           # blocked (inside 3h of the event)
        (185, ["US10Y", "US2Y"]),                   # free again: a rates event
        (200, ["VIX"]),                             # alone
        (24 * 60, ["NQ", "ES", "YM", "DXY"]),       # next day, another event
        (24 * 60 + 5, ["NQ"]),                      # blocked
    ]
    last_note_at: dict[str, datetime] = {}
    written: list[tuple[int, str, list[str]]] = []
    wanted = set()
    for minutes, symbols in passes:
        now = base + timedelta(minutes=minutes)
        movers = [replay_mover(s, now, desk_order) for s in symbols
                  if not (s in last_note_at and now - last_note_at[s] < timedelta(hours=3))]
        wanted |= {(minutes, s) for s in symbols if not (
            s in last_note_at and now - last_note_at[s] < timedelta(hours=3))}
        groups = EVT.group_moves(movers, events=True)
        assert len(groups) <= 1 or not any(g.is_event for g in groups)
        for group in groups:
            names = [m.instrument.symbol for m in group.members]
            written.append((minutes, group.lead.instrument.symbol, names))
            for name in names:
                last_note_at[name] = now
    # At most one note per pass, and every instrument that would have had a note is in one.
    assert len({minute for minute, _, _ in written}) == len(written)
    assert written == [
        (0, "NQ", ["NQ", "US2Y", "US10Y", "ES", "GOLD"]),
        (1, "WTI", ["WTI"]),
        (185, "US2Y", ["US2Y", "US10Y"]),
        (200, "VIX", ["VIX"]),
        (24 * 60, "NQ", ["NQ", "ES", "YM", "DXY"]),
    ]
    assert {(minute, name) for minute, _, names in written for name in names} == wanted
