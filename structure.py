"""
Pure-Python market-structure detection for ICT / SMC analysis.

Operates on a chronological list of OHLC candle dicts exactly as produced by
get_klines():  {"open_time","open","high","low","close","volume"}.

No third-party dependencies — every rule is explicit and unit-tested in
tests/test_structure.py. The definitions are pragmatic, widely-used
*heuristics*, not ground truth; each is documented at its function.

Deliberately NOT attempted: automatic Elliott Wave counting (reliable
auto-counting isn't feasible). The swing points below are what a human would
hand-count waves from.
"""
from typing import Any, Optional

Candle = dict


def find_swings(candles: list, left: int = 2, right: int = 2) -> list:
    """
    Fractal swing points. Index i is a swing HIGH if its high is strictly
    greater than the `left` highs immediately before and the `right` highs
    immediately after it (swing LOW is symmetric on lows). The final `right`
    candles cannot be confirmed yet and are skipped.

    Returns a chronological list of {index, time, price, type} where type is
    "high" or "low".
    """
    swings: list = []
    n = len(candles)
    for i in range(left, n - right):
        hi = candles[i]["high"]
        lo = candles[i]["low"]
        if (all(hi > candles[i - k]["high"] for k in range(1, left + 1))
                and all(hi > candles[i + k]["high"] for k in range(1, right + 1))):
            swings.append({"index": i, "time": candles[i]["open_time"],
                           "price": hi, "type": "high"})
        if (all(lo < candles[i - k]["low"] for k in range(1, left + 1))
                and all(lo < candles[i + k]["low"] for k in range(1, right + 1))):
            swings.append({"index": i, "time": candles[i]["open_time"],
                           "price": lo, "type": "low"})
    swings.sort(key=lambda s: s["index"])
    return swings


def detect_structure(candles: list, swings: list, right: int = 2) -> tuple[str, list]:
    """
    Break of Structure (BOS) vs Change of Character (CHoCH) from the swing
    sequence and candle closes.

    A swing at index j is only "known" once `right` candles have printed after
    it (index j + right). Walking candles left→right, when a close breaks above
    the most recent known swing high we register a bullish break; below the most
    recent known swing low, a bearish break. A break *with* the prevailing trend
    is a BOS (continuation); the first break *against* it is a CHoCH (reversal).

    Returns (trend, events) where trend is "up"/"down"/"none" and events is a
    chronological list of {type: BOS|CHoCH, direction: bullish|bearish, level,
    time, index}.
    """
    # When each swing becomes usable as a reference level: index + right.
    activations: dict[int, list] = {}
    for s in swings:
        activations.setdefault(s["index"] + right, []).append(s)

    trend = "none"
    ref_high: Optional[dict] = None
    ref_low: Optional[dict] = None
    broken_high: Optional[float] = None
    broken_low: Optional[float] = None
    events: list = []

    for t, candle in enumerate(candles):
        for s in activations.get(t, []):
            if s["type"] == "high":
                ref_high = s
            else:
                ref_low = s

        close = candle["close"]

        if ref_high and close > ref_high["price"] and ref_high["price"] != broken_high:
            direction = "bullish"
            kind = "CHoCH" if trend == "down" else "BOS"
            events.append({"type": kind, "direction": direction,
                           "level": ref_high["price"],
                           "time": candle["open_time"], "index": t})
            broken_high = ref_high["price"]
            trend = "up"

        if ref_low and close < ref_low["price"] and ref_low["price"] != broken_low:
            direction = "bearish"
            kind = "CHoCH" if trend == "up" else "BOS"
            events.append({"type": kind, "direction": direction,
                           "level": ref_low["price"],
                           "time": candle["open_time"], "index": t})
            broken_low = ref_low["price"]
            trend = "down"

    return trend, events


def _mitigated(candles: list, third_index: int, top: float, bottom: float,
               bullish: bool) -> bool:
    """A gap/zone is mitigated once a later candle trades back into [bottom, top]."""
    for c in candles[third_index + 1:]:
        if bullish and c["low"] <= top:
            return True
        if not bullish and c["high"] >= bottom:
            return True
    return False


