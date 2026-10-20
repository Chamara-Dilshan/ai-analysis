"""
Pure structure helpers: picking zones and confirm levels from a 4H
structure read, and the bar range since the last run.

Split out of standalone_monitor.py (2026-10-21), which re-exports every
name here: standalone_monitor.X keeps working for bot.py, backtest.py and
the tests.
"""
from datetime import datetime, timedelta, timezone


# Minimum zone width, as a percent of price. The raw structure engine emits
# one-tick gaps — a $10 band on a $79k asset — and "nearest zone below price"
# picks those far more often than real ones. Price can never be meaningfully
# INSIDE a band that thin at the moment an hourly check runs, so without this
# filter the setup gate is unreachable in practice.
MIN_ZONE_PCT = 0.35


# How close price must get to a zone to count as having reached it.
#
# This cannot be a containment test. Zones in the "open" list are open
# precisely BECAUSE price has not traded back into them — the moment it does,
# the engine marks them mitigated and they leave the list. So "price inside an
# open zone" is self-contradictory and was true 0 times in 4,320 backtested
# hours. Proximity is what the trade actually needs anyway: price arriving at
# the zone edge, not sitting in its middle.
ZONE_TOUCH_PCT = 0.25


def wide_enough(top: float, bottom: float, price: float) -> bool:
    """Reject one-tick gaps. See MIN_ZONE_PCT."""
    if not price:
        return False
    return ((top - bottom) / price) * 100 >= MIN_ZONE_PCT


def pick_demand_zone(struct_4h: dict, price: float):
    # Zones AT or below price: bottom <= price. The old test was top < price,
    # which selected only zones entirely beneath the current price — so the
    # "is price inside the zone" gate could never be true, by construction.
    zones = []
    for ob in struct_4h["order_blocks"]["open"]:
        if ob["type"] == "bullish" and ob["bottom"] <= price:
            zones.append((ob["top"], ob["bottom"], "ob"))
    for fvg in struct_4h["fair_value_gaps"]["open"]:
        if fvg["type"] == "bullish" and fvg["bottom"] <= price:
            zones.append((fvg["top"], fvg["bottom"], "fvg"))
    zones = [z for z in zones if wide_enough(z[0], z[1], price)]
    if not zones:
        return None
    # NEAREST below price, not the highest-topped. max(top) let a single wide
    # stale FVG outrank every real zone, and its far-away bottom then failed
    # the fee/min-notional gate — reading as "no setup" while a valid one sat
    # right under price. Distance is measured to the edge price meets first.
    top, bottom, kind = min(zones, key=lambda z: abs(price - z[0]))
    return {"top": top, "bottom": bottom, "kind": kind}


def pick_supply_zone(struct_4h: dict, price: float):
    """Mirror of pick_demand_zone — nearest bearish OB/FVG above price, for
    when the higher timeframes are aligned short."""
    # Mirror of pick_demand_zone: zones AT or above price, so price can be
    # inside one rather than always strictly beneath it.
    zones = []
    for ob in struct_4h["order_blocks"]["open"]:
        if ob["type"] == "bearish" and ob["top"] >= price:
            zones.append((ob["top"], ob["bottom"], "ob"))
    for fvg in struct_4h["fair_value_gaps"]["open"]:
        if fvg["type"] == "bearish" and fvg["top"] >= price:
            zones.append((fvg["top"], fvg["bottom"], "fvg"))
    zones = [z for z in zones if wide_enough(z[0], z[1], price)]
    if not zones:
        return None
    # Nearest above price — mirror of pick_demand_zone. min(bottom) had the
    # same defect: one wide stale zone captured the choice.
    top, bottom, kind = min(zones, key=lambda z: abs(z[1] - price))
    return {"top": top, "bottom": bottom, "kind": kind}


def reached_zone(zone, price: float, low: float = None, high: float = None) -> bool:
    """Has price arrived at this zone within the last hour?

    Tests the hour's RANGE against the zone, not just the close. Price often
    tags a zone and leaves inside the same hour; a close-only test misses
    those entirely, which understates how often the setup is really available.
    See ZONE_TOUCH_PCT for why this is proximity rather than containment.
    """
    if not zone or not price:
        return False
    tol = price * ZONE_TOUCH_PCT / 100
    lo = low if low is not None else price
    hi = high if high is not None else price
    # Range overlap between the hour's travel and the zone (plus tolerance).
    return lo <= (zone["top"] + tol) and hi >= (zone["bottom"] - tol)


def pick_bull_confirm(struct_4h: dict):
    highs = [h["price"] for h in struct_4h["swings"]["recent_highs"]]
    return max(highs) if highs else None


def pick_bear_confirm(struct_4h: dict):
    lows = [l["price"] for l in struct_4h["swings"]["recent_lows"]]
    return min(lows) if lows else None


def latest_event_time(struct: dict):
    events = struct.get("recent_structure_events") or []
    return events[0]["time"] if events else None


def latest_event(struct: dict):
    """The most recent structure event as a whole dict, so callers can see
    direction and not just timing."""
    events = struct.get("recent_structure_events") or []
    return events[0] if events else None


def is_newer(candidate, previous) -> bool:
    """True if candidate is a structure-event timestamp strictly newer than
    previous. Guards against the swing engine revising its "latest" event
    backwards, which a simple inequality mistakes for fresh news."""
    if not candidate:
        return False
    if not previous:
        return True
    try:
        return datetime.fromisoformat(candidate) > datetime.fromisoformat(previous)
    except Exception:
        return candidate != previous


def range_since(candles: list, since_utc, bar_hours: int = 1, after_only: bool = False):
    """(low, high) across every candle still forming at or after `since_utc`,
    plus the current forming bar. With no reference point only the forming
    bar counts. Runs are hourly at :07, so between two runs one bar
    completes; its wicks are what the close-only check kept missing.

    `after_only` keeps ONLY bars that opened at or after `since_utc`, dropping
    the bar that straddles it. For a position, that straddling bar is the one
    the entry happened inside: part of it traded BEFORE the fill, so its low
    cannot be attributed to the trade. Including it made a hour-1 continuation
    entry stop itself out by construction — the stop is the forming 4H
    candle's low, a strict subset of that same 1H bar (measured at 20.2% of
    all firings). Returns (None, None) when nothing qualifies yet, so the
    caller falls back to the current price, which is always a true
    observation. Matches the backtest, which walks from the bar AFTER entry.
    """
    if not candles:
        return None, None
    chosen = candles[-1:]
    if since_utc is not None:
        picked = []
        for c in candles:
            try:
                open_t = datetime.fromisoformat(c["open_time"])
            except Exception:
                continue
            if open_t.tzinfo is None:
                open_t = open_t.replace(tzinfo=timezone.utc)
            keep = open_t >= since_utc if after_only else (
                open_t + timedelta(hours=bar_hours) > since_utc)
            if keep:
                picked.append(c)
        if picked:
            chosen = picked
        elif after_only:
            return None, None
    return min(c["low"] for c in chosen), max(c["high"] for c in chosen)
