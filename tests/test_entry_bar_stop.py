"""
Regression tests for the hour-1 false stop-out (2026-09-16 audit, measured at
20.2% of continuation firings).

A continuation stop is the FORMING 4H candle's low. Fire in the first hour of
that candle and the stop is built from minutes of data — a strict subset of
the 1H bar the next hourly check re-examines. min(that bar) <= stop is then
true by construction, so the trade stopped itself out the hour after opening.
"""
from datetime import datetime, timedelta, timezone

import standalone_monitor as m

H = timedelta(hours=1)
OPEN_4H = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
ENTRY = OPEN_4H + timedelta(minutes=7)      # cron fires at :07


def bar(open_dt, low, high):
    mid = (low + high) / 2
    return {"open_time": open_dt.isoformat(), "open": mid, "high": float(high),
            "low": float(low), "close": mid, "volume": 1.0}


def candles():
    # 12:00 bar dips to 99 AFTER the 12:00-12:07 window the stop came from.
    return [bar(OPEN_4H - H, 104.0, 106.0),
            bar(OPEN_4H, 99.0, 105.0),          # holds the entry at 12:07
            bar(OPEN_4H + H, 103.0, 107.0)]     # forming, never near the stop


def _pos(**kw):
    p = {"symbol": "BTCUSDT", "direction": "long", "entry": 104.0,
         "stop": 100.0, "target": 112.0, "mode": "continuation",
         "opened": ENTRY.isoformat()}
    p.update(kw)
    return p


def _read():
    return {"price": 105.0, "candles_1h": candles(), "bar_low": 99.0,
            "bar_high": 107.0, "event_1h": None, "trend_1d": "up",
            "trend_4h": "up"}


# --------------------------------------------------------------------------- #
# range_since(after_only=...)
# --------------------------------------------------------------------------- #
def test_after_only_drops_the_bar_straddling_the_entry():
    lo, hi = m.range_since(candles(), ENTRY, after_only=True)
    assert (lo, hi) == (103.0, 107.0)      # only the 13:00 bar


def test_default_still_includes_the_straddling_bar():
    # Unchanged for its original caller: the run-to-run window must keep wicks.
    lo, hi = m.range_since(candles(), ENTRY)
    assert lo == 99.0


def test_after_only_returns_none_when_no_bar_has_opened_yet():
    # Checked in the same hour it was opened — nothing qualifies.
    only_entry_bar = candles()[:2]
    assert m.range_since(only_entry_bar, ENTRY, after_only=True) == (None, None)


# --------------------------------------------------------------------------- #
# read_for_trade + manage_position
# --------------------------------------------------------------------------- #
def test_hour_one_entry_is_not_stopped_by_its_own_entry_bar():
    pos, r = _pos(), _read()
    scoped = m.read_for_trade(r, pos, OPEN_4H)   # run window starts before entry
    assert scoped["bar_low"] == 103.0            # 99.0 excluded
    m.manage_position(pos, scoped, ENTRY + H)
    assert not pos.get("closed")


def test_a_genuine_stop_after_the_entry_bar_still_closes_it():
    pos = _pos()
    later = candles() + [bar(OPEN_4H + 2 * H, 97.0, 101.0)]
    r = dict(_read(), candles_1h=later)
    scoped = m.read_for_trade(r, pos, OPEN_4H)
    assert scoped["bar_low"] == 97.0             # post-entry bars still count
    m.manage_position(pos, scoped, ENTRY + 2 * H)
    assert pos.get("closed")


def test_an_older_trade_keeps_the_normal_run_window():
    # opened before the run window -> read_for_trade returns r untouched.
    pos = _pos(opened=(OPEN_4H - 5 * H).isoformat())
    r = _read()
    assert m.read_for_trade(r, pos, OPEN_4H) is r


def test_falls_back_to_current_price_when_no_bar_qualifies():
    pos, r = _pos(), dict(_read(), candles_1h=candles()[:2])
    scoped = m.read_for_trade(r, pos, OPEN_4H)
    assert scoped["bar_low"] is None
    m.manage_position(pos, scoped, ENTRY)
    assert not pos.get("closed")     # price 105 is nowhere near the 100 stop
