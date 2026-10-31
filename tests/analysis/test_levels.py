"""Key levels: overnight, settle, prior day and week, gap; missing data is null."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from core.analysis.levels import key_levels
from core.domain.models import MarketObservation


def at(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def obs(ts: datetime, value: float, contract: str = "NQZ26") -> MarketObservation:
    return MarketObservation(symbol="NQ", timestamp=ts, value=value, unit="index points",
                             source="cnbc", received_at=ts + timedelta(minutes=10),
                             metadata={"feed_status": "delayed", "contract_code": contract})


def nq(value=29_175.0, settle=29_150.0, contract="NQZ26"):
    return SimpleNamespace(value=value, previous_close=settle, contract_code=contract,
                           feed_status="delayed")


def by_key(levels):
    return {level.key: level for level in levels.levels}


# Thursday Oct 22 2026. Session opened Wed 22:00 UTC; cash opens 13:30 UTC.
PREMARKET = at(2026, 10, 22, 12, 0)
AFTER_OPEN = at(2026, 10, 22, 15, 0)


def seed(repo):
    repo.store([
        # Previous week (Oct 12–16).
        obs(at(2026, 10, 13, 15, 0), 30_212.0), obs(at(2026, 10, 15, 15, 0), 29_142.0),
        # Prior session (Wed Oct 21: Tue 22:00 UTC – Wed 21:00 UTC).
        obs(at(2026, 10, 20, 23, 0), 29_910.0), obs(at(2026, 10, 21, 15, 0), 29_960.0),
        # Tonight's overnight (from Wed 22:00 UTC).
        obs(at(2026, 10, 22, 1, 0), 29_080.0), obs(at(2026, 10, 22, 8, 0), 29_220.0),
        obs(at(2026, 10, 22, 11, 0), 29_170.0),
        # Cash session.
        obs(at(2026, 10, 22, 13, 30, 5), 29_180.0), obs(at(2026, 10, 22, 14, 0), 29_300.0),
    ])


def test_levels_before_the_cash_open(memory_repo):
    seed(memory_repo)
    levels = by_key(key_levels("NQ", nq(), memory_repo.ohlc, now=PREMARKET))
    assert levels["overnight_high"].value == 29_220.0
    assert levels["overnight_low"].value == 29_080.0
    assert levels["overnight_high"].partial is True
    assert levels["prior_settle"].value == 29_150.0
    assert (levels["prior_day_high"].value, levels["prior_day_low"].value) == (29_960.0, 29_910.0)
    assert (levels["prior_week_high"].value, levels["prior_week_low"].value) == (30_212.0,
                                                                                29_142.0)
    # Before 09:30 ET the gap is where the cash market would open now.
    gap = levels["gap_vs_settle"]
    assert gap.value == 25.0 and gap.partial and gap.value_text == "+25.00 (+0.09%)"
    assert all(level.explainer for level in levels.values())


def test_after_the_open_the_gap_uses_the_first_cash_price(memory_repo):
    seed(memory_repo)
    levels = by_key(key_levels("NQ", nq(value=29_300.0), memory_repo.ohlc, now=AFTER_OPEN))
    assert levels["gap_vs_settle"].value == 30.0  # 29,180 first cash quote − 29,150
    assert levels["gap_vs_settle"].partial is False
    # The overnight stops at the cash open: the 29,300 cash quote is not in it.
    assert levels["overnight_high"].value == 29_220.0
    assert levels["overnight_high"].partial is False


def test_missing_data_is_null_with_a_reason_never_zero(memory_repo):
    levels = by_key(key_levels("NQ", nq(settle=None), memory_repo.ohlc, now=PREMARKET))
    for key in ("overnight_high", "prior_day_low", "prior_week_high", "prior_settle",
                "gap_vs_settle"):
        assert levels[key].value is None
        assert levels[key].value_text == "unavailable"
        assert levels[key].unavailable_reason
    assert levels["overnight_high"].unavailable_reason == "no stored quotes for this window"
    assert levels["gap_vs_settle"].unavailable_reason == "no prior settle"


def test_no_instrument_state_at_all(memory_repo):
    seed(memory_repo)
    levels = by_key(key_levels("NQ", None, memory_repo.ohlc, now=PREMARKET))
    assert levels["prior_settle"].value is None
    assert levels["overnight_high"].value == 29_220.0


def test_quotes_from_another_contract_are_not_mixed_in(memory_repo):
    # After the roll to March, last week's December quotes are a different price series.
    memory_repo.store([obs(at(2026, 10, 13, 15, 0), 30_212.0, contract="NQZ26")])
    levels = by_key(key_levels("NQ", nq(contract="NQH27"), memory_repo.ohlc, now=PREMARKET))
    assert levels["prior_week_high"].value is None
    assert "NQZ26" in levels["prior_week_high"].unavailable_reason


# ES around the Thanksgiving half day. In late November New York is UTC−5.
# Thu Nov 26 2026 is a holiday; Fri Nov 27 closes early: cash 13:00 ET (18:00 UTC),
# Globex 13:15 ET (18:15 UTC). Its session opens Thu 18:00 ET (23:00 UTC).


def es_obs(ts: datetime, value: float, contract: str = "ESZ26") -> MarketObservation:
    return MarketObservation(symbol="ES", timestamp=ts, value=value, unit="index points",
                             source="cnbc", received_at=ts + timedelta(minutes=10),
                             metadata={"feed_status": "delayed", "contract_code": contract})


def es(value=6_805.0, settle=6_800.0, contract="ESZ26"):
    return SimpleNamespace(value=value, previous_close=settle, contract_code=contract,
                           feed_status="delayed")


def seed_es_thanksgiving(repo):
    repo.store([
        # Week of Nov 16–20 (prior week for Fri Nov 27).
        es_obs(at(2026, 11, 16, 15, 0), 6_900.0), es_obs(at(2026, 11, 19, 15, 0), 6_700.0),
        # Week of Nov 23–27. Mon Nov 23 and Tue Nov 24 sessions.
        es_obs(at(2026, 11, 23, 15, 0), 6_750.0), es_obs(at(2026, 11, 24, 15, 0), 6_760.0),
        # Wed Nov 25 session (Tue 23:00 UTC – Wed 22:00 UTC): prior day for Fri Nov 27.
        es_obs(at(2026, 11, 25, 2, 0), 6_780.0), es_obs(at(2026, 11, 25, 16, 0), 6_820.0),
        es_obs(at(2026, 11, 25, 21, 50), 6_800.0),
        # Fri Nov 27 overnight (from Thu 23:00 UTC to the 14:30 UTC cash open).
        es_obs(at(2026, 11, 27, 1, 0), 6_790.0), es_obs(at(2026, 11, 27, 9, 0), 6_830.0),
        es_obs(at(2026, 11, 27, 14, 0), 6_810.0),
        # Fri Nov 27 short cash session, then the last minutes before the 13:15 ET close.
        es_obs(at(2026, 11, 27, 14, 30, 5), 6_812.0), es_obs(at(2026, 11, 27, 16, 0), 6_845.0),
        es_obs(at(2026, 11, 27, 18, 10), 6_840.0),
        # After the 13:15 ET Globex close: not part of any session.
        es_obs(at(2026, 11, 27, 18, 40), 6_999.0), es_obs(at(2026, 11, 27, 20, 0), 6_600.0),
        # Mon Nov 30 overnight (from Sun 23:00 UTC).
        es_obs(at(2026, 11, 30, 1, 0), 6_835.0), es_obs(at(2026, 11, 30, 10, 0), 6_855.0),
    ])


def test_es_levels_on_the_friday_half_day(memory_repo):
    seed_es_thanksgiving(memory_repo)
    # Fri Nov 27, 12:00 ET: inside the short cash session.
    now = at(2026, 11, 27, 17, 0)
    result = key_levels("ES", es(value=6_845.0, settle=6_800.0), memory_repo.ohlc, now=now)
    levels = by_key(result)
    assert result.trade_date.isoformat() == "2026-11-27"
    assert result.contract == "ESZ26"
    # Overnight: Thu 18:00 ET to the 09:30 ET cash open (no holiday-day quotes exist).
    assert (levels["overnight_high"].value, levels["overnight_low"].value) == (6_830.0, 6_790.0)
    assert levels["overnight_high"].window_start == at(2026, 11, 26, 23, 0)
    assert levels["overnight_high"].window_end == at(2026, 11, 27, 14, 30)
    assert levels["overnight_high"].partial is False
    # Prior day skips the Thanksgiving holiday: it is Wednesday's full session.
    assert (levels["prior_day_high"].value, levels["prior_day_low"].value) == (6_820.0, 6_780.0)
    assert levels["prior_day_high"].window_start == at(2026, 11, 24, 23, 0)
    assert levels["prior_day_high"].window_end == at(2026, 11, 25, 22, 0)
    assert (levels["prior_week_high"].value, levels["prior_week_low"].value) == (6_900.0,
                                                                                6_700.0)
    assert levels["prior_week_high"].window_start == at(2026, 11, 15, 23, 0)
    assert levels["prior_week_high"].window_end == at(2026, 11, 20, 22, 0)
    assert levels["prior_settle"].value == 6_800.0
    gap = levels["gap_vs_settle"]
    assert gap.value == 12.0 and gap.window_start == at(2026, 11, 27, 14, 30)


def test_es_levels_after_the_early_close_stay_on_the_short_session(memory_repo):
    seed_es_thanksgiving(memory_repo)
    # Fri Nov 27, 15:00 ET: the market shut at 13:15 ET; a 6,600 print at 15:00 ET
    # is outside every session and must not leak into anything.
    result = key_levels("ES", es(), memory_repo.ohlc, now=at(2026, 11, 27, 20, 0))
    levels = by_key(result)
    assert result.trade_date.isoformat() == "2026-11-27"
    assert levels["overnight_low"].value == 6_790.0
    assert levels["gap_vs_settle"].value == 12.0  # still the first 09:30 ET print


def test_es_monday_prior_day_is_the_short_friday_session(memory_repo):
    seed_es_thanksgiving(memory_repo)
    # Mon Nov 30, 08:00 ET pre-market.
    now = at(2026, 11, 30, 13, 0)
    levels = by_key(key_levels("ES", es(value=6_850.0, settle=6_840.0), memory_repo.ohlc,
                               now=now))
    # Prior day = Fri Nov 27, Thu 18:00 ET to the 13:15 ET early close. The 13:10 ET
    # print is in; the 13:40 ET (6,999) and 15:00 ET (6,600) prints are not.
    day_high, day_low = levels["prior_day_high"], levels["prior_day_low"]
    assert (day_high.value, day_low.value) == (6_845.0, 6_790.0)
    assert day_high.window_start == at(2026, 11, 26, 23, 0)
    assert day_high.window_end == at(2026, 11, 27, 18, 15)
    # Prior week = Mon Nov 23 – Fri Nov 27 (four sessions), ending at the early close.
    week_high, week_low = levels["prior_week_high"], levels["prior_week_low"]
    assert (week_high.value, week_low.value) == (6_845.0, 6_750.0)
    assert week_high.window_start == at(2026, 11, 22, 23, 0)
    assert week_high.window_end == at(2026, 11, 27, 18, 15)
    # Overnight so far: Sun 18:00 ET to now.
    assert (levels["overnight_high"].value, levels["overnight_low"].value) == (6_855.0,
                                                                              6_835.0)
    assert levels["overnight_high"].window_start == at(2026, 11, 29, 23, 0)
    assert levels["overnight_high"].partial is True
    assert levels["gap_vs_settle"].value == 10.0


def test_ym_levels_are_written_in_whole_points(memory_repo):
    """YM quotes whole index points, so its levels carry no decimals; NQ keeps two."""
    def ym(ts: datetime, value: float) -> MarketObservation:
        return MarketObservation(symbol="YM", timestamp=ts, value=value, unit="index points",
                                 source="cnbc", received_at=ts + timedelta(minutes=10),
                                 metadata={"feed_status": "delayed", "contract_code": "YMZ26"})

    memory_repo.store([ym(at(2026, 10, 22, 1, 0), 51_701.0), ym(at(2026, 10, 22, 8, 0), 52_662.0)])
    state = SimpleNamespace(value=51_928.0, previous_close=52_512.0, contract_code="YMZ26",
                            feed_status="delayed")
    levels = by_key(key_levels("YM", state, memory_repo.ohlc, now=PREMARKET))
    assert levels["overnight_high"].value_text == "52,662"
    assert levels["overnight_low"].value_text == "51,701"
    assert levels["prior_settle"].value_text == "52,512"
    assert levels["gap_vs_settle"].value_text == "-584 (-1.11%)"
