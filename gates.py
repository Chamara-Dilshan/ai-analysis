"""
The two setup gates - reversal (pullback into a zone) and trend
continuation (enter on a fresh trend-aligned 4H BOS) - and the plan
constants they enforce. Pure functions of the market read.

Split out of standalone_monitor.py (2026-10-21), which re-exports every
name here: standalone_monitor.X keeps working for bot.py, backtest.py and
the tests.
"""
from datetime import datetime, timedelta
import risk
import structure
from zones import reached_zone


# The plan's trading windows, UTC. A setup outside these is prep, not a trade.
KILL_ZONES = (("London", 7, 9), ("New York", 13, 15))


# How recent a 1H structure event must be to count as "the trigger" rather
# than old history. Backtesting showed 3h was far too tight — structure
# events are rarer than that, so the gate passed 0 times in 4,320 hours.
# Half a day keeps the signal current while letting price take its time
# reaching the zone after the turn.
TRIGGER_MAX_AGE = timedelta(hours=12)


# Capital plan — must match risk.py's defaults and the MCP server.
CAPITAL, RISK_PCT, LEVERAGE = 200, 2, 5


# Round-trip fees as a percent of the $4 risk. A very tight stop can make the
# trade cost a fifth of what it risks, which quietly demands a much higher
# win rate than the stated R:R suggests.
MAX_FEE_PCT_OF_RISK = 10.0


def active_kill_zone(now_utc: datetime):
    """Name of the kill zone we are inside, or None. Weekends never qualify —
    the plan does not trade them."""
    if now_utc.weekday() >= 5:
        return None
    for name, start, end in KILL_ZONES:
        if start <= now_utc.hour < end:
            return name
    return None


def evaluate_setup(r: dict, now_utc: datetime, news_blocked: bool = False,
                   require_kill_zone: bool = True):
    """Check the full entry checklist, not just whether a level moved.

    A crossed level is an event; a setup is an event that also happens to be
    in the right direction, at the right price, at the right time, clear of
    news, and cheap enough to be worth taking. Returns a dict describing
    every gate so a near-miss is still legible in the log.

    `require_kill_zone=False` exists only for backtest.py, to measure whether
    the session window earns its place — the structural gates lined up on
    three symbols outside the window on 2026-09-18 and were all blocked.
    Live behaviour is unchanged by default.
    """
    gates = {}

    # Gate: higher timeframes agree. Trading against 1D or 4H is the single
    # most expensive mistake available here.
    aligned_long = r["trend_1d"] == "up" and r["trend_4h"] == "up"
    aligned_short = r["trend_1d"] == "down" and r["trend_4h"] == "down"
    gates["aligned"] = aligned_long or aligned_short
    direction = "long" if aligned_long else "short" if aligned_short else None

    # Gate: price is in the 4H zone we care about. For longs that is a
    # bullish OB/FVG below price (discount); shorts are the mirror.
    zone = r["demand"] if direction == "long" else r["supply"]
    gates["in_zone"] = reached_zone(zone, r["price"],
                                     r.get("bar_low"), r.get("bar_high"))

    # Gate: the 1H has actually turned inside that zone. Arriving at a level
    # proves nothing on its own — this is the trigger.
    ev = r["event_1h"]
    trigger_ok = False
    if ev and direction:
        want = "bullish" if direction == "long" else "bearish"
        try:
            ev_time = datetime.fromisoformat(ev["time"])
            fresh = (now_utc - ev_time) <= TRIGGER_MAX_AGE
        except Exception:
            fresh = False
        trigger_ok = ev["direction"] == want and fresh
    gates["trigger"] = trigger_ok

    # Gate: the plan's session windows.
    kz = active_kill_zone(now_utc)
    gates["kill_zone"] = (kz is not None) or not require_kill_zone

    # Gate: no high-impact release inside the blackout window. The plan
    # forbids trading through FOMC/CPI/NFP, where a stop is decoration.
    gates["news_clear"] = not news_blocked

    if not all(gates.values()):
        return {"ready": False, "gates": gates, "direction": direction,
                "zone": zone, "kill_zone": kz}

    # Structural gates passed — build the trade. Stop goes just beyond the
    # zone that is supposed to hold; if it does not hold, the reason for the
    # trade is gone.
    if direction == "long":
        entry, stop = r["price"], round(zone["bottom"] * 0.999, 2)
    else:
        entry, stop = r["price"], round(zone["top"] * 1.001, 2)

    # Gate: fees must not eat the edge. A stop tight enough to make the
    # round trip cost a large slice of the $4 risk is a trade that needs to
    # be right far more often than the R:R implies.
    fee_pct = None
    try:
        m = risk.position_size(entry=entry, stop_loss=stop, capital=CAPITAL,
                                risk_pct=RISK_PCT, leverage=LEVERAGE)
        fee_pct = m["fees"]["pct_of_risk"]
        gates["fee_ok"] = fee_pct <= MAX_FEE_PCT_OF_RISK and m["passes_plan"]
    except Exception:
        # If the math will not compute, do not claim the setup is ready.
        gates["fee_ok"] = False

    return {"ready": all(gates.values()), "gates": gates,
            "direction": direction, "zone": zone, "kill_zone": kz,
            "entry": entry, "stop": stop, "fee_pct": fee_pct}


