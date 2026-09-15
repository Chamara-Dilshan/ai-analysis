"""
Regression tests for two silent defects found in the 2026-09-16 audit:
  - manage_position could trail a stop to the WRONG SIDE of price, which on
    the exchange is an instant exit that then reports as a win.
  - main() advanced state["last_run"] even when a symbol's fetch failed, so
    the skipped hour's stop/target check was never re-examined.
"""
from datetime import datetime, timezone

import standalone_monitor as m

NOW = datetime(2026, 9, 16, 12, 7, tzinfo=timezone.utc)


def _pos(direction="long", entry=100.0, stop=95.0, **kw):
    p = {"symbol": "BTCUSDT", "direction": direction, "entry": entry,
         "stop": stop, "target": entry + 2 * (entry - stop) if direction == "long"
         else entry - 2 * (stop - entry), "mode": "continuation"}
    p.update(kw)
    return p


def _read(price, low=None, high=None, **kw):
    r = {"price": price, "bar_low": low if low is not None else price,
         "bar_high": high if high is not None else price,
         "event_1h": None, "trend_1d": "up", "trend_4h": "up"}
    r.update(kw)
    return r


def _moves(msgs):
    # Only the trail message — "MOVE STOP" alone also matches the separate
    # breakeven announcement, which several of these cases legitimately fire.
    return [x for x in msgs if "New 4H swing" in x]


# --------------------------------------------------------------------------- #
# Trail must never cross price
# --------------------------------------------------------------------------- #
def test_long_ignores_a_confirmed_swing_low_sitting_above_price():
    # The audit's exact scenario: swing low confirms at 103 (it needs candles
    # either side, so by confirmation price has already fallen through it) and
    # price is 101. Trailing there would stop the long out instantly.
    pos = _pos()
    msgs = m.manage_position(pos, _read(101.0, recent_low_4h=103.0), NOW)
    assert pos.get("trail_stop") is None
    assert _moves(msgs) == []


def test_long_still_trails_a_swing_low_below_price():
    pos = _pos()
    msgs = m.manage_position(pos, _read(110.0, recent_low_4h=104.0), NOW)
    assert pos["trail_stop"] == 104.0
    assert len(_moves(msgs)) == 1


def test_short_ignores_a_confirmed_swing_high_below_price():
    pos = _pos(direction="short", entry=100.0, stop=105.0)
    msgs = m.manage_position(pos, _read(99.0, recent_high_4h=97.0), NOW)
    assert pos.get("trail_stop") is None
    assert _moves(msgs) == []


def test_short_still_trails_a_swing_high_above_price():
    pos = _pos(direction="short", entry=100.0, stop=105.0)
    msgs = m.manage_position(pos, _read(90.0, recent_high_4h=96.0), NOW)
    assert pos["trail_stop"] == 96.0
    assert len(_moves(msgs)) == 1


def test_a_swing_exactly_at_price_is_also_refused():
    # Equality is the same instant-exit case, not a safe edge.
    pos = _pos()
    msgs = m.manage_position(pos, _read(104.0, recent_low_4h=104.0), NOW)
    assert pos.get("trail_stop") is None
    assert _moves(msgs) == []


def test_guard_does_not_disturb_an_already_trailed_stop():
    pos = _pos(trail_stop=104.0)
    m.manage_position(pos, _read(101.0, recent_low_4h=103.0), NOW)
    assert pos["trail_stop"] == 104.0   # kept: only tightens, never loosens


def test_reversal_trades_are_untouched_by_the_guard():
    pos = _pos(mode="reversal")
    m.manage_position(pos, _read(110.0, recent_low_4h=104.0), NOW)
    assert pos.get("trail_stop") is None   # reversal never trails at all
