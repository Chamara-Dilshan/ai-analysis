"""backtest.py — replay standalone_monitor's setup gates over history.

Answers the question the live monitor cannot: does SETUP READY ever fire, how
often, and would those setups have made money? A signal generator that has
never produced a signal is not a strategy, it is a hypothesis — and this
morning's zone-width bug proved the failure mode is silent.

Method: walk forward one 1H candle at a time. At each step, rebuild exactly
the structure the monitor would have seen using only candles up to that
moment, run the same evaluate_setup(), and when it fires, simulate the trade
forward to either the 1:2 target or the stop.

Honest limits:
  - News blackout is not replayed (the calendar feed is this-week only), so
    live will fire slightly LESS often than this reports.
  - Fills are assumed at the 1H close, with no slippage.
  - If a candle spans both stop and target, the stop is assumed first.
  - One position at a time per symbol, which is how the plan trades.

Usage:
    python backtest.py                 # 6 months, both symbols
    python backtest.py --months 12
    python backtest.py --symbol BTCUSDT
    python backtest.py --mode continuation --exit trailing
"""
import argparse
import asyncio
import bisect
from datetime import datetime, timedelta, timezone

import server
import structure
import standalone_monitor as mon

MS_PER_HOUR = 3_600_000
BINANCE_MAX = 1500


async def fetch_history(symbol: str, interval: str, start_ms: int, end_ms: int):
    """Paginated kline fetch. server._fetch_klines caps at 500 with no date
    range, which is fine live and useless for a backtest."""
    out, cursor = [], start_ms
    while cursor < end_ms:
        raw = await server._get(
            server.BINANCE_BASE_URL, "/fapi/v1/klines",
            {"symbol": symbol, "interval": interval, "startTime": cursor,
             "endTime": end_ms, "limit": BINANCE_MAX},
            cache_ttl=0,
        )
        if not raw:
            break
        out.extend(raw)
        cursor = raw[-1][0] + 1
        if len(raw) < BINANCE_MAX:
            break
    # open_time must be the same ISO string server._fetch_klines produces —
    # structure events inherit it, and evaluate_setup parses those with
    # fromisoformat. Feeding raw milliseconds makes every freshness check
    # throw and silently forces the trigger gate false.
    candles = [
        {"open_time": server._iso_ms(c[0]), "open": float(c[1]),
         "high": float(c[2]), "low": float(c[3]), "close": float(c[4]),
         "volume": float(c[5])}
        for c in out
    ]
    times_ms = [c[0] for c in out]
    return candles, times_ms


def simulate(candles_1h, start_idx, direction, entry, stop, max_hours=240):
    """Walk forward from the bar after entry to the first of stop or 1:2.

    Returns (outcome, r_multiple, hours_held). Unresolved trades are reported
    separately rather than silently counted as wins — an open trade is not a
    profitable one.
    """
    risk = abs(entry - stop)
    target = entry + 2 * risk if direction == "long" else entry - 2 * risk
    for offset in range(1, max_hours + 1):
        i = start_idx + offset
        if i >= len(candles_1h):
            break
        c = candles_1h[i]
        if direction == "long":
            hit_stop, hit_target = c["low"] <= stop, c["high"] >= target
        else:
            hit_stop, hit_target = c["high"] >= stop, c["low"] <= target
        # Pessimistic when a single candle covers both.
        if hit_stop:
            return "loss", -1.0, offset
        if hit_target:
            return "win", 2.0, offset
    return "open", 0.0, max_hours


