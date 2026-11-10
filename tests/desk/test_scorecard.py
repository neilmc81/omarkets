"""The scorecard: counts grouped by kind, prompt version and model on a seeded store."""

from datetime import UTC, datetime, timedelta

from core.desk import scorecard as SC
from core.desk.models import Checkpoint, DeskNote, FollowUp, NoteDraft, NoteKind, WhyRead
from core.desk.store import DeskStore

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
WEEK = timedelta(days=7)


def written(why=None):
    return NoteDraft(headline="Headline", summary="Summary.", what_it_means="Means.",
                     market_reaction="React.", learn="Learn.", nq_tone="mixed",
                     why=WhyRead(level=why, text="Because.") if why else None)


def seed(store, note_id, kind, days_ago, *, model="deepseek/x · desk:v11", cost=0.01, why=None,
         verdict=None, status="ok", rating=None, hours=0):
    at = NOW - timedelta(days=days_ago, hours=hours)
    note = DeskNote(id=note_id, kind=kind, title=note_id, created_at=at, status=status,
                    draft=written(why) if status == "ok" else None, model=model, cost_usd=cost)
    store.save(note)
    if verdict:
        store.save_follow_up(note_id, FollowUp(
            checkpoints=[Checkpoint(label="1h", at=at)], verdict=verdict, final=True, text="t"))
    if rating:
        store.set_rating(note_id, rating, at)


def seeded():
    store = DeskStore(":memory:")
    # Current week
    seed(store, "move:NQ:a", NoteKind.MOVE, 1, why="reported", verdict="held", rating="useful")
    seed(store, "move:ES:b", NoteKind.MOVE, 2, why="likely", verdict="faded", rating="not_useful")
    seed(store, "move:GOLD:c", NoteKind.MOVE, 3, why="unknown")  # no follow-up yet
    seed(store, "move:WTI:d", NoteKind.MOVE, 2, model="deepseek/x · desk:v9")  # no why: older
    seed(store, "move:VIX:e", NoteKind.MOVE, 4, status="failed", cost=None)
    seed(store, "ask:NQ:f", NoteKind.ASK, 1, why="reported", cost=0.05,
         model="anthropic/opus · desk:v11")
    seed(store, "release:g", NoteKind.RELEASE, 5, verdict="grew")
    seed(store, "premarket:h", NoteKind.PREMARKET, 1, cost=0.20, model="anthropic/opus · desk:v11")
    # The week before
    seed(store, "move:NQ:old", NoteKind.MOVE, 9, why="likely", verdict="reversed")
    seed(store, "move:ES:older", NoteKind.MOVE, 13, model=None, cost=None)
    # Outside both
    seed(store, "move:NQ:ancient", NoteKind.MOVE, 20)
    return store


def card(store, **kwargs):
    notes = store.between(NOW - 2 * WEEK, NOW)
    return SC.build(notes, now=NOW, start=NOW - WEEK, end=NOW, **kwargs)


def group(per, kind, version):
    return next(g for g in per.groups if g.kind == kind and g.prompt_version == version)


def test_totals_by_kind_and_prompt_version():
    per = card(seeded()).current
    assert (per.written, per.failed) == (7, 1)
    assert {(k.kind.value, k.written, k.failed) for k in per.by_kind} == {
        ("move", 4, 1), ("ask", 1, 0), ("release", 1, 0), ("premarket", 1, 0)}
    assert {(v.prompt_version, v.written, v.failed) for v in per.by_prompt_version} == {
        ("desk:v11", 6, 1), ("desk:v9", 1, 0)}


def test_groups_keep_prompt_versions_apart():
    per = card(seeded()).current
    v11 = group(per, NoteKind.MOVE, "desk:v11")
    v9 = group(per, NoteKind.MOVE, "desk:v9")
    assert (v11.written, v11.failed, v9.written, v9.failed) == (3, 1, 1, 0)
    assert v11.why.model_dump() == {"reported": 1, "likely": 1, "unknown": 1, "not_recorded": 0}
    assert v9.why.model_dump() == {"reported": 0, "likely": 0, "unknown": 0, "not_recorded": 1}


def test_verdicts_and_missing_verdicts_are_counted_apart():
    per = card(seeded()).current
    v11 = group(per, NoteKind.MOVE, "desk:v11")
    assert v11.verdicts.model_dump() == {"grew": 0, "held": 1, "faded": 1, "reversed": 0,
                                         "quiet": 0, "no_verdict": 1}
    assert group(per, NoteKind.RELEASE, "desk:v11").verdicts.grew == 1
    # Kinds with no follow-up or no why have none: not zero of everything.
    assert group(per, NoteKind.ASK, "desk:v11").verdicts is None
    assert group(per, NoteKind.RELEASE, "desk:v11").why is None
    assert group(per, NoteKind.PREMARKET, "desk:v11").verdicts is None


def test_ratings_are_split_and_failed_notes_are_not_unrated():
    v11 = group(card(seeded()).current, NoteKind.MOVE, "desk:v11")
    assert v11.ratings.model_dump() == {"useful": 1, "not_useful": 1, "unrated": 1}


def test_cost_is_grouped_by_model_and_version_and_missing_cost_is_not_zero():
    costs = {(c.model, c.prompt_version): c for c in card(seeded()).current.costs}
    deepseek = costs[("deepseek/x", "desk:v11")]
    assert (deepseek.notes, deepseek.priced, deepseek.unpriced) == (5, 4, 1)
    assert round(deepseek.total_usd, 4) == 0.04 and round(deepseek.mean_usd, 4) == 0.01
    opus = costs[("anthropic/opus", "desk:v11")]
    assert round(opus.total_usd, 2) == 0.25 and round(opus.mean_usd, 3) == 0.125
    assert costs[("deepseek/x", "desk:v9")].notes == 1


def test_previous_period_and_a_note_with_no_model():
    previous = card(seeded()).previous
    assert (previous.written, previous.failed) == (2, 0)
    unknown = next(c for c in previous.costs if c.model == "unknown")
    assert unknown.prompt_version == "unknown" and unknown.mean_usd is None
    assert unknown.priced == 0 and unknown.total_usd == 0


def test_empty_ranges():
    store = DeskStore(":memory:")
    empty = card(store)
    assert empty.current.written == 0 and empty.current.groups == []
    assert empty.current.costs == [] and empty.previous.groups == []
    only_old = SC.build(seeded().between(NOW - 40 * WEEK, NOW), now=NOW,
                        start=NOW + WEEK, end=NOW + 2 * WEEK, compare=False)
    assert only_old.current.written == 0 and only_old.previous is None
    assert "no notes in this period" in SC.format_text(empty)


def test_the_range_is_half_open():
    store = DeskStore(":memory:")
    seed(store, "move:NQ:edge", NoteKind.MOVE, 0)  # created exactly at NOW
    seed(store, "move:NQ:start", NoteKind.MOVE, 7)  # exactly at the start
    per = card(store).current
    assert per.written == 1  # the one at the start counts, the one at the end does not


def test_split_model():
    assert SC.split_model("a/b · desk:v11") == ("a/b", "desk:v11")
    assert SC.split_model("a · b · desk:v9") == ("a · b", "desk:v9")
    assert SC.split_model("plain") == ("plain", "unknown")
    assert SC.split_model(None) == ("unknown", "unknown")


def test_plain_text_table_names_every_split():
    text = SC.format_text(card(seeded()))
    assert text.count("by kind and prompt version") == 2 and "Previous period" in text
    assert "desk:v9" in text and "anthropic/opus" in text and "(1 unpriced)" in text
    assert "n/a" in text  # a mean with no priced notes