def _structural_stop(candles_4h: list, event_index: int, long: bool,
                     swing_strength: int, candle_stop: float) -> float:
    """Nearest CONFIRMED 4H swing low (long) / high (short) before the breakout
    candle, never tighter than the breakout candle's own extreme. Only candles
    up to the breakout are consulted and a swing needs `swing_strength`
    candles on both sides, so anything returned was fully formed before the
    break — no look-ahead. Falls back to the candle stop if no swing exists."""
    swings = structure.find_swings(candles_4h[:event_index + 1], swing_strength, swing_strength)
    kind = "low" if long else "high"
    pts = [s["price"] for s in swings if s["type"] == kind and s["index"] < event_index]
    if not pts:
        return candle_stop
    return min(candle_stop, pts[-1]) if long else max(candle_stop, pts[-1])


def _continuation_fee(entry: float, stop: float):
    """(fee % of risk, risk % used) for a continuation stop, sized to Binance's
    minimum notional when 2% would fall short. fee is None if the math fails."""
    sl_distance_pct = abs(entry - stop) / entry * 100
    min_risk_pct = round(risk.MIN_NOTIONAL_USD * sl_distance_pct / CAPITAL, 3)
    risk_pct_used = max(RISK_PCT, min_risk_pct)
    try:
        m = risk.position_size(entry=entry, stop_loss=stop, capital=CAPITAL,
                                risk_pct=risk_pct_used, leverage=LEVERAGE)
        return m["fees"]["pct_of_risk"], risk_pct_used
    except Exception:
        return None, risk_pct_used


