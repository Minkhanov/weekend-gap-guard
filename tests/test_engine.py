import json
import math

import pytest
from conftest import FRI_OPEN, FRI_PRE_CLOSE, MON_PRE_OPEN, SAT_NOON, TUE_NIGHT, XMAS_SAT, make_view

from gapguard.models import AssetStatus
from gapguard.risk.account import AccountState, Holding, demo_account
from gapguard.risk.engine import DEFAULT_GAP_Q99, OrderIntent, evaluate
from gapguard.risk.policy import GuardPolicy

P = GuardPolicy()


def buy(usd=2000.0, stop=None, symbol="TSLAon"):
    return OrderIntent(symbol, "buy", usd, stop)


def check(d, name):
    return next(c for c in d.checks if c.name == name)


# ---------------------------------------------------------------------------- session phases
def test_regular_session_small_order_is_allowed():
    d = evaluate(buy(2000), make_view(FRI_OPEN), demo_account(), P)
    assert d.verdict == "ALLOW" and d.allowed_usd == 2000
    assert check(d, "risk.per_dollar").data["move"] == pytest.approx(0.03)     # priced on the stop


@pytest.mark.parametrize("at,phase,cap", [
    (FRI_PRE_CLOSE, "pre_close", 1250.0),
    (SAT_NOON, "closed", 875.0),
    (MON_PRE_OPEN, "pre_open", 1250.0),
])
def test_closure_phases_cut_the_position_cap(at, phase, cap):
    d = evaluate(buy(2000), make_view(at), demo_account(), P)
    assert d.phase == phase
    assert d.verdict == "REDUCE" and d.allowed_usd == cap and d.binding == "position.cap"


def test_weekend_risk_is_priced_on_the_gap_not_the_stop():
    d = evaluate(buy(500, stop=0.02), make_view(SAT_NOON, q99=0.07), demo_account(), P)
    r = check(d, "risk.per_dollar").data
    assert r["move"] == pytest.approx(0.07) and r["stop"] == 0.02
    assert r["worst_per_usd"] == pytest.approx(0.07 * 1.43)


def test_wider_stop_than_gap_wins():
    d = evaluate(buy(500, stop=0.12), make_view(SAT_NOON, q99=0.07), demo_account(), P)
    assert check(d, "risk.per_dollar").data["move"] == pytest.approx(0.12)


def test_gap_floor_applies_to_calm_tickers():
    d = evaluate(buy(500), make_view(SAT_NOON, q99=0.005), demo_account(), GuardPolicy(default_stop_pct=0.01))
    assert check(d, "risk.per_dollar").data["move"] == pytest.approx(0.02)


def test_missing_gap_model_uses_pessimistic_default():
    d = evaluate(buy(500), make_view(SAT_NOON, q99=None), demo_account(), P)
    assert check(d, "risk.per_dollar").data["q99"] == pytest.approx(DEFAULT_GAP_Q99)


def test_long_weekend_cuts_more_and_widens_the_gap():
    d = evaluate(buy(2000), make_view(XMAS_SAT), demo_account(), P)
    assert d.verdict == "REDUCE"
    assert d.allowed_usd == pytest.approx(612.50)          # 2 500 × 0.35 × 0.7
    assert check(d, "risk.per_dollar").data["q99"] == pytest.approx(0.08 * math.sqrt(1.25))


def test_weekday_overnight_is_ordinary_risk():
    d = evaluate(buy(2000), make_view(TUE_NIGHT), demo_account(), P)
    assert d.verdict == "ALLOW"
    assert "weekday overnight" in check(d, "session.phase").detail


# ---------------------------------------------------------------------------- fail closed
def test_no_price_blocks():
    d = evaluate(buy(), make_view(SAT_NOON, price=None), demo_account(), P)
    assert d.verdict == "BLOCK" and d.binding == "data.price" and d.allowed_usd == 0


def test_stale_quote_blocks():
    d = evaluate(buy(), make_view(FRI_OPEN, quote_age_s=3600), demo_account(), P)
    assert d.verdict == "BLOCK" and d.binding == "data.price"


def test_missing_reference_during_closure_blocks():
    d = evaluate(buy(), make_view(SAT_NOON, ref=None), demo_account(), P)
    assert d.verdict == "BLOCK" and d.binding == "data.reference"


