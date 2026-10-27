"""Normal move size facts for the desk writer, and the scaled large-move trigger."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from core.analysis.move_size import MoveSize
from core.desk import facts as F
from core.desk.models import NoteDraft, NoteKind
from core.desk.prompts import SYSTEM, user_message
from core.desk.service import DeskService, DeskSources
from core.desk.store import DeskStore
from core.desk.writer import invented_numbers
from core.domain.sessions import globex_session
from tests.analysis.test_move_size import NOW, ohlc_of, past_days, session_rows

VXN_AT = datetime(2026, 10, 26, 20, 15, tzinfo=UTC)


def size(**kwargs):
    values = dict(trade_date=NOW.date(), sessions=14, typical_range=450.75,
                  typical_range_pct=1.4553, realised_vol_pct=17.18, today_range=170.25,
                  today_range_pct=0.5774, today_partial=True, today_complete=False)
    return MoveSize(**{**values, **kwargs})


def index(value, at=VXN_AT):
    return SimpleNamespace(value=value, timestamp=at)


def test_normal_move_texts():
    facts = F.normal_move(size(), index(26.4), index(18.39), trigger_pct=0.8, trigger_scaled=True)
    assert facts == {
        "typical_daily_range": "451 points (1.46%)",
        "typical_measured_over": "median of the last 14 complete sessions",
        "session_range_so_far": "Thu Oct 22 session: 170 points (0.58%), 40% of a typical day",
        "realised_volatility": "17.2% a year",
        "vxn": "26.40", "vxn_as_of": "Mon Oct 26 16:15 ET",
        "vix": "18.39", "vxn_minus_vix": "+8.01 points",
        "large_move_trigger": "0.80% in one hour, scaled to the typical daily range",
    }


def test_finished_session_and_missing_parts_are_left_out():
    facts = F.normal_move(size(typical_range=None, typical_range_pct=None,
                               realised_vol_pct=None, today_partial=False,
                               today_complete=True),
                          None, None, trigger_pct=0.8, trigger_scaled=False)
    assert facts == {"session_range": "Thu Oct 22 session: 170 points (0.58%)",
                     "large_move_trigger": "0.80% in one hour, fixed (not enough history to "
                                           "scale it)"}
    assert F.normal_move(None, None, None, trigger_pct=0.8, trigger_scaled=False) is None
    # A finished session with gaps in its quotes says nothing about its range.
    incomplete = size(typical_range=None, typical_range_pct=None, realised_vol_pct=None,
                      today_partial=False, today_complete=False)
    assert F.normal_move(incomplete, None, None, trigger_pct=0.8, trigger_scaled=False) is None
    assert F.normal_move(None, index(None), index(float("nan")), trigger_pct=0.8,
                         trigger_scaled=True) is None


def test_vxn_and_vix_compared_only_when_quoted_together():
    facts = F.normal_move(None, index(26.4), index(18.39, VXN_AT - timedelta(hours=2)),
                          trigger_pct=0.8, trigger_scaled=True)
    assert facts["vxn"] == "26.40"
    assert "vix" not in facts and "vxn_minus_vix" not in facts


def test_normal_move_numbers_pass_the_writer_number_check():
    facts = F.normal_move(size(), index(26.4), index(18.39), trigger_pct=0.8, trigger_scaled=True)
    message = user_message(NoteKind.MOVE, {"normal_move_size": facts})
    draft = NoteDraft(
        headline="NQ is having a quiet day", summary="NQ has moved 170 points, 40% of normal.",
        what_it_means="A typical day is 451 points; VXN is 26.40, 8.01 above VIX.",
        market_reaction="Realised volatility is 17.2% a year.", rates_read="Yields flat.",
        learn="VXN measures expected Nasdaq swings.", nq_tone="neutral")
    assert invented_numbers(draft, SYSTEM + message) == []


def desk(ohlc, instruments=None, threshold=0.8):
    return DeskService(
        sources=DeskSources(market_state=lambda: SimpleNamespace(instruments=instruments or {}),
                            history=lambda *a, **k: [], calendar_events=lambda s, e: [],
                            news_events=lambda: [], ohlc=ohlc),
        store=DeskStore(":memory:"), writer=None, move_threshold_pct=threshold)


def test_trigger_scales_once_per_trade_date():
    rows = [r for day in past_days(20) for r in session_rows(day, swing=200.0)]
    calls = []

    def ohlc(*args, **kwargs):
        calls.append(1)
        return ohlc_of(rows)(*args, **kwargs)

    service = desk(ohlc)
    # Range 400 on a last price of 19,800 is 2.02%; 0.55 x 2.02 = 1.11%.
    assert service._move_trigger(NOW) == (1.11, True)
    assert service._move_trigger(NOW + timedelta(hours=1)) == (1.11, True)
    assert len(calls) == 1


def test_trigger_falls_back_to_the_fixed_threshold():
    assert desk(None)._move_trigger(NOW) == (0.8, False)

    def broken(*args, **kwargs):
        raise RuntimeError("storage down")

    service = desk(broken, threshold=0.9)
    assert service._move_trigger(NOW) == (0.9, False)
    assert service._trigger is None  # a failure is retried, not cached
    assert desk(ohlc_of([]), threshold=0.9)._move_trigger(NOW) == (0.9, False)


async def test_large_move_uses_the_scaled_trigger(monkeypatch):
    rows = [r for day in past_days(20) for r in session_rows(day, swing=200.0)]
    service = desk(ohlc_of(rows))
    move = SimpleNamespace(change=1.0)
    monkeypatch.setattr(F, "measure_moves", lambda *a, **k: [move])
    assert await service._large_move(NOW) is None        # 1.0% < 1.11% scaled trigger
    move.change = -1.2
    assert await service._large_move(NOW) is move


def test_context_carries_normal_move_size():
    rows = [r for day in past_days(20) for r in session_rows(day, swing=200.0)]
    facts = desk(ohlc_of(rows), {"VXN": index(26.4), "VIX": index(18.39)})._nq_context(NOW)
    assert facts["normal_move_size"]["typical_daily_range"] == "400 points (2.02%)"
    assert facts["normal_move_size"]["large_move_trigger"].startswith("1.11% in one hour")


def test_trigger_description_matches_the_cached_trigger():
    rows = [r for day in past_days(20) for r in session_rows(day, swing=200.0)]
    state = {"broken": False}

    def ohlc(*args, **kwargs):
        if state["broken"]:
            raise RuntimeError("storage down")
        return ohlc_of(rows)(*args, **kwargs)

    service = desk(ohlc, {"VXN": index(26.4)})
    assert service._move_trigger(NOW) == (1.11, True)
    state["broken"] = True
    facts = service._nq_context(NOW + timedelta(hours=1))["normal_move_size"]
    assert facts["large_move_trigger"] == "1.11% in one hour, scaled to the typical daily range"


def test_saturday_range_is_labelled_with_fridays_date():
    saturday = datetime(2026, 10, 24, 15, 0, tzinfo=UTC)
    friday = past_days(1, globex_session(datetime(2026, 10, 26).date()))[0]
    rows = [r for day in past_days(20, globex_session(friday)) for r in session_rows(day)]
    rows += session_rows(friday)
    facts = desk(ohlc_of(rows))._nq_context(saturday)["normal_move_size"]
    assert facts["session_range"].startswith("Fri Oct 23 session: 400 points")
