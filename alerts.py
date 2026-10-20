"""
Formatting what gets sent: setup alerts, the copy-ready order block,
the plan math and the timestamp footer.

Split out of standalone_monitor.py (2026-10-21), which re-exports every
name here: standalone_monitor.X keeps working for bot.py, backtest.py and
the tests.
"""
from datetime import datetime, timedelta
import math
import risk
from gates import CAPITAL, LEVERAGE, RISK_PCT


SL_OFFSET = timedelta(hours=5, minutes=30)


def footer(now_utc: datetime) -> str:
    sl = now_utc + SL_OFFSET
    next_close_hour = ((now_utc.hour // 4) + 1) * 4
    next_close = now_utc.replace(minute=0, second=0, microsecond=0)
    if next_close_hour >= 24:
        next_close = next_close.replace(hour=0) + timedelta(days=1)
    else:
        next_close = next_close.replace(hour=next_close_hour)
    mins_left = int((next_close - now_utc).total_seconds() // 60)
    sl_note = " (+1d)" if sl.date() != now_utc.date() else ""
    return (
        f"UTC {now_utc.strftime('%H:%M')} | "
        f"SL {sl.strftime('%H:%M')}{sl_note} (UTC+5:30) | "
        f"next 4H close ~{mins_left} min"
    )


# Binance quantity step per symbol (the LOT_SIZE filter). Refreshed from
# exchangeInfo every run; these values only stand in if that call fails.
LOT_STEPS = {"BTCUSDT": 0.001, "ETHUSDT": 0.001, "BNBUSDT": 0.01,
             "SOLUSDT": 0.01, "XRPUSDT": 0.1}   # verified live 2026-10-21


def _fmt_qty(qty: float, step: float) -> str:
    decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return f"{qty:.{decimals}f}"


def order_ticket(symbol: str, direction: str, entry: float, stop: float,
                 raw_qty: float, mode: str) -> str:
    """The numbers to type into Binance, rounded to the symbol's quantity
    step. Rounded DOWN so risk never exceeds the plan, unless that would fall
    under Binance's $100 minimum, in which case up to the minimum - and the
    risk line shows what that costs."""
    step = LOT_STEPS.get(symbol, 0.001)
    qty = math.floor(raw_qty / step + 1e-9) * step
    if qty * entry < risk.MIN_NOTIONAL_USD:
        qty = math.ceil(risk.MIN_NOTIONAL_USD / entry / step - 1e-9) * step
    dist = abs(entry - stop)
    long = direction == "long"
    tp = round(entry + 2 * dist if long else entry - 2 * dist, 6)
    lines = [f"ORDER - {'BUY / LONG' if long else 'SELL / SHORT'} {symbol}",
             f"Qty {_fmt_qty(qty, step)}  (~${qty * entry:,.0f})",
             f"Stop-market {stop}"]
    if mode == "continuation":
        half = math.floor(qty / 2 / step + 1e-9) * step
        if half >= step:
            lines.append(f"Take-profit {tp} on HALF ({_fmt_qty(half, step)}), trail the rest")
        else:
            lines.append(f"Take-profit {tp} - qty too small to split, so full TP or trail all")
    else:
        lines.append(f"Take-profit {tp} on the FULL qty")
    lines.append(f"Risk if stopped: ${qty * dist:.2f}")
    return "\n".join(lines)


def format_setup_alert(symbol: str, r: dict, setup: dict, now_utc: datetime) -> str:
    """The message Chamara actually wants: what triggered, the numbers, and
    the risk-management levels — not just 'a level moved'."""
    entry, stop = setup["entry"], setup["stop"]
    d = setup["direction"].upper()
    try:
        m = risk.position_size(entry=entry, stop_loss=stop, capital=CAPITAL,
                                risk_pct=RISK_PCT, leverage=LEVERAGE)
    except Exception as e:
        return f"SETUP READY {symbol} {d} @ {entry} / stop {stop} (math failed: {e})"

    verdict = "PASS" if m["passes_plan"] else "FAIL"
    warn = ""
    if m.get("warnings"):
        warn = "\nWarnings: " + "; ".join(str(w) for w in m["warnings"])

    # 1R from entry — where the stop moves to break even.
    r_dist = abs(entry - stop)
    breakeven = round(entry + r_dist, 2) if d == "LONG" else round(entry - r_dist, 2)

    return (
        f"SETUP READY — {symbol} {d}\n"
        f"All gates passed ({setup['kill_zone']} kill zone).\n\n"
        f"{order_ticket(symbol, setup['direction'], entry, stop, m['position']['size_btc'], 'reversal')}\n\n"
        f"Entry {entry}  |  Stop {stop}  |  Plan check: {verdict}\n"
        f"Zone {setup['zone']['bottom']}-{setup['zone']['top']}\n"
        f"Size {m['position']['size_btc']} (${m['position']['notional_usd']}), "
        f"margin@{LEVERAGE}x ${m['position']['margin_by_leverage_usd'][f'{LEVERAGE}x']}\n"
        f"Risk ${m['risk_amount_usd']} ({m['inputs']['risk_pct']}%), "
        f"approx liq {m['approx_liquidation_price']}\n"
        f"Targets 1:2={m['plan_target_prices']['1:2']}  "
        f"1:3={m['plan_target_prices']['1:3']}\n"
        f"Fees ~{m['fees']['pct_of_risk']}% of risk\n\n"
        f"Manage: breakeven stop at {breakeven} (1R), then take the FULL "
        f"position off at 1:2. Trailing was re-tested on corrected data over "
        f"three windows and won only one of them (it is not a money-loser — "
        f"that older claim was wrong — just not better here), so the fixed "
        f"1:2 stands. Exit on an opposing 1H CHoCH rather "
        f"than waiting for the stop.\n"
        f"1D {r['trend_1d']} / 4H {r['trend_4h']} — aligned.\n"
        f"{warn}\n"
        f"Math only, NOT a recommendation. You decide.\n"
        f"{footer(now_utc)}"
    )


def format_continuation_alert(symbol: str, setup: dict, now_utc: datetime) -> str:
    """The continuation-mode counterpart to format_setup_alert. No zone to
    quote — the fresh trend-aligned 4H BOS itself is the trigger — and the
    risk % actually used can be above the plan's default 2% when the
    structurally-correct stop is wide enough to fall under Binance's $100
    minimum at 2% (Chamara's call, 2026-08-29: size to the minimum instead
    of shrinking the stop or skipping the trade)."""
    entry, stop = setup["entry"], setup["stop"]
    d = setup["direction"].upper()
    risk_pct_used = setup.get("risk_pct_used", RISK_PCT)
    try:
        m = risk.position_size(entry=entry, stop_loss=stop, capital=CAPITAL,
                                risk_pct=risk_pct_used, leverage=LEVERAGE)
    except Exception as e:
        return f"CONTINUATION SETUP {symbol} {d} @ {entry} / stop {stop} (math failed: {e})"

    verdict = "PASS" if m["passes_plan"] else "FAIL"
    warn = ""
    if m.get("warnings"):
        warn = "\nWarnings: " + "; ".join(str(w) for w in m["warnings"])
    risk_note = (
        f" (above the default {RISK_PCT}% — sized up to clear Binance's "
        f"${risk.MIN_NOTIONAL_USD:.0f} minimum at this stop width)"
        if risk_pct_used > RISK_PCT else ""
    )

    r_dist = abs(entry - stop)
    breakeven = round(entry + r_dist, 2) if d == "LONG" else round(entry - r_dist, 2)
    ev = setup.get("event") or {}

    return (
        f"CONTINUATION SETUP — {symbol} {d}\n"
        f"Fresh {ev.get('type', 'BOS')} at {ev.get('level')} ({ev.get('time')}), "
        f"same direction as the 1D trend — entering on the break, no pullback wait.\n\n"
        f"{order_ticket(symbol, setup['direction'], entry, stop, m['position']['size_btc'], 'continuation')}\n\n"
        f"Entry {entry}  |  Stop {stop} (breakout candle's own low/high)  |  "
        f"Plan check: {verdict}\n"
        f"Size {m['position']['size_btc']} (${m['position']['notional_usd']}), "
        f"margin@{LEVERAGE}x ${m['position']['margin_by_leverage_usd'][f'{LEVERAGE}x']}\n"
        f"Risk ${m['risk_amount_usd']} ({risk_pct_used}%){risk_note}, "
        f"approx liq {m['approx_liquidation_price']}\n"
        f"Targets 1:2={m['plan_target_prices']['1:2']}  "
        f"1:3={m['plan_target_prices']['1:3']}\n"
        f"Fees ~{m['fees']['pct_of_risk']}% of risk\n\n"
        f"Manage: breakeven stop at {breakeven} (1R), then trail behind each "
        f"new 4H swing — the default, but NOT unconditional. On corrected "
        f"out-of-sample data trailing beats a fixed 1:2 overall (+128R vs "
        f"+49R across two windows) and that edge is almost entirely from "
        f"TRENDING markets; in chop the fixed 1:2 actually won (+21R vs "
        f"+13R). So if price has been chopping rather than trending, taking "
        f"the full position at 1:2 is a reasonable call, not a mistake. "
        f"Half off at 1:2 if you want something locked in early, rest "
        f"trailed; or trail the whole position with no fixed TP at all. "
        f"Decide now, don't wait until it's already in profit.\n"
        f"{warn}\n"
        f"Math only, NOT a recommendation. You decide.\n"
        f"{footer(now_utc)}"
    )


def format_math(label: str, entry: float, stop: float) -> str:
    try:
        m = risk.position_size(entry=entry, stop_loss=stop, capital=200,
                                risk_pct=2, leverage=5)
        verdict = "PASS" if m["passes_plan"] else "FAIL"
        return (
            f"    {label} math (plan check, NOT a recommendation): "
            f"{m['direction'].upper()} @ {entry} / stop {stop} -> {verdict}; "
            f"size {m['position']['size_btc']} BTC "
            f"(${m['position']['notional_usd']}), "
            f"margin@5x ${m['position']['margin_by_leverage_usd']['5x']}, "
            f"approx liq {m['approx_liquidation_price']}, "
            f"targets 1:2={m['plan_target_prices']['1:2']} "
            f"1:3={m['plan_target_prices']['1:3']}"
        )
    except Exception as e:
        return f"    (math skipped: {e})"