def find_fvgs(candles: list, min_pct: float = 0.0) -> list:
    """
    Fair Value Gaps (3-candle imbalance). Bullish FVG: candle i+1's low is above
    candle i-1's high (an untouched gap below price). Bearish FVG: candle i+1's
    high is below candle i-1's low. `min_pct` filters tiny gaps by size relative
    to price.

    Returns chronological list of {type, top, bottom, size_pct, index, time,
    mitigated}.
    """
    fvgs: list = []
    n = len(candles)
    for i in range(1, n - 1):
        prev, nxt = candles[i - 1], candles[i + 1]
        top = bottom = None
        kind = None
        if nxt["low"] > prev["high"]:
            kind, top, bottom = "bullish", nxt["low"], prev["high"]
        elif prev["low"] > nxt["high"]:
            kind, top, bottom = "bearish", prev["low"], nxt["high"]
        if kind is None:
            continue
        size_pct = (top - bottom) / bottom * 100 if bottom else 0.0
        if size_pct < min_pct:
            continue
        fvgs.append({
            "type": kind,
            "top": round(top, 2),
            "bottom": round(bottom, 2),
            "size_pct": round(size_pct, 3),
            "index": i,
            "time": candles[i]["open_time"],
            "mitigated": _mitigated(candles, i + 1, top, bottom, kind == "bullish"),
        })
    return fvgs


def find_order_blocks(candles: list) -> list:
    """
    Order blocks tied to displacement: the last opposite-color candle right
    before a candle that creates a Fair Value Gap. A bullish FVG's displacement
    is preceded by the last down-candle (bullish/demand OB); a bearish FVG's by
    the last up-candle (bearish/supply OB). The OB zone is that candle's full
    [low, high] range.

    Returns chronological list of {type, top, bottom, index, time, mitigated}.
    """
    obs: list = []
    for fvg in find_fvgs(candles):
        disp = fvg["index"]  # displacement (middle) candle of the FVG
        want_bearish_candle = fvg["type"] == "bullish"  # OB is the opposite color
        j = disp - 1
        ob_idx = None
        while j >= 0:
            c = candles[j]
            is_bear = c["close"] < c["open"]
            is_bull = c["close"] > c["open"]
            if (want_bearish_candle and is_bear) or (not want_bearish_candle and is_bull):
                ob_idx = j
                break
            j -= 1
        if ob_idx is None:
            continue
        c = candles[ob_idx]
        top, bottom = c["high"], c["low"]
        obs.append({
            "type": "bullish" if fvg["type"] == "bullish" else "bearish",
            "top": round(top, 2),
            "bottom": round(bottom, 2),
            "index": ob_idx,
            "time": c["open_time"],
            "mitigated": _mitigated(candles, disp, top, bottom, fvg["type"] == "bullish"),
        })
    # De-dupe order blocks that resolve to the same candle.
    seen: set = set()
    unique: list = []
    for ob in obs:
        if ob["index"] in seen:
            continue
        seen.add(ob["index"])
        unique.append(ob)
    return unique


def find_liquidity(swings: list, tol_pct: float = 0.1) -> dict:
    """
    Equal highs / equal lows = resting liquidity (stops) above/below price.
    Swing highs whose prices sit within `tol_pct` of each other are clustered
    into a buy-side liquidity level; swing lows into sell-side. Only clusters of
    two or more are returned.

    Returns {"equal_highs": [{price, count, times}], "equal_lows": [...]}.
    """
    def cluster(points: list) -> list:
        out: list = []
        used = [False] * len(points)
        for a in range(len(points)):
            if used[a]:
                continue
            group = [points[a]]
            used[a] = True
            for b in range(a + 1, len(points)):
                if used[b]:
                    continue
                base = points[a]["price"]
                if base and abs(points[b]["price"] - base) / base * 100 <= tol_pct:
                    group.append(points[b])
                    used[b] = True
            if len(group) >= 2:
                avg = sum(g["price"] for g in group) / len(group)
                out.append({
                    "price": round(avg, 2),
                    "count": len(group),
                    "times": [g["time"] for g in group],
                })
        return out

    highs = [s for s in swings if s["type"] == "high"]
    lows = [s for s in swings if s["type"] == "low"]
    return {"equal_highs": cluster(highs), "equal_lows": cluster(lows)}


