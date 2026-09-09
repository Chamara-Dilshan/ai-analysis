"""Stop and target checks use the bar RANGE since the last run, not the
hourly close (fixed 2026-09-09).

Live instance 2026-09-07 07:00 UTC: BTC's 1H low of 78,939 went through a
trailed stop at 79,125 and the bar closed back above it. The tracker, which
compared the close with the stop, said nothing. Binance had already closed
the position. These tests pin the range-based behaviour.
"""
from datetime import datetime, timezone, timedelta

import standalone_monitor as m

NOW = datetime(2026, 9, 7, 7, 7, tzinfo=timezone.utc)


def _pos(**kw):
    pos = {"symbol": "BTCUSDT", "direction": "long", "entry": 80000.0,
           "stop": 79000.0, "mode": "continuation"}
    pos.update(kw)
    return pos


# --- the live miss -------------------------------------------------------------

def test_wick_through_trailed_stop_is_a_stop_hit_even_if_close_recovers():
    pos = _pos(trail_stop=79125.0)
    r = {"price": 79300.0, "bar_low": 78939.0, "bar_high": 79400.0, "event_1h": None}
    msgs = m.manage_position(pos, r, NOW)
    assert pos["closed"]
    assert any("STOP HIT" in x and "78939.0" in x for x in msgs)


def test_close_only_read_falls_back_to_price():
    # No range supplied (old callers, tests): behave exactly as before.
    pos = _pos()
    assert m.manage_position(pos, {"price": 79500.0, "event_1h": None}, NOW) == []
    assert not pos.get("closed")


def test_short_wick_through_stop_closes():
    pos = _pos(direction="short", entry=80000.0, stop=81000.0)
    r = {"price": 80500.0, "bar_low": 80100.0, "bar_high": 81050.0, "event_1h": None}
    m.manage_position(pos, r, NOW)
    assert pos["closed"]


# --- milestones use the favourable extreme -------------------------------------

def test_1r_reached_by_the_high_not_the_close():
    pos = _pos()
    r = {"price": 80600.0, "bar_low": 80200.0, "bar_high": 81050.0, "event_1h": None}
    msgs = m.manage_position(pos, r, NOW)
    assert pos["breakeven_done"] and any("BREAKEVEN" in x for x in msgs)


def test_2r_reached_by_the_high_closes_a_reversal_trade():
    pos = _pos(mode="reversal", breakeven_done=True)
    r = {"price": 81500.0, "bar_low": 81000.0, "bar_high": 82000.0, "event_1h": None}
    msgs = m.manage_position(pos, r, NOW)
    assert pos["closed"] and any("1:2 TARGET" in x for x in msgs)


def test_bar_covering_stop_and_target_counts_as_stopped():
    # Pessimistic, same as the paper journal and the backtest.
    pos = _pos(mode="reversal")
    r = {"price": 81900.0, "bar_low": 78900.0, "bar_high": 82100.0, "event_1h": None}
    msgs = m.manage_position(pos, r, NOW)
    assert pos["closed"]
    assert any("STOP HIT" in x for x in msgs)
    assert not any("1:2 TARGET" in x for x in msgs)


# --- range_since: which bars count -------------------------------------------

def _bars(start: datetime, n: int):
    out = []
    for i in range(n):
        t = start + timedelta(hours=i)
        out.append({"open_time": t.isoformat(), "low": 100.0 - i, "high": 200.0 + i})
    return out


def test_range_since_covers_bars_that_traded_after_the_last_run():
    start = datetime(2026, 9, 7, 0, 0, tzinfo=timezone.utc)
    candles = _bars(start, 8)                 # 00:00 .. 07:00 (07:00 forming)
    candles[5]["low"] = 50.0                  # a spike in the 05:00 bar
    last_run = datetime(2026, 9, 7, 6, 7, tzinfo=timezone.utc)
    lo, hi = m.range_since(candles, last_run)
    # 06:00 bar (still open at 06:07, closed 07:00) and the forming 07:00 bar.
    assert (lo, hi) == (candles[7]["low"], candles[7]["high"])
    # The 05:00 bar closed at 06:00, before the 06:07 run: its spike is
    # not included — the previous run already saw that bar.
    assert lo != 50.0


def test_range_since_without_a_reference_is_the_forming_bar():
    start = datetime(2026, 9, 7, 0, 0, tzinfo=timezone.utc)
    candles = _bars(start, 3)
    assert m.range_since(candles, None) == (candles[-1]["low"], candles[-1]["high"])


def test_range_since_after_a_long_gap_spans_every_missed_bar():
    start = datetime(2026, 9, 7, 0, 0, tzinfo=timezone.utc)
    candles = _bars(start, 8)
    last_run = datetime(2026, 9, 7, 1, 7, tzinfo=timezone.utc)   # VM was down 6h
    lo, hi = m.range_since(candles, last_run)
    assert lo == min(c["low"] for c in candles[1:])
    assert hi == max(c["high"] for c in candles[1:])


def test_range_since_empty_candles():
    assert m.range_since([], NOW) == (None, None)


# --- per-trade window: bars before the entry do not count ---------------------

def test_read_for_trade_starts_at_the_trade_open_not_the_last_run():
    start = datetime(2026, 9, 7, 9, 0, tzinfo=timezone.utc)
    candles = _bars(start, 3)                 # 09:00, 10:00, 11:00 (forming)
    candles[1]["low"] = 10.0                  # 10:00 bar dipped hard...
    r = {"price": 150.0, "bar_low": 10.0, "bar_high": 202.0, "candles_1h": candles}
    last_run = datetime(2026, 9, 7, 10, 7, tzinfo=timezone.utc)
    # ...but the trade was only added at 10:30, after the dip's bar had
    # been mostly formed. The 10:00 bar was still open at 10:30 though, so
    # it IS in the window; only the 09:00 bar is excluded.
    pos = {"opened": datetime(2026, 9, 7, 10, 30, tzinfo=timezone.utc).isoformat()}
    rr = m.read_for_trade(r, pos, last_run)
    assert rr["bar_low"] == 10.0
    # Added at 11:05: the 10:00 bar had closed, its dip is not ours.
    pos = {"opened": datetime(2026, 9, 7, 11, 5, tzinfo=timezone.utc).isoformat()}
    rr = m.read_for_trade(r, pos, last_run)
    assert rr["bar_low"] == candles[2]["low"]


def test_read_for_trade_without_open_time_uses_the_run_window():
    r = {"price": 1.0, "bar_low": 0.5, "bar_high": 2.0, "candles_1h": []}
    assert m.read_for_trade(r, {}, None) is r