def test_ancient_reference_blocks():
    from datetime import timedelta
    d = evaluate(buy(), make_view(SAT_NOON, ref_at=SAT_NOON - timedelta(hours=150)), demo_account(), P)
    assert d.verdict == "BLOCK" and d.binding == "data.reference"


# ---------------------------------------------------------------------------- venue status
def test_paused_asset_blocks_buys_and_sells():
    st = AssetStatus(False, "pause", "ASSET_PAUSED", "stock_split")
    assert evaluate(buy(), make_view(FRI_OPEN, status=st), demo_account(), P).verdict == "BLOCK"
    d = evaluate(OrderIntent("TSLAon", "sell", 500), make_view(FRI_OPEN, status=st), demo_account(), P)
    assert d.verdict == "BLOCK" and d.binding == "venue.status"


def test_earnings_limited_widens_gap_and_halves_size():
    st = AssetStatus(True, "closed", "ASSET_LIMITED", "earnings")
    d = evaluate(buy(2000), make_view(SAT_NOON, status=st), demo_account(), P)
    assert check(d, "venue.status").status == "cut"
    assert check(d, "risk.per_dollar").data["q99"] == pytest.approx(0.2)
    assert d.allowed_usd == pytest.approx(875 * 0.5)


# ---------------------------------------------------------------------------- stale reference
def test_buying_into_a_weekend_rally_beyond_q99_is_blocked():
    d = evaluate(buy(), make_view(SAT_NOON, price=108.0, ref=100.0), demo_account(), P)
    assert d.verdict == "BLOCK" and d.binding == "reference.drift"


def test_buying_after_a_weekend_drop_is_cut_not_blocked():
    d = evaluate(buy(2000), make_view(SAT_NOON, price=92.0, ref=100.0), demo_account(), P)
    assert d.verdict == "REDUCE" and check(d, "reference.drift").status == "cut"
    assert d.allowed_usd == pytest.approx(875 * 0.5)


def test_upward_drift_stretches_the_worst_case():
    d = evaluate(buy(500), make_view(SAT_NOON, price=102.0, ref=100.0), demo_account(), P)
    assert check(d, "risk.per_dollar").data["move"] == pytest.approx(1 - 0.92 / 1.02)
    down = evaluate(buy(500), make_view(SAT_NOON, price=98.0, ref=100.0), demo_account(), P)
    assert check(down, "risk.per_dollar").data["move"] == pytest.approx(0.08)


def test_moderate_drift_halves_and_small_drift_passes():
    assert check(evaluate(buy(), make_view(SAT_NOON, price=104.0), demo_account(), P), "reference.drift").status == "cut"
    assert check(evaluate(buy(), make_view(SAT_NOON, price=102.0), demo_account(), P), "reference.drift").status == "pass"


def test_drift_is_not_judged_while_the_market_still_trades():
    d = evaluate(buy(), make_view(FRI_PRE_CLOSE, price=106.0, ref=100.0), demo_account(), P)
    assert check(d, "reference.drift").status == "info"


def test_dispersion_block_cut_and_pass():
    blk = evaluate(buy(), make_view(SAT_NOON, price=96.0, ref=96.0, siblings={"B": 100.0, "C": 100.0}), demo_account(), P)
    assert blk.verdict == "BLOCK" and blk.binding == "reference.dispersion"
    cut = evaluate(buy(), make_view(SAT_NOON, price=98.5, ref=98.5, siblings={"B": 100.0, "C": 100.0}), demo_account(), P)
    assert check(cut, "reference.dispersion").status == "cut"
    ok = evaluate(buy(), make_view(SAT_NOON, siblings={"B": 100.2, "C": 100.1}), demo_account(), P)
    assert check(ok, "reference.dispersion").status == "pass"


def test_healthy_token_is_not_punished_for_a_broken_sibling():
    d = evaluate(buy(), make_view(SAT_NOON, siblings={"BROKEN": 60.0, "OTHER": 100.1}), demo_account(), P)
    assert check(d, "reference.dispersion").status == "pass"


def test_premium_checked_only_against_a_live_print():
    live = evaluate(buy(), make_view(FRI_OPEN, price=102.0, stock_price=100.0), demo_account(), P)
    assert check(live, "reference.premium").status == "cut"
    broken = evaluate(buy(), make_view(FRI_OPEN, price=104.0, stock_price=100.0), demo_account(), P)
    assert broken.verdict == "BLOCK" and broken.binding == "reference.premium"
    shut = evaluate(buy(), make_view(SAT_NOON, price=101.0, stock_price=100.0), demo_account(), P)
    assert check(shut, "reference.premium").status == "info"


