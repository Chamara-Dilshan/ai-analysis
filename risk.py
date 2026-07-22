"""
Pure-Python position sizing & risk math for Chamara's $100 BTC plan.

No I/O, no third-party deps — every rule here is an explicit encoding of the
trading plan (Docs/02_trading_plan.md, Section 2 & 4) so the numbers are
computed the same way every time instead of by hand in chat. Unit-tested in
tests/test_risk.py.

Key teaching point baked into the output: leverage changes only your MARGIN and
liquidation price — never your risk. Risk is fixed by (position size × stop
distance). That is the single thing the plan flags as a weakness.
"""
from typing import Optional

# --- Plan constants (Docs/02_trading_plan.md) --- #
MAX_RISK_PCT = 2.0          # hard cap: never risk more than 2% ($2 on $100)
MAX_LEVERAGE = 5.0          # hard cap: never above 5x
MIN_RR = 2.0                # minimum 1:2 reward:risk

# --- Binance BTCUSDT USDⓈ-M perpetual exchange filters (verify; they change) --- #
# NOTE: the $100 min-notional floor is stricter than the ~$65 figure in the docs
# and is the real binding constraint on a $100 account.
MIN_QTY_BTC = 0.001
MIN_NOTIONAL_USD = 100.0
DEFAULT_TAKER_FEE_PCT = 0.05   # per side (Binance USDⓈ-M taker, no BNB discount)

# Fees eating more than this share of the $ risk = the edge is being taxed away.
FEE_DRAG_WARN_PCT = 25.0