def evaluate_trend_continuation_setup(trend_1d: str, event_4h: dict, candles_4h: list,
                                       price: float, now_utc: datetime,
                                       news_blocked: bool = False,
                                       max_fee_pct: float = None,
                                       stop_mode: str = "candle",
                                       swing_strength: int = 3,
                                       choch_flip_hours: float = 0.0,
                                       event_1d: dict = None):
    """Trend-continuation entry: trade a fresh 4H BOS immediately, without
    waiting for the pullback evaluate_setup() requires.

    `max_fee_pct` (default MAX_FEE_PCT_OF_RISK) and `stop_mode` exist for
    backtest.py to test alternatives without touching live behaviour:
      candle    — stop at the breakout candle's own low/high (live default)
      structure — stop at the nearest confirmed 4H swing before the break,
                  never tighter than the candle stop
      hybrid    — candle stop, widened to the structural one only when the
                  candle stop fails the fee gate
    Added 2026-09-15 after the VM log showed the week's only real setup
    (ETH 2026-09-14 20:00 BOS) pass every gate except fee_ok because the
    breakout candle was small. `swing_strength` must match whatever produced
    `candles_4h`'s structure (check_symbol uses 3 on 4H).

    Added 2026-08-29. The 2026-08-19->08-28 rally (BTC ~$65k->$81.5k, ETH
    ~$1,900->$2,566) went almost entirely uncaptured by evaluate_setup —
    every hourly check saw a confirmed bullish BOS but the demand-zone
    pullback it waits for never came before price ran further. This is the
    other half of the plan: evaluate_setup handles reversals (wait for the
    pullback, tight stop near structure); this handles continuation (enter
    on the break itself, wider stop at the breakout candle's own low/high).

    Deliberately does NOT require price in a demand/supply zone, a separate
    1H trigger, or a kill-zone window — the 4H BOS closing in the daily
    trend's direction IS the trigger, and it happens on the 4H clock
    (00/04/08/12/16/20 UTC), not London/NY hours. Requiring a kill zone here
    would reintroduce the same "confirmed but stale by the time conditions
    line up" gap this mode exists to fix. News blackout still applies —
    that's about not entering into a stop-hunt candle, unrelated to entry
    style.

    Chamara's call (2026-08-29): the 2%-risk math must not gate the entry.
    A structurally wide stop is fine — size to Binance's $100 minimum
    notional instead of shrinking the stop or skipping the trade, and report
    the resulting risk % plainly rather than silently forcing 2%.

    `candles_4h` must be the SAME candle list/slice that produced `event_4h`
    via structure.analyze(), since event_4h["index"] indexes into it.
    """
    gates = {}

    # A CHoCH is excluded on purpose: it is the signature of a trend CHANGING,
    # not continuing, and taking them is what would buy every failed bounce in
    # chop. The cost is that the FIRST leg of a genuine new trend is invisible
    # here — BTC's 2026-09-18 rally off the lows registered as a CHoCH and the
    # gate sat it out from $78,047 to $81,265.
    #
    # choch_flip_hours > 0 (backtest only) relaxes that in one narrow case: a
    # fresh 4H CHoCH counts if the DAILY has itself just flipped the same way
    # within that window, i.e. this is the opening leg of a new daily trend
    # rather than another failed bounce inside an old one.
    kind_ok = bool(event_4h) and event_4h["type"] == "BOS"
    entry_kind = "bos"
    if (not kind_ok and choch_flip_hours and event_4h
            and event_4h["type"] == "CHoCH" and event_1d
            and event_1d.get("type") == "CHoCH"
            and event_1d.get("direction") == event_4h["direction"]):
        try:
            flip_age = now_utc - datetime.fromisoformat(event_1d["time"])
            if timedelta(0) <= flip_age <= timedelta(hours=choch_flip_hours):
                kind_ok = True
                entry_kind = "choch-flip"
        except Exception:
            pass

    direction = None
    if kind_ok and trend_1d == "up" and event_4h["direction"] == "bullish":
        direction = "long"
    elif kind_ok and trend_1d == "down" and event_4h["direction"] == "bearish":
        direction = "short"
    gates["trend_aligned_bos"] = direction is not None

    # Fresh means "this is the candle that just closed" — without this the
    # same break re-fires every hour for days until the next event.
    fresh = False
    if direction:
        try:
            fresh = (now_utc - datetime.fromisoformat(event_4h["time"])) <= timedelta(hours=4)
        except Exception:
            fresh = False
    gates["fresh"] = fresh
    gates["news_clear"] = not news_blocked

    if not all(gates.values()):
        return {"ready": False, "gates": gates, "direction": direction}

    breakout = candles_4h[event_4h["index"]]
    entry = price
    long = direction == "long"
    candle_stop = breakout["low"] if long else breakout["high"]
    stop = candle_stop
    if stop_mode == "structure":
        stop = _structural_stop(candles_4h, event_4h["index"], long, swing_strength, candle_stop)

    # Sanity: the stop must still sit on the correct side of entry. Can fail
    # if price has already round-tripped past the breakout candle's opposite
    # extreme by the time this runs.
    valid_side = (stop < entry) if long else (stop > entry)
    gates["stop_valid"] = valid_side
    if not valid_side:
        return {"ready": False, "gates": gates, "direction": direction}

    cap = MAX_FEE_PCT_OF_RISK if max_fee_pct is None else max_fee_pct
    fee_pct, risk_pct_used = _continuation_fee(entry, stop)
    gates["fee_ok"] = fee_pct is not None and fee_pct <= cap

    if not gates["fee_ok"] and stop_mode == "hybrid":
        wider = _structural_stop(candles_4h, event_4h["index"], long, swing_strength, candle_stop)
        if wider != stop:
            stop = wider
            fee_pct, risk_pct_used = _continuation_fee(entry, stop)
            gates["fee_ok"] = fee_pct is not None and fee_pct <= cap

    # "2% strict" (2026-10-21): a stop too wide for 2% risk to reach Binance's
    # $100 minimum is skipped, not sized up. Pre-registered and passed on the
    # never-used 2019-09 -> 2021-01 window (+$179 vs +$176, max drawdown -$54
    # vs -$57) after winning 2 of 3 earlier windows; Docs/05. Narrow on profit,
    # clear on risk: no trade ever risks more than the plan's 2%.
    gates["risk_ok"] = risk_pct_used <= RISK_PCT

    return {
        "ready": all(gates.values()), "gates": gates, "direction": direction,
        "entry": entry, "stop": stop, "fee_pct": fee_pct,
        "risk_pct_used": risk_pct_used,
        "risk_unusually_high": risk_pct_used > 6.0,
        "stop_source": "structure" if stop != candle_stop else "candle",
        "stop_dist_pct": abs(entry - stop) / entry * 100,
        "entry_kind": entry_kind,
        "event": event_4h,
    }