def premium_discount(candles: list, swings: Optional[list] = None) -> dict:
    """
    Where the latest close sits within the CURRENT DEALING RANGE — the most
    recent swing low to the most recent swing high (the ICT definition) —
    rather than the whole analyzed window, which can stay dominated by an old
    extreme long after price has moved on. >55% = premium (sell zone in ICT),
    <45% = discount (buy zone), else equilibrium.

    Falls back to the full window's high/low (range_source "full_window")
    when `swings` is omitted or doesn't contain at least one high and one low
    — e.g. too few candles, or swing_strength too high for the sample.

    A swing only confirms once candles have printed after it (see find_swings),
    so the "last" swing can lag the real extreme — price can already be trading
    beyond it, putting position_pct outside [0, 100]. That's reported honestly
    as zone "above_range"/"below_range" with an explanatory note, rather than
    mislabeled "premium"/"discount" or silently clamped. (Impossible for the
    full_window fallback, whose bounds are the analyzed candles' own extremes.)

    Returns {range_high, range_low, equilibrium, current_close, position_pct,
    zone, range_source, note}.
    """
    close = candles[-1]["close"]
    range_high = range_low = None
    range_source = "full_window"

    if swings:
        last_high = next((s for s in reversed(swings) if s["type"] == "high"), None)
        last_low = next((s for s in reversed(swings) if s["type"] == "low"), None)
        if last_high and last_low and last_high["price"] != last_low["price"]:
            range_high = max(last_high["price"], last_low["price"])
            range_low = min(last_high["price"], last_low["price"])
            range_source = "dealing_range"

    if range_high is None:
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        range_high, range_low = max(highs), min(lows)

    span = range_high - range_low
    pct = (close - range_low) / span * 100 if span else 50.0

    note = None
    if pct > 100:
        zone = "above_range"
        note = ("Price is trading ABOVE the last confirmed swing high — a new "
                 "high likely hasn't fractal-confirmed yet. Treat this range as "
                 "stale until it does.")
    elif pct < 0:
        zone = "below_range"
        note = ("Price is trading BELOW the last confirmed swing low — a new "
                 "low likely hasn't fractal-confirmed yet. Treat this range as "
                 "stale until it does.")
    elif pct > 55:
        zone = "premium"
    elif pct < 45:
        zone = "discount"
    else:
        zone = "equilibrium"

    return {
        "range_high": round(range_high, 2),
        "range_low": round(range_low, 2),
        "equilibrium": round((range_high + range_low) / 2, 2),
        "current_close": round(close, 2),
        "position_pct": round(pct, 1),
        "zone": zone,
        "range_source": range_source,
        "note": note,
    }


def atr(candles: list, period: int = 14) -> Optional[float]:
    """
    Average True Range over the last `period` candles — a plain volatility
    yardstick (simple rolling mean of True Range; NOT Wilder's smoothing).
    True Range for candle i is max(high-low, |high - prev_close|,
    |low - prev_close|). Returns None if fewer than `period + 1` candles are
    available (one prior close is needed to start the series).

    Intended use: sanity-check a planned stop distance against recent
    volatility (e.g. in calculate_position_size) — not a signal by itself.
    """
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        c, prev_close = candles[i], candles[i - 1]["close"]
        trs.append(max(
            c["high"] - c["low"],
            abs(c["high"] - prev_close),
            abs(c["low"] - prev_close),
        ))
    window = trs[-period:]
    return sum(window) / len(window)


def analyze(candles: list, swing_strength: int = 2, eq_tol_pct: float = 0.1,
            max_items: int = 5, atr_period: int = 14) -> dict:
    """
    Full structure read for a candle window. Lists are capped to `max_items`
    (most recent first; open/unmitigated zones preferred) to keep the payload
    small. Returns a dict ready to merge with symbol/interval by the caller.
    """
    if len(candles) < (swing_strength * 2 + 1):
        return {"error": "not enough candles for structure analysis",
                "candles_analyzed": len(candles)}

    swings = find_swings(candles, swing_strength, swing_strength)
    trend, events = detect_structure(candles, swings, swing_strength)
    fvgs = find_fvgs(candles)
    obs = find_order_blocks(candles)
    liquidity = find_liquidity(swings, eq_tol_pct)
    atr_value = atr(candles, atr_period)

    open_fvgs = [f for f in fvgs if not f["mitigated"]]
    open_obs = [o for o in obs if not o["mitigated"]]

    def recent(items: list) -> list:
        return list(reversed(items[-max_items:]))

    highs = [s for s in swings if s["type"] == "high"]
    lows = [s for s in swings if s["type"] == "low"]

    return {
        "candles_analyzed": len(candles),
        "trend": trend,
        "recent_structure_events": recent(events),
        "swings": {
            "recent_highs": recent(highs),
            "recent_lows": recent(lows),
            "total_highs": len(highs),
            "total_lows": len(lows),
        },
        "premium_discount": premium_discount(candles, swings),
        "fair_value_gaps": {
            "open": recent(open_fvgs),
            "open_count": len(open_fvgs),
            "total_count": len(fvgs),
        },
        "order_blocks": {
            "open": recent(open_obs),
            "open_count": len(open_obs),
            "total_count": len(obs),
        },
        "liquidity": liquidity,
        "atr": {
            "value": round(atr_value, 2) if atr_value is not None else None,
            "period": atr_period,
            "pct_of_price": (
                round(atr_value / candles[-1]["close"] * 100, 3)
                if atr_value is not None else None
            ),
        },
        "notes": (
            "Heuristic ICT/SMC structure — swings/BOS/CHoCH/FVG/OB/liquidity/ATR "
            "computed from OHLC, not validated signals. premium_discount uses the "
            "current dealing range (last swing low to last swing high), falling "
            "back to the full window if swings are too sparse. Elliott Wave is "
            "not auto-counted; use the swing points to count manually."
        ),
    }
