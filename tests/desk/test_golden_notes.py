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


def test_repeated_lesson_is_by_taught_term_not_the_catch_all_topic():
    history = [
        {"id": "a", "created_at": "1", "learn": "The VIX is the fear gauge."},
        {"id": "b", "created_at": "2", "learn": "Some general point about markets."},
        {"id": "c", "created_at": "3", "learn": "The VIX measures expected swings."},
        {"id": "d", "created_at": "4", "learn": "Another general point."},
    ]
    assert G.repeated_lesson("c", history) == "vix"
    assert G.repeated_lesson("d", history) is None


def test_v4_baseline_is_unchanged():
    """The golden set's desk:v4 scores. A new prompt version is compared with these."""
    recorded = json.loads((G.GOLDEN / "baseline.json").read_text())
    assert G.summary(G.score_all()) == recorded["desk:v4"]
