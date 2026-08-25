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


async def backtest(symbol: str, months: int):
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
            st4h = structure.analyze(c4h[max(0, i4 - 199):i4 + 1], swing_strength=3)
            cached_4h_idx = i4
        if i1 != cached_1d_idx:
            st1d = structure.analyze(c1d[max(0, i1 - 179):i1 + 1], swing_strength=2)
            cached_1d_idx = i1
        st1h = structure.analyze(c1h[max(0, i - 199):i + 1], swing_strength=2)

        price = bar["close"]
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
            outcome, rr, hours = simulate(
                c1h, i, setup["direction"], setup["entry"], setup["stop"])
            # Charge the round trip. Reporting gross R flatters every result;
            # on a $200 account with a 2% risk the fee is a real slice of it.
            fee_r = (setup.get("fee_pct") or 0.0) / 100.0
            trades.append({
                "time": now.strftime("%Y-%m-%d %H:%M"),
                "direction": setup["direction"], "entry": setup["entry"],
                "stop": setup["stop"], "outcome": outcome,
                "r": rr - fee_r if outcome != "open" else 0.0,
                "gross_r": rr, "fee_r": fee_r, "hours": hours,
            })
            busy_until = i + hours

    return trades, gate_hits, sum(1 for t in t1h if t >= begin_ms)


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
        print(f"  win rate {100 * len(wins) / resolved:.1f}%")
        print(f"  total {total_r:+.1f}R, expectancy {total_r / resolved:+.2f}R/trade")
        print(f"  on $200 at 2% risk: {total_r * 4:+.2f} USD")
    print("\n  date              dir    entry      stop    result   hrs")
    for t in trades[-15:]:
        print(f"  {t['time']}  {t['direction']:5s} {t['entry']:9.1f} "
              f"{t['stop']:9.1f}  {t['outcome']:7s} {t['hours']:4d}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=6)
    ap.add_argument("--symbol", default=None)
    args = ap.parse_args()

    symbols = [args.symbol.upper()] if args.symbol else mon.SYMBOLS
    for sym in symbols:
        trades, gates, bars = await backtest(sym, args.months)
        report(sym, trades, gates, bars)


if __name__ == "__main__":
    asyncio.run(main())
