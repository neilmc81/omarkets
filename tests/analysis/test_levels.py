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
