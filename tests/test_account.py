import pytest

from gapguard.risk.account import AccountState, Holding, account_checks, demo_account
from gapguard.risk.policy import GuardPolicy

P = GuardPolicy()


def acc(**kw) -> AccountState:
    base = dict(equity=10_000.0, day_start_equity=10_000.0, floor=9_000.0, daily_loss_limit=500.0, profit_target=1_000.0)
    base.update(kw)
    return AccountState(**base)


def test_floor_door_and_daily_limit_numbers():
    checks, max_risk, mult = account_checks(acc(), 0.0, P)
    assert mult == 1.0
    # floor: (10 000 − 9 000) / 1.43 = 699.30; daily: 500 / 1.43 = 349.65 → daily binds
    assert max_risk == pytest.approx(500 / 1.43)
    floor = next(c for c in checks if c.name == "account.floor")
    assert floor.data["max_new_risk"] == pytest.approx(1000 / 1.43)


def test_open_risk_is_subtracted():
    _, max_risk, _ = account_checks(acc(), 100.0, P)
    assert max_risk == pytest.approx(500 / 1.43 - 100)


def test_equity_at_floor_blocks():
    checks, max_risk, _ = account_checks(acc(equity=9_000.0, day_start_equity=9_000.0), 0.0, P)
    assert max_risk == 0.0
    assert checks[-1].name == "account.floor" and checks[-1].status == "block"


def test_open_positions_alone_can_break_the_floor():
    checks, max_risk, _ = account_checks(acc(), 720.0, P)      # 1.43 × 720 = 1 029.6 > room 1 000
    assert max_risk == 0.0
    assert checks[-1].status == "block" and "already reaches" in checks[-1].detail


def test_daily_limit_reached_blocks():
    checks, max_risk, _ = account_checks(acc(equity=9_500.0, day_start_equity=10_000.0, floor=8_000.0), 0.0, P)
    assert max_risk == 0.0
    assert checks[-1].name == "account.daily" and checks[-1].status == "block"


def test_daily_limit_partially_used():
    _, max_risk, _ = account_checks(acc(equity=9_600.0, day_start_equity=10_000.0, floor=8_000.0), 0.0, P)
    assert max_risk == pytest.approx(100 / 1.43)


def test_consistency_rule_blocks_at_40_percent_of_target():
    checks, max_risk, _ = account_checks(acc(equity=10_400.0), 0.0, P)
    assert max_risk == 0.0
    assert checks[-1].name == "account.consistency" and checks[-1].status == "block"


def test_consistency_rule_halves_near_the_cap():
    checks, max_risk, mult = account_checks(acc(equity=10_330.0), 0.0, P)
    assert mult == 0.5 and max_risk > 0
    assert any(c.name == "account.consistency" and c.status == "cut" for c in checks)


def test_no_target_switches_consistency_off():
    checks, _, mult = account_checks(acc(profit_target=None), 0.0, P)
    assert mult == 1.0
    assert any(c.name == "account.consistency" and c.status == "info" for c in checks)


def test_zero_equity_fails_closed():
    checks, max_risk, _ = account_checks(acc(equity=0.0), 0.0, P)
    assert max_risk == 0.0 and checks[0].status == "block"


def test_account_roundtrip_and_gross():
    a = acc(holdings=[Holding("TSLAon", "TSLA", 1_000.0, 0.02), Holding("NVDAB", "NVDA", -500.0)])
    b = AccountState.from_dict(a.to_dict())
    assert b.gross_exposure == 1_500.0
    assert b.holdings[0].stop_pct == 0.02
    assert demo_account().daily_loss_limit == 500.0
