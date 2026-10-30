"""The golden-set checker: each mechanical rule, and the recorded desk:v4 baseline."""

from __future__ import annotations

import json

from tests.desk import golden as G


def test_direction_word_next_to_a_signed_number():
    assert G.signed_direction("NQ rose +0.88% and the 2-year was down -1.7bp.") == [
        "rose +0.88%", "down -1.7bp"]
    assert G.signed_direction("NQ rose 0.88%; the change is +0.88%.") == []
    assert G.signed_direction("It fell by about -0.4%.") == ["fell by about -0.4%"]


def test_closed_market_called_stale():
    assert G.closed_as_stale(["Futures are shut for the weekend, so prices are stale."])
    assert G.closed_as_stale(["The NQ quote is stale while the market is open."]) == []
    # Keeping the states apart is what the rule asks for.
    assert G.closed_as_stale(["The market is closed, not stale."]) == []


def test_absent_versus_missing():
    assert G.absent_not_missing(["There was no event in the window."])
    assert G.absent_not_missing(["There was no event on the supplied calendar."]) == []
    assert G.absent_not_missing(["No scheduled events are in the feed yet."]) == []


def test_basis_point_written_as_a_percent():
    assert G.bp_as_percent("The 2-year fell 3.1bp (1bp is 0.01%).") == ["bp (1bp is 0.01%"]
    assert G.bp_as_percent("The 10-year yield rose 0.02% today.") == ["10-year yield rose 0.02%"]
    # NQ's percent change next to a yield level is fine.
    assert G.bp_as_percent("NQ ended +0.09% and the 10-year yield sits at 4.999%.") == []


def test_sign_errors_against_measured_moves():
    moves = [{"change": 0.88, "text": "+0.88%"}, {"change": -0.06, "text": "-0.06%"},
             {"change": 0.06, "text": "+0.06%"}]
    assert G.sign_errors("NQ fell 0.88% in the window.", moves) == ["fell 0.88%"]
    assert G.sign_errors("NQ rose 0.88% in the window.", moves) == []
    # A magnitude shared by an up and a down move is never flagged.
    assert G.sign_errors("NQ slipped 0.06% and the VIX rose 0.06%.", moves) == []


def test_repeated_lesson_is_judged_from_the_candidate_text():
    history = [
        {"id": "a", "created_at": "1", "learn": "The VIX is the fear gauge."},
        {"id": "b", "created_at": "2", "learn": "Some general point about markets."},
        {"id": "c", "created_at": "3", "learn": "The VIX measures expected swings."},
    ]
    assert G.repeated_lesson("The VIX measures expected swings.", "3", history,
                             note_id="c") == "vix"
    # A new lesson for the same note, on a fresh term, is not a repeat.
    assert G.repeated_lesson("A Treasury auction is how the government borrows.", "3",
                             history, note_id="c") is None
    # The catch-all topic alone never counts as a repeat.
    assert G.repeated_lesson("Another general point.", "4", history) is None


def test_a_short_candidate_cannot_win_by_saying_less():
    fixtures, history = G.load()
    premarket = next(f for f in fixtures if f["kind"] == "premarket")
    draft = {**premarket["note"]["draft"], "summary": "Up.", "what_it_means": "Data out.",
             "market_reaction": "Calm.", "rates_read": "Flat.", "learn": "Yields matter.",
             "watch_next": []}
    short = G.score({**premarket, "note": {**premarket["note"], "draft": draft}}, history)
    assert short.under_length and short.issues >= 1


def test_v4_baseline_is_unchanged():
    """The golden set's desk:v4 scores. A new prompt version is compared with these."""
    recorded = json.loads((G.GOLDEN / "baseline.json").read_text())
    assert G.summary(G.score_all()) == recorded["desk:v4"]