def simulate_trailing(candles_4h, start_idx, direction, entry, stop,
                       swing_strength=2, max_candles=120):
    """4H-native alternative to simulate()'s fixed 1:2 exit, added 2026-08-29
    to test whether trailing beats a hard cap for continuation trades — the
    mode built specifically to catch big trends, which a fixed 1:2 works
    against. Real precedent: the 2026-08-12 ETH long closed at 2R when 6R+
    was briefly available, purely because only one fixed TP existed.

    Policy (matches the plan's documented management rules in
    manage_position(), just executed mechanically instead of alerted):
      - stop to breakeven once price reaches 1R
      - half the position locked at 1:2 (0.5R contribution, fixed)
      - the remaining half trails behind the most recent CONFIRMED swing in
        the trade's favor (new higher low for a long, lower high for a
        short) — never moved backward — until it is stopped out or
        max_candles runs out

    Swing detection only ever looks at candles up to the current step (no
    look-ahead). Returns (outcome, r_multiple, candles_held) — r_multiple is
    already the blended total (locked half + trailed half, or the full
    position if it never reached 1:2).
    """
    risk = abs(entry - stop)
    if risk <= 0:
        return "open", 0.0, 0
    long = direction == "long"
    target_2r = entry + 2 * risk if long else entry - 2 * risk
    current_stop = stop
    breakeven_done = partial_done = False
    partial_r = 0.0

    def r_at(price):
        return (price - entry) / risk if long else (entry - price) / risk

    last_idx = min(start_idx + max_candles, len(candles_4h) - 1)
    for offset in range(1, max_candles + 1):
        i = start_idx + offset
        if i >= len(candles_4h):
            break
        c = candles_4h[i]
        hit_stop = c["low"] <= current_stop if long else c["high"] >= current_stop
        hit_2r = c["high"] >= target_2r if long else c["low"] <= target_2r

        # Pessimistic when one candle covers both, same convention as simulate().
        if hit_stop:
            total = partial_r + (0.5 if partial_done else 1.0) * r_at(current_stop)
            return ("win" if total > 0 else "loss"), total, offset

        if hit_2r and not partial_done:
            partial_done = True
            partial_r = 0.5 * 2.0
            current_stop = entry
            breakeven_done = True
        elif not breakeven_done:
            hit_1r = c["high"] >= entry + risk if long else c["low"] <= entry - risk
            if hit_1r:
                current_stop = entry
                breakeven_done = True

        # Trail behind the latest swing confirmed using only history up to
        # here — find_swings needs `swing_strength` candles either side, so
        # anything from the last few candles genuinely isn't confirmable yet.
        seen = candles_4h[max(0, i - 199):i + 1]
        swings = structure.find_swings(seen, swing_strength, swing_strength)
        pivots = [s["price"] for s in swings if s["type"] == ("low" if long else "high")]
        if pivots:
            candidate = pivots[-1]
            if (long and candidate > current_stop) or (not long and candidate < current_stop):
                current_stop = candidate

    # Ran out of data or hit max_candles — still open, valued at the last close.
    last_close = candles_4h[last_idx]["close"]
    total = partial_r + (0.5 if partial_done else 1.0) * r_at(last_close)
    return "open", total, min(max_candles, last_idx - start_idx)


