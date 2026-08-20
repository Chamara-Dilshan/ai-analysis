"""
Deterministic unit tests for risk.py — position sizing math and rule checks.
Pure logic, no I/O. Numbers are hand-checkable against Docs/02_trading_plan.md.
"""
import pytest

import risk as R


def test_long_sizing_basic():
    # $200, risk 1% ($2), stop 0.5% away -> $400 notional.
    out = R.position_size(entry=65000, stop_loss=64675, capital=200,
                          risk_pct=1, leverage=3)
    assert out["direction"] == "long"
    assert out["risk_amount_usd"] == 2.0
    assert out["stop_distance_pct"] == 0.5
    assert out["position"]["notional_usd"] == 400.0
    assert out["position"]["size_btc"] == round(400 / 65000, 6)
    assert out["position"]["margin_required_usd"] == round(400 / 3, 2)
    assert out["passes_plan"] is True
    assert out["warnings"] == []


def test_short_direction_inferred():
    out = R.position_size(entry=65000, stop_loss=65325, capital=200, risk_pct=1)
    assert out["direction"] == "short"
    assert out["plan_target_prices"]["1:2"] == 65000 - 2 * 325


def test_plan_targets_long():
    out = R.position_size(entry=65000, stop_loss=64675)
    assert out["plan_target_prices"] == {
        "1:2": 65650.0, "1:3": 65975.0, "1:4": 66300.0,
    }


def test_reward_risk_ratio():
    out = R.position_size(entry=65000, stop_loss=64675, take_profit=65650)
    assert out["reward_risk_ratio"] == 2.0


def test_rr_rounding_not_self_contradictory():
    # Target $0.10 short of true 1:2 -> rr 1.9997, rounds to 2.00. The displayed
    # ratio and the pass/fail must agree (no "2.00 is below 2:1" warning).
    out = R.position_size(entry=65542.7, stop_loss=65200, take_profit=66228)
    assert out["reward_risk_ratio"] == 2.0
    assert not any("reward:risk" in w.lower() for w in out["warnings"])
    assert out["passes_plan"] is True


def test_rr_below_minimum_warns():
    out = R.position_size(entry=65000, stop_loss=64675, take_profit=65325)
    assert out["reward_risk_ratio"] == 1.0
    assert any("reward:risk" in w.lower() for w in out["warnings"])
    assert out["passes_plan"] is False


def test_take_profit_wrong_side_warns():
    # Long but TP below entry.
    out = R.position_size(entry=65000, stop_loss=64675, take_profit=64000)
    assert any("wrong side" in w.lower() for w in out["warnings"])


def test_undersized_position_warns():
    # $200 capital, 3% stop on 1% risk ($2) -> $66.67 notional, below the $100
    # min notional (Binance's own floor — doesn't move with account size).
    out = R.position_size(entry=65000, stop_loss=63050, capital=200, risk_pct=1)
    assert out["position"]["notional_usd"] == 66.67
    assert any("minimum" in w.lower() for w in out["warnings"])
    assert out["passes_plan"] is False


def test_risk_over_cap_warns():
    out = R.position_size(entry=65000, stop_loss=64675, capital=200, risk_pct=3)
    assert any("exceeds your 2.0% hard cap" in w for w in out["warnings"])
    assert out["passes_plan"] is False


def test_leverage_over_cap_warns():
    out = R.position_size(entry=65000, stop_loss=64675, leverage=10)
    assert any("exceeds your 5.0x cap" in w for w in out["warnings"])


def test_stop_beyond_liquidation_warns():
    # High leverage + a wide stop places the stop past the liq price.
    out = R.position_size(entry=65000, stop_loss=58000, capital=200,
                          risk_pct=2, leverage=10)
    assert any("liquidat" in w.lower() for w in out["warnings"])
    assert out["passes_plan"] is False


def test_leverage_does_not_change_risk():
    a = R.position_size(entry=65000, stop_loss=64675, leverage=2)
    b = R.position_size(entry=65000, stop_loss=64675, leverage=5)
    # Notional and $ risk identical; only margin differs.
    assert a["position"]["notional_usd"] == b["position"]["notional_usd"]
    assert a["risk_amount_usd"] == b["risk_amount_usd"]
    assert a["position"]["margin_required_usd"] != b["position"]["margin_required_usd"]


def test_invalid_inputs_raise():
    with pytest.raises(ValueError):
        R.position_size(entry=65000, stop_loss=65000)  # zero stop distance
    with pytest.raises(ValueError):
        R.position_size(entry=-1, stop_loss=100)
    with pytest.raises(ValueError):
        R.position_size(entry=65000, stop_loss=64000, leverage=0)