# ---------------------------------------------------------------------------- caps
def test_gross_exposure_cap_binds():
    a = AccountState(10_000, 10_000, 9_000, 500, 1_000, holdings=[Holding("NVDAon", "NVDA", 9_000, 0.01)])
    d = evaluate(buy(2000), make_view(FRI_OPEN), a, P)
    assert d.verdict == "REDUCE" and d.binding == "exposure.gross" and d.allowed_usd == 1000


def test_issuer_limit_binds():
    d = evaluate(buy(2000), make_view(FRI_OPEN, limit=1500.0), demo_account(), P)
    assert d.verdict == "REDUCE" and d.binding == "issuer.limit" and d.allowed_usd == 1500


def test_account_guard_binds_with_exact_size():
    a = AccountState(10_000, 10_400, 9_000, 500, None)      # drawdown 400 of 500 → 100 / 1.43 room
    d = evaluate(buy(2400), make_view(FRI_OPEN), a, P)
    assert d.verdict == "REDUCE" and d.binding == "account"
    assert d.allowed_usd == pytest.approx(2331.00)


def test_weekend_holdings_are_repriced_on_the_gap():
    a = AccountState(10_000, 10_000, 9_000, 500, None, holdings=[Holding("NVDAon", "NVDA", 4_000, 0.01)])
    # weekday: 4 000 × 1 % = 40 planned; weekend: 4 000 × q99 8 % = 320 planned
    wk = evaluate(buy(500), make_view(SAT_NOON), a, P, gap_by_ticker={"NVDA": 0.08})
    assert wk.metrics["open_planned_risk"] == pytest.approx(320)
    assert wk.verdict == "REDUCE" and wk.binding == "account"


def test_below_minimum_order_blocks():
    a = AccountState(10_000, 10_000, 9_000, 500, None, holdings=[Holding("NVDAon", "NVDA", 9_995, 0.001)])
    d = evaluate(buy(2000), make_view(FRI_OPEN), a, P)
    assert d.verdict == "BLOCK" and d.allowed_usd == 0 and "minimum" in d.summary


def test_account_block_propagates():
    a = AccountState(10_400, 10_000, 9_000, 500, 1_000)      # day profit 400 = 40 % of target
    d = evaluate(buy(100), make_view(FRI_OPEN), a, P)
    assert d.verdict == "BLOCK" and d.binding == "account.consistency"


# ---------------------------------------------------------------------------- sells, output
def test_sells_are_never_sized_down():
    a = AccountState(9_001, 10_000, 9_000, 500, 1_000)       # nearly at the floor, daily limit almost gone
    d = evaluate(OrderIntent("TSLAon", "sell", 5_000), make_view(SAT_NOON, price=95.0), a, P)
    assert d.verdict == "ALLOW" and d.allowed_usd == 5_000
    assert "weekend price" in d.summary


def test_order_validation():
    with pytest.raises(ValueError):
        OrderIntent("X", "short", 100)
    with pytest.raises(ValueError):
        OrderIntent("X", "buy", -1)
    with pytest.raises(ValueError):
        OrderIntent("X", "buy", 100, stop_pct=3)


def test_decision_serialises_and_renders():
    d = evaluate(buy(2000), make_view(SAT_NOON, siblings={"TSLAB": 100.1}), demo_account(), P)
    blob = json.dumps(d.to_dict())
    assert '"verdict": "REDUCE"' in blob and "checks" in blob
    text = d.render()
    assert text.startswith("[REDUCE]") and "position.cap" in text
    assert d.size_ratio == pytest.approx(875 / 2000)


def test_policy_overrides():
    p = GuardPolicy.from_dict({"phase_size_mult": {"closed": 0.1}, "max_position_pct": 0.5})
    assert p.size_mult("closed") == 0.1 and p.size_mult("open") == 1.0
    d = evaluate(buy(2000), make_view(SAT_NOON), demo_account(), p)
    assert d.allowed_usd == 500
    with pytest.raises(ValueError):
        GuardPolicy.from_dict({"no_such_knob": 1})