async def backtest(symbol: str, months: int, start_date=None, end_date=None,
                    mode: str = "reversal", exit_style: str = "fixed"):
    """start_date/end_date make an out-of-sample run possible: the tuning was
    done on the most recent 18 months, so testing on an earlier window is the
    only way to tell a real edge from a fitted one.

    mode="reversal" replays evaluate_setup (pullback into a demand/supply
    zone, tight stop). mode="continuation" replays
    evaluate_trend_continuation_setup (enter on a fresh trend-aligned 4H BOS,
    wider stop at the breakout candle's own low/high, sized to Binance's
    minimum rather than a fixed 2%) — added 2026-08-29 to test whether it
    would have caught the 2026-08-19->08-28 rally that evaluate_setup missed.
    """
    if start_date and end_date:
        start = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
        end = datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc)
    else:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=30 * months)
    # Warm-up so the very first evaluation has as much history behind it as
    # the live monitor would.
    fetch_start = start - timedelta(days=60)
    s_ms, e_ms = int(fetch_start.timestamp() * 1000), int(end.timestamp() * 1000)

    c1h, t1h = await fetch_history(symbol, "1h", s_ms, e_ms)
    c4h, t4h = await fetch_history(symbol, "4h", s_ms, e_ms)
    c1d, t1d = await fetch_history(symbol, "1d", s_ms, e_ms)
    print(f"{symbol}: {len(c1h)} 1H, {len(c4h)} 4H, {len(c1d)} 1D candles")

    begin_ms = int(start.timestamp() * 1000)

    trades, gate_hits = [], {}
    busy_until = -1
    cached_4h_idx = cached_1d_idx = -1
    st4h = st1d = None
    c4h_slice = []  # the exact slice st4h was built from — event["index"] indexes into this

    for i, bar in enumerate(c1h):
        if t1h[i] < begin_ms or i < 200:
            continue
        if i <= busy_until:
            continue

        now = datetime.fromtimestamp(t1h[i] / 1000, tz=timezone.utc)

        # Higher-timeframe structure only changes when its candle changes, so
        # recompute on the boundary instead of every hour.
        i4 = bisect.bisect_right(t4h, t1h[i]) - 1
        i1 = bisect.bisect_right(t1d, t1h[i]) - 1
        if i4 < 20 or i1 < 20:
            continue
        if i4 != cached_4h_idx:
            c4h_slice = c4h[max(0, i4 - 199):i4 + 1]
            st4h = structure.analyze(c4h_slice, swing_strength=3)
            cached_4h_idx = i4
        if i1 != cached_1d_idx:
            st1d = structure.analyze(c1d[max(0, i1 - 179):i1 + 1], swing_strength=2)
            cached_1d_idx = i1

        price = bar["close"]

        if mode == "continuation":
            setup = mon.evaluate_trend_continuation_setup(
                st1d.get("trend"), mon.latest_event(st4h), c4h_slice, price, now)
        else:
            st1h = structure.analyze(c1h[max(0, i - 199):i + 1], swing_strength=2)
            r = {
                "price": price,
                "trend_1d": st1d.get("trend"),
                "trend_4h": st4h.get("trend"),
                "demand": mon.pick_demand_zone(st4h, price),
                "supply": mon.pick_supply_zone(st4h, price),
                "event_1h": mon.latest_event(st1h),
                "bar_low": bar["low"],
                "bar_high": bar["high"],
            }
            setup = mon.evaluate_setup(r, now)

        for g, ok in setup["gates"].items():
            gate_hits[g] = gate_hits.get(g, 0) + (1 if ok else 0)

        if setup["ready"]:
            if mode == "continuation" and exit_style == "trailing":
                # event["index"] is relative to c4h_slice, not the full c4h
                # list — map it back so simulate_trailing walks real candles.
                ev = setup["event"]
                full_breakout_idx = max(0, i4 - 199) + ev["index"]
                outcome, rr, candles_held = simulate_trailing(
                    c4h, full_breakout_idx, setup["direction"],
                    setup["entry"], setup["stop"])
                hours = candles_held * 4  # simulate_trailing walks 4H candles
            else:
                outcome, rr, hours = simulate(
                    c1h, i, setup["direction"], setup["entry"], setup["stop"])
            # Charge the round trip. Reporting gross R flatters every result;
            # on a $200 account with a 2% risk the fee is a real slice of it.
            fee_r = (setup.get("fee_pct") or 0.0) / 100.0
            net_r = rr - fee_r if outcome != "open" else 0.0
            risk_pct_used = setup.get("risk_pct_used", mon.RISK_PCT)
            trades.append({
                "time": now.strftime("%Y-%m-%d %H:%M"),
                "direction": setup["direction"], "entry": setup["entry"],
                "stop": setup["stop"], "outcome": outcome,
                "r": net_r,
                "gross_r": rr, "fee_r": fee_r, "hours": hours,
                "session": mon.active_kill_zone(now),
                "zone_kind": (setup.get("zone") or {}).get("kind") if mode != "continuation" else "continuation",
                # Where in the 4H range the entry happened. Low = deep
                # discount for a long; high = deep premium for a short.
                "range_pct": (st4h.get("premium_discount") or {}).get("position_pct"),
                "weekday": now.strftime("%a"),
                "risk_pct_used": risk_pct_used,
                # Actual $ at Chamara's $200 capital and the risk % this
                # specific trade used — for continuation trades that can be
                # above the default 2% (see evaluate_trend_continuation_setup).
                "usd": net_r * mon.CAPITAL * risk_pct_used / 100,
            })
            busy_until = i + hours

    return trades, gate_hits, sum(1 for t in t1h if t >= begin_ms)


