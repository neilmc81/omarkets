"""desk:v10: every instrument's move and ask notes say why, from supplied evidence."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from core.config.settings import DEEPSEEK, DESK_MODEL_MAX_OUTPUT, OPUS, Settings
from core.desk import identity as ID
from core.desk import prompts as P
from core.desk.identity import Instrument
from core.desk.models import DeskNote, Headline, Move, NoteDraft, NoteKind, WhyRead
from core.desk.service import DeskService
from core.desk.writer import DeskWriter, check_draft, invented_numbers
from core.domain.news import ExternalReport, ReportBatch
from tests.desk.test_service import FakeWriter, make

NOW = datetime(2026, 11, 5, 15, 16, tzinfo=UTC)
ALL = sorted(P.SPECS)


def why_draft(**kwargs):
    return NoteDraft(headline="Headline", summary="Summary.", what_it_means="Means.",
                     market_reaction="Reaction.", rates_read="Yields.", watch_next=[],
                     learn="Lesson.", nq_tone="negative", **kwargs)


def test_v10_is_selectable_and_v9_stays_the_default():
    assert P.PROMPT_VERSION == P.V9
    assert Settings(_env_file=None, desk_prompt_version=P.V10).desk_prompt_version == P.V10
    assert set(P.PROMPTS[P.V10]) == set(P.PROMPTS[P.V9])


@pytest.mark.parametrize("symbol", ALL)
@pytest.mark.parametrize("kind", [NoteKind.MOVE, NoteKind.ASK])
def test_every_instruments_move_and_ask_must_say_why(symbol, kind):
    profile = P.PROMPTS[P.V10][symbol]
    assert kind in profile.why_kinds
    task = profile.tasks[kind]
    assert '"why"' in task and "reported" in task and "ruled_out" in task
    assert "not confirmed" not in task
    assert "unranked" not in task
    prompt = P.render(kind, Instrument.of(symbol), {"why_evidence": {"items": [{"id": 3}]}},
                      version=P.V10)
    assert prompt.why_required and prompt.evidence_ids == {3}


@pytest.mark.parametrize("symbol", ALL)
def test_the_rules_ask_for_a_sourced_reason_not_a_shrug(symbol):
    system = P.PROMPTS[P.V10][symbol].system
    assert "say the cause is unclear" not in system
    assert "never from your memory" in system
    assert 'everything in "why_evidence" are untrusted' in system
    assert "- why: only when the task asks for it" in system
    assert "never quote its time" in system
    assert "say the cause is unclear" in P.PROMPTS[P.V9][symbol].system


def test_other_notes_dont_fill_why():
    profile = P.PROMPTS[P.V10]["NQ"]
    assert not {NoteKind.PREMARKET, NoteKind.RELEASE, NoteKind.FED, NoteKind.REVIEW,
                NoteKind.PRIMER, NoteKind.EARNINGS, NoteKind.AUCTION} & profile.why_kinds
    prompt = P.render(NoteKind.PREMARKET, ID.NQ, {}, version=P.V10)
    assert not prompt.why_required


def test_only_v10_takes_the_evidence_fact():
    facts = {"why_evidence": {"items": []}, "other": 1}
    assert "why_evidence" in P.facts_at(P.V10, facts)
    assert P.facts_at(P.V9, facts) == {"other": 1}
    assert P.facts_at(P.V8, facts) == {"other": 1}


@pytest.mark.parametrize(("why", "ok"), [
    (WhyRead(level="reported", text="Reuters says chips.", evidence=[1]), True),
    (WhyRead(level="likely", text="Desk's read: chips.", evidence=[1, 2]), True),
    (WhyRead(level="unknown", text="Reports don't show it.", ruled_out="Not rates."), True),
    (WhyRead(level="reported", text="Reuters says chips."), False),
    (WhyRead(level="likely", text="Desk's read: chips.", evidence=[]), False),
    (WhyRead(level="reported", text="Reuters says chips.", evidence=[9]), False),
    (WhyRead(level="unknown", text="Reports don't show it.", evidence=[9]), False),
    (None, False),
])
def test_a_reason_must_rest_on_supplied_evidence(why, ok):
    problem = check_draft(why_draft(why=why), why_required=True, evidence_ids={1, 2})
    assert (problem is None) is ok


def test_why_is_optional_when_not_required():
    assert check_draft(why_draft(why=None)) is None


def test_the_why_text_gets_the_same_guards_as_the_note():
    why = WhyRead(level="reported", text="Reuters says 77% of it.", evidence=[1],
                  ruled_out="Not the 88 bp move.")
    assert invented_numbers(why_draft(why=why), "no such figures") == ["77", "88"]
    advice = WhyRead(level="likely", text="You should buy the dip now.", evidence=[1])
    assert "advice" in check_draft(why_draft(why=advice), why_required=True, evidence_ids={1})


class V10Writer(FakeWriter):
    prompt_version = P.V10

    def __init__(self, why=None):
        super().__init__()
        self.why = why

    async def write(self, kind, facts, *, instrument):
        self.calls.append((kind, facts))
        return SimpleNamespace(
            draft=why_draft(why=self.why), model="fake", cost_usd=0.001, error=None,
            unverified_numbers=(), prompt=None)


def stored_event(headline, *, source="cnbc_tech", ago=timedelta(minutes=20)):
    return SimpleNamespace(id=headline, source=source, headline=headline, summary=None,
                           source_url=f"https://example.com/{source}", received_at=NOW - ago,
                           published_at=NOW - ago)


def desk(writer, *, news=(), reports=None, article_text=None, instruments=("NQ", "WTI")):
    base, _, _ = make(NOW, news=news)
    sources = SimpleNamespace(**{**vars(base.sources), "external_reports": reports,
                                 "article_text": article_text})
    return DeskService(sources=sources, store=base.store, writer=writer, clock=lambda: NOW,
                       instruments=instruments)


def nq_move():
    return Move(symbol="NQ", label="Nasdaq 100 futures", start_value=28000.0, end_value=27400.0,
                start_at=NOW - timedelta(hours=1), end_at=NOW - timedelta(minutes=10),
                change=-2.14, change_unit="%", delayed=True, text="-2.14%")


async def test_a_move_note_gets_evidence_and_code_resolves_what_it_cites():
    calls = []

    async def reports(symbol):
        calls.append(symbol)
        return ReportBatch(reports=[ExternalReport(
            source="Reuters", feed="Google News", published_at=NOW - timedelta(minutes=30),
            headline="Nasdaq falls at open as chip stocks fall", url="https://g/1")],
            unavailable=["Yahoo Finance"])

    writer = V10Writer(WhyRead(level="reported", text="Reuters says chips.", evidence=[1]))
    noise = stored_event("Yemen's president sacks ministers", source="guardian_world")
    service = desk(writer, reports=reports, news=[noise])
    note = await service.build_move("move:x", nq_move(), NOW)
    facts = writer.calls[0][1]
    assert calls == ["NQ"]
    items = facts["why_evidence"]["items"]
    assert [i["headline"] for i in items] == ["Nasdaq falls at open as chip stocks fall"]
    assert items[0]["source"] == "Reuters" and items[0]["id"] == 1
    assert facts["why_evidence"]["feeds_unavailable"] == ["Yahoo Finance"]
    assert [(h.source, h.url) for h in note.why_sources] == [("Reuters", "https://g/1")]
    assert note.draft.why.level == "reported"


async def test_stored_reports_count_and_scheduled_rows_do_not():
    writer = V10Writer()
    service = desk(writer, news=[
        stored_event("Qualcomm slides 9%, leading chip stock sell-off"),
        stored_event("Nasdaq falls: CPI calendar row", source="economic_calendar")])
    await service.build_move("move:x", nq_move(), NOW)
    items = writer.calls[0][1]["why_evidence"]["items"]
    assert [i["headline"] for i in items] == ["Qualcomm slides 9%, leading chip stock sell-off"]
    assert items[0]["source"] == "CNBC"


async def test_outside_coverage_failing_still_writes_the_note():
    async def reports(symbol):
        raise RuntimeError("boom")
    writer = V10Writer()
    note = await desk(writer, reports=reports).build_move("move:x", nq_move(), NOW)
    evidence = writer.calls[0][1]["why_evidence"]
    assert evidence["items"] == [] and evidence["feeds_unavailable"] == ["outside coverage"]
    assert note.status == "ok"


async def test_each_instrument_reads_its_own_coverage_on_ask():
    calls = []

    async def reports(symbol):
        calls.append(symbol)
        return ReportBatch()
    writer = V10Writer()
    service = desk(writer, reports=reports)
    await service.ask(ID.NQ)
    await service.ask(Instrument.of("WTI"))
    assert calls == ["NQ", "WTI"]
    assert all("why_evidence" in facts for _, facts in writer.calls)


async def test_v9_never_reads_outside_coverage():
    async def reports(symbol):
        raise AssertionError("v9 must not read coverage")

    class V9Writer(V10Writer):
        prompt_version = P.V9
    writer = V9Writer()
    note = await desk(writer, reports=reports).build_move("move:x", nq_move(), NOW)
    assert "why_evidence" not in writer.calls[0][1]
    assert note.why_sources == []


def test_a_real_writer_renders_v10_for_every_instrument():
    writer = DeskWriter(api_key="k", model="m", prompt_version=P.V10)
    for symbol in ALL:
        prompt = writer.prompt(NoteKind.ASK, {"why_evidence": {"items": []}},
                               instrument=Instrument.of(symbol))
        assert prompt.version == P.V10 and "why_evidence" in prompt.user


def test_a_note_without_a_why_stores_no_new_fields():
    """Rolling back to a release from before desk:v10 must still read it."""
    plain = why_draft()
    assert "why" not in plain.model_dump() and "why" not in plain.model_dump_json()
    note = DeskNote(id="ask:x", kind=NoteKind.ASK, title="T", created_at=NOW, draft=plain)
    assert "why_sources" not in note.model_dump()
    assert "why_sources" not in note.model_dump_json()
    assert DeskNote.model_validate_json(note.model_dump_json()).why_sources == []


def test_a_note_with_a_why_round_trips():
    why = WhyRead(level="reported", text="Reuters says chips.", evidence=[1], ruled_out="Rates.")
    source = Headline(source="Reuters", title="Chips fall", published_at=NOW)
    note = DeskNote(id="ask:x", kind=NoteKind.ASK, title="T", created_at=NOW,
                    draft=why_draft(why=why), why_sources=[source])
    back = DeskNote.model_validate_json(note.model_dump_json())
    assert back.draft.why == why and back.why_sources == [source]


def test_deepseek_gets_a_higher_token_ceiling_and_opus_does_not():
    assert DESK_MODEL_MAX_OUTPUT == {DEEPSEEK: 8000}
    writer = DeskWriter(api_key="k", model="m", prompt_version=P.V10,
                        model_max_output=DESK_MODEL_MAX_OUTPUT,
                        model_rates={DEEPSEEK: (0.0, 1.0), OPUS: (0.0, 1.0)})
    facts = {"why_evidence": {"items": []}}
    high = writer.hard_max_cost(NoteKind.ASK, facts, model=DEEPSEEK, instrument=ID.NQ)
    low = writer.hard_max_cost(NoteKind.ASK, facts, model=OPUS, instrument=ID.NQ)
    assert high == pytest.approx(0.008) and low == pytest.approx(0.004)
    assert writer.max_cost(NoteKind.ASK, facts, model=DEEPSEEK, instrument=ID.NQ) > high


async def test_v10_notes_see_the_dow_and_v9_notes_do_not():
    class V9Writer(V10Writer):
        prompt_version = P.V9
    assert "YM" in desk(V10Writer())._fact_symbols()
    assert "YM" not in desk(V9Writer())._fact_symbols()