def position_size(
    entry: float,
    stop_loss: float,
    take_profit: Optional[float] = None,
    capital: float = 100.0,
    risk_pct: float = 1.0,
    leverage: float = 3.0,
    taker_fee_pct: float = DEFAULT_TAKER_FEE_PCT,
) -> dict:
    """
    Compute a plan-compliant position from an entry, a stop, and (optionally) a
    target. Returns sizing, margin, fees, an approximate liquidation price,
    R:R, the plan's tiered target prices, and a list of rule warnings.

    Direction is inferred: stop below entry = long, stop above entry = short.
    """
    if entry <= 0 or stop_loss <= 0:
        raise ValueError("entry and stop_loss must be positive prices.")
    if entry == stop_loss:
        raise ValueError("entry and stop_loss cannot be equal (zero stop distance).")
    if capital <= 0:
        raise ValueError("capital must be positive.")
    if risk_pct <= 0:
        raise ValueError("risk_pct must be positive.")
    if leverage < 1:
        raise ValueError("leverage must be >= 1.")

    direction = "long" if stop_loss < entry else "short"

    sl_distance = abs(entry - stop_loss)
    sl_distance_pct = sl_distance / entry * 100
    risk_amount = capital * risk_pct / 100.0

    # Core plan formula: Position Value = risk$ / stop-distance%.
    notional = risk_amount / (sl_distance_pct / 100.0)
    size_btc = notional / entry
    margin_required = notional / leverage

    # Round-trip taker fees (entry + exit).
    roundtrip_fee_frac = 2 * taker_fee_pct / 100.0
    fee_usd = notional * roundtrip_fee_frac
    fee_pct_of_risk = (fee_usd / risk_amount * 100.0) if risk_amount else 0.0

    # Approximate isolated-margin liquidation (excludes maintenance margin & fees).
    if direction == "long":
        approx_liq = entry * (1 - 1 / leverage)
    else:
        approx_liq = entry * (1 + 1 / leverage)

    # Plan's tiered target prices, projected off the stop distance (1R).
    plan_targets = {}
    for label, mult in (("1:2", 2), ("1:3", 3), ("1:4", 4)):
        if direction == "long":
            plan_targets[label] = round(entry + mult * sl_distance, 2)
        else:
            plan_targets[label] = round(entry - mult * sl_distance, 2)

    # Reward:risk on the supplied target, if any.
    rr: Optional[float] = None
    tp_side_ok = True
    if take_profit is not None and take_profit > 0:
        rr = abs(take_profit - entry) / sl_distance
        if direction == "long" and take_profit <= entry:
            tp_side_ok = False
        if direction == "short" and take_profit >= entry:
            tp_side_ok = False

    # --- Rule checks (each is an actionable, plain-language stop) --- #
    warnings: list = []
    if risk_pct > MAX_RISK_PCT:
        warnings.append(
            f"Risk {risk_pct}% exceeds your {MAX_RISK_PCT}% hard cap "
            f"(${round(risk_amount, 2)} vs ${round(capital * MAX_RISK_PCT / 100, 2)} max)."
        )
    if leverage > MAX_LEVERAGE:
        warnings.append(f"Leverage {leverage}x exceeds your {MAX_LEVERAGE}x cap.")
    if size_btc < MIN_QTY_BTC or notional < MIN_NOTIONAL_USD:
        warnings.append(
            f"Position (~{round(size_btc, 6)} BTC / ${round(notional, 2)}) is below "
            f"Binance's minimum ({MIN_QTY_BTC} BTC / ${MIN_NOTIONAL_USD}). To place it "
            "you'd have to break your risk rule — the plan says SKIP this trade."
        )
    sl_beyond_liq = (
        (direction == "long" and stop_loss <= approx_liq)
        or (direction == "short" and stop_loss >= approx_liq)
    )
    if sl_beyond_liq:
        warnings.append(
            "CRITICAL: your stop is at/beyond the approximate liquidation price — "
            "you'd be liquidated before the stop is hit. Lower leverage or tighten the stop."
        )
    if rr is not None and tp_side_ok and rr < MIN_RR:
        warnings.append(f"Reward:risk {rr:.2f} is below your {MIN_RR:.0f}:1 minimum.")
    if not tp_side_ok:
        warnings.append(
            f"Take-profit ${take_profit} is on the wrong side of entry for a {direction}."
        )
    if fee_pct_of_risk > FEE_DRAG_WARN_PCT:
        warnings.append(
            f"Fees (~${fee_usd:.2f}) eat {fee_pct_of_risk:.0f}% of your "
            f"${risk_amount:.2f} risk — the stop may be too tight for this account."
        )

    passes = not warnings

    return {
        "direction": direction,
        "inputs": {
            "entry": entry,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "capital_usd": capital,
            "risk_pct": risk_pct,
            "leverage": leverage,
        },
        "risk_amount_usd": round(risk_amount, 2),
        "stop_distance_pct": round(sl_distance_pct, 3),
        "position": {
            "notional_usd": round(notional, 2),
            "size_btc": round(size_btc, 6),
            "margin_required_usd": round(margin_required, 2),
            "margin_by_leverage_usd": {
                "2x": round(notional / 2, 2),
                "3x": round(notional / 3, 2),
                "5x": round(notional / 5, 2),
            },
        },
        "reward_risk_ratio": round(rr, 2) if rr is not None else None,
        "plan_target_prices": plan_targets,
        "fees": {
            "est_roundtrip_usd": round(fee_usd, 3),
            "pct_of_risk": round(fee_pct_of_risk, 1),
            "taker_fee_pct_per_side": taker_fee_pct,
        },
        "approx_liquidation_price": round(approx_liq, 2),
        "liquidation_note": (
            "Approximate (isolated margin, excludes maintenance margin & fees). "
            "Always confirm the real liquidation price in Binance's order form."
        ),
        "key_point": (
            "Leverage changes only your margin and liquidation price — NOT your "
            f"risk. Your ${round(risk_amount, 2)} risk is fixed by the stop distance "
            "and position size."
        ),
        "passes_plan": passes,
        "warnings": warnings,
        "verdict": (
            "Sizing respects your plan — you still decide whether to take it."
            if passes else
            "⚠️ Fix the warnings before trading. On $100, when the math "
            "doesn't fit cleanly, the disciplined move is usually to skip."
        ),
        "rules_checked": (
            "risk ≤ 2%, leverage ≤ 5x, R:R ≥ 2:1, size ≥ Binance "
            "minimum, stop inside liquidation, fee drag."
        ),
        "disclaimer": (
            "Position-sizing math only — not a signal or advice. You read the "
            "setup and decide; capital protection comes first."
        ),
    }
