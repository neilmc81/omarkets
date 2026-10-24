from datetime import date, timedelta

import pytest

from core.domain.contracts import active_contract, contract_for_symbol


def test_nq_front_month_selection():
    contract = active_contract("NQ", date(2026, 9, 18))
    assert contract.code == "NQZ26"
    assert contract.expiry == date(2026, 12, 18)
    assert contract.provider_month == "Z26"


def test_rolls_to_next_quarter_before_expiry():
    contract = active_contract("NQ", date(2026, 12, 15), roll_days_before_expiry=8)
    assert contract.code == "NQH27"
    assert contract.expiry == date(2027, 3, 19)


def test_contract_rolls_only_at_threshold():
    just_before = active_contract("NQ", date(2026, 12, 9), roll_days_before_expiry=8)
    at_roll = active_contract("NQ", date(2026, 12, 10), roll_days_before_expiry=8)
    assert just_before.code == "NQZ26"
    assert at_roll.code == "NQH27"


def test_quarter_codes_cover_all_four_months():
    codes = {
        active_contract("ES", date(2026, month, 1)).code
        for month in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)
    }
    assert {
        "ESH26",
        "ESM26",
        "ESU26",
        "ESZ26",
        "ESH27",
    }.issuperset(codes)
    assert all(code.startswith("ES") for code in codes)


def test_contract_for_symbol_only_for_futures():
    assert contract_for_symbol("NQ", date(2026, 9, 18)) is not None
    assert contract_for_symbol("ES", date(2026, 9, 18)) is not None
    assert contract_for_symbol("VIX", date(2026, 9, 18)) is None
    assert contract_for_symbol("BTC", date(2026, 9, 18)) is None


def test_expiry_is_always_a_friday():
    for month in (3, 6, 9, 12):
        contract = active_contract("NQ", date(2026, month, 1))
        assert contract.expiry.weekday() == 4


def test_provider_symbol_uses_root_and_month():
    contract = active_contract("ES", date(2026, 6, 1))
    assert contract.provider_symbol() == "ESM26"
    assert contract.provider_symbol("@") == "@ESM26"
    assert contract.contract_month == "2026-06"


@pytest.mark.parametrize("when", [date(2020, 1, 1), date(2035, 7, 15), date(2026, 12, 31)])
def test_active_contract_always_resolves(when):
    contract = active_contract("NQ", when)
    assert contract.expiry > when - timedelta(days=400)
    assert contract.expiry.weekday() == 4


@pytest.mark.parametrize(
    ("code_month", "last_trade"),
    [
        # The 25th is a Sunday: four business days before it.
        ((2026, 11), date(2026, 10, 20)),
        # The 25th is a business day: three business days before it.
        ((2026, 12), date(2026, 11, 20)),
        # Christmas (Dec 25) is a holiday: four business days before it.
        ((2027, 1), date(2026, 12, 21)),
    ],
)
def test_crude_last_trading_day_follows_the_nymex_rule(code_month, last_trade):
    year, month = code_month
    contract = next(
        c for c in (active_contract("CL", last_trade - timedelta(days=d), roll_days_before_expiry=0)
                    for d in range(0, 40))
        if c.contract_month == f"{year}-{month:02d}"
    )
    assert contract.expiry == last_trade


def test_wti_uses_an_explicit_crude_contract_and_rolls_before_expiry():
    # Oct 20, 2026: the November contract's last trading day. The desk must
    # already be on December, not the expiring (and soon frozen) November.
    contract = contract_for_symbol("WTI", date(2026, 10, 20))
    assert contract.code == "CLZ26"
    assert contract.contract_month == "2026-12"
    assert contract.expiry == date(2026, 11, 20)
    assert contract.provider_symbol() == "CLZ26"
    assert active_contract("CL", date(2026, 10, 11)).code == "CLX26"
    assert active_contract("CL", date(2026, 10, 12)).code == "CLZ26"


def test_crude_rolls_across_the_year_end():
    assert active_contract("CL", date(2026, 12, 15)).code == "CLG27"
    assert active_contract("CL", date(2026, 12, 15)).contract_month == "2027-02"


def test_equity_contract_month_is_its_expiry_month():
    contract = active_contract("NQ", date(2026, 9, 18))
    assert contract.contract_month == "2026-12"