def breakdown(trades, key, label):
    """Slice resolved trades by one attribute. Pure diagnosis of data we
    already have — no new parameters, so no new chance to overfit."""
    done = [t for t in trades if t["outcome"] in ("win", "loss")]
    if not done:
        return
    buckets = {}
    for t in done:
        buckets.setdefault(t.get(key), []).append(t)
    print(f"\n  by {label}:")
    for name, group in sorted(buckets.items(), key=lambda kv: str(kv[0])):
        w = sum(1 for t in group if t["outcome"] == "win")
        total = sum(t["r"] for t in group)
        print(f"    {str(name):12s} n={len(group):3d}  win {100*w/len(group):5.1f}%  "
              f"{total:+6.1f}R  exp {total/len(group):+.2f}R")


def depth_bucket(t):
    """Coarse buckets for how far into the range the entry sat."""
    v = t.get("range_pct")
    if v is None:
        return "unknown"
    if v < 25:
        return "deep-disc"
    if v < 50:
        return "discount"
    if v < 75:
        return "premium"
    return "deep-prem"


def report(symbol, trades, gate_hits, bars):
    print(f"\n{'=' * 58}\n{symbol} — {bars} hourly evaluations\n{'=' * 58}")
    print("Gate pass rate (share of hours each gate was satisfied):")
    for g, n in sorted(gate_hits.items(), key=lambda kv: -kv[1]):
        print(f"  {g:12s} {n:6d}  {100 * n / max(bars, 1):5.1f}%")

    if not trades:
        print("\nNO SETUPS FIRED. The gates never aligned.")
        return

    wins = [t for t in trades if t["outcome"] == "win"]
    losses = [t for t in trades if t["outcome"] == "loss"]
    opens = [t for t in trades if t["outcome"] == "open"]
    resolved = len(wins) + len(losses)
    total_r = sum(t["r"] for t in trades)

    print(f"\nSetups fired: {len(trades)}  "
          f"(~1 every {bars / len(trades):.0f} hours)")
    print(f"  wins {len(wins)} | losses {len(losses)} | unresolved {len(opens)}")
    if resolved:
        total_usd = sum(t.get("usd", t["r"] * 4) for t in trades if t["outcome"] != "open")
        avg_risk_pct = sum(t.get("risk_pct_used", 2.0) for t in trades) / len(trades)
        print(f"  win rate {100 * len(wins) / resolved:.1f}%")
        print(f"  total {total_r:+.1f}R, expectancy {total_r / resolved:+.2f}R/trade")
        print(f"  on $200, avg {avg_risk_pct:.2f}% risk/trade: {total_usd:+.2f} USD "
              f"(uses each trade's actual risk %, not a flat 2%)")
    for t in trades:
        t["depth"] = depth_bucket(t)
    breakdown(trades, "direction", "direction")
    breakdown(trades, "session", "session")
    breakdown(trades, "zone_kind", "zone type")
    breakdown(trades, "depth", "entry depth in 4H range")

    print("\n  date              dir    entry      stop    result   hrs")
    for t in trades[-5:]:
        print(f"  {t['time']}  {t['direction']:5s} {t['entry']:9.1f} "
              f"{t['stop']:9.1f}  {t['outcome']:7s} {t['hours']:4d}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=6)
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--start", default=None, help="YYYY-MM-DD, out-of-sample")
    ap.add_argument("--end", default=None, help="YYYY-MM-DD")
    ap.add_argument("--mode", default="reversal", choices=["reversal", "continuation"],
                     help="reversal = evaluate_setup (pullback into a zone); "
                          "continuation = evaluate_trend_continuation_setup "
                          "(enter on a fresh trend-aligned 4H BOS)")
    ap.add_argument("--exit", dest="exit_style", default="fixed",
                     choices=["fixed", "trailing"],
                     help="fixed = walk to stop or a hard 1:2 (original). "
                          "trailing = breakeven at 1R, half off at 1:2, rest "
                          "trails behind new 4H swings (only affects "
                          "--mode continuation; reversal always uses fixed).")
    args = ap.parse_args()

    if args.start:
        print(f"OUT-OF-SAMPLE WINDOW {args.start} to {args.end}")
    print(f"MODE: {args.mode}  EXIT: {args.exit_style}")
    symbols = [args.symbol.upper()] if args.symbol else mon.SYMBOLS
    for sym in symbols:
        trades, gates, bars = await backtest(sym, args.months, args.start, args.end,
                                              args.mode, args.exit_style)
        report(sym, trades, gates, bars)


if __name__ == "__main__":
    asyncio.run(main())
