"""
evaluate_trend_continuation_setup's opt-in stop / fee-cap variants. The
defaults must keep reproducing the live behaviour; the variants exist for
backtest.py to test the 2026-09-15 fee-gate hypothesis.
"""
from datetime import datetime, timedelta, timezone

import standalone_monitor as m


def _c(i, low, high, close=None):
    close = (low + high) / 2 if close is None else close
    return {"open_time": f"c{i}", "open": close, "high": float(high),
            "low": float(low), "close": float(close), "volume": 1.0}


def _long_series(breakout_low=73900.0):
    """A confirmed swing low at index 4 (72,800 — three higher lows either
    side), a grind up, then a SMALL breakout candle at index 10 whose own low
    is the tight candle stop. One candle after so the slice ends past the
    break, as the live 4H slice does."""
    lows_highs = [
        (73400, 73600), (73300, 73550), (73200, 73500), (73000, 73400),
        (72800, 73300),                                   # index 4: swing low
        (73000, 73450), (73200, 73500), (73300, 73600),
        (73500, 73700), (73700, 73900),
    ]
    candles = [_c(i, lo, hi) for i, (lo, hi) in enumerate(lows_highs)]
    candles.append(_c(10, breakout_low, 74600.0, close=74400.0))   # index 10: breakout
    candles.append(_c(11, 74300.0, 74550.0))
    return candles


def _short_series():
    # Mirror of _long_series around 74,000: swing HIGH at index 4, small
    # breakout candle at index 10 whose own high is the tight stop.
    flipped = []
    for c in _long_series():
        lo, hi = 148000.0 - c["high"], 148000.0 - c["low"]
        flipped.append({**c, "low": lo, "high": hi, "close": 148000.0 - c["close"],
                        "open": 148000.0 - c["open"]})
    return flipped


def _event(direction="bullish", index=10):
    now = datetime(2026, 9, 14, 21, 7, tzinfo=timezone.utc)
    return {"type": "BOS", "direction": direction, "level": 73900.0,
            "time": (now - timedelta(hours=1)).isoformat(), "index": index}, now


def test_default_is_the_candle_stop_and_the_tight_one_fails_the_fee_gate():
    # The ETH 2026-09-14 shape: every gate passes except fee_ok.
    ev, now = _event()
    s = m.evaluate_trend_continuation_setup("up", ev, _long_series(), 74400.0, now)
    assert s["stop"] == 73900.0 and s["stop_source"] == "candle"
    assert s["gates"]["trend_aligned_bos"] and s["gates"]["fresh"] and s["gates"]["stop_valid"]
    assert s["gates"]["fee_ok"] is False and s["ready"] is False
    assert s["fee_pct"] > m.MAX_FEE_PCT_OF_RISK


def test_higher_fee_cap_lets_the_same_setup_through():
    ev, now = _event()
    s = m.evaluate_trend_continuation_setup("up", ev, _long_series(), 74400.0, now,
                                            max_fee_pct=20.0)
    assert s["stop"] == 73900.0
    assert s["gates"]["fee_ok"] and s["ready"]


def test_structure_stop_uses_the_confirmed_swing_low_before_the_break():
    ev, now = _event()
    s = m.evaluate_trend_continuation_setup("up", ev, _long_series(), 74400.0, now,
                                            stop_mode="structure")
    assert s["stop"] == 72800.0 and s["stop_source"] == "structure"
    assert s["gates"]["fee_ok"] and s["ready"]
    assert s["stop_dist_pct"] > 2.0


def test_structure_stop_never_tightens_below_the_candle_stop():
    # Breakout candle wicked far below the swing low (a sweep): the candle's
    # own low is already the wider stop and must be kept.
    ev, now = _event()
    s = m.evaluate_trend_continuation_setup("up", ev, _long_series(breakout_low=72000.0),
                                            74400.0, now, stop_mode="structure")
    assert s["stop"] == 72000.0 and s["stop_source"] == "candle"


def test_structure_stop_mirrors_for_shorts():
    ev, now = _event(direction="bearish")
    s = m.evaluate_trend_continuation_setup("down", ev, _short_series(), 73600.0, now,
                                            stop_mode="structure")
    assert s["direction"] == "short"
    assert s["stop"] == 148000.0 - 72800.0 and s["stop_source"] == "structure"
    assert s["ready"]


def test_structure_falls_back_to_the_candle_when_no_swing_is_confirmable():
    # Five flat candles then the break: no index can have 3 confirmed
    # candles either side, so there is nothing structural to widen to.
    flat = [_c(i, 73950.0, 74050.0) for i in range(5)]
    flat.append(_c(5, 73500.0, 74600.0, close=74400.0))
    ev, now = _event(index=5)
    s = m.evaluate_trend_continuation_setup("up", ev, flat, 74400.0, now, stop_mode="structure")
    assert s["stop"] == 73500.0 and s["stop_source"] == "candle"


def test_hybrid_keeps_the_candle_stop_when_it_already_clears_fees():
    ev, now = _event()
    wide_candle = _long_series(breakout_low=72500.0)   # ~2.6% away: fees fine
    s = m.evaluate_trend_continuation_setup("up", ev, wide_candle, 74400.0, now,
                                            stop_mode="hybrid")
    assert s["stop"] == 72500.0 and s["stop_source"] == "candle" and s["ready"]


def test_hybrid_widens_to_structure_only_when_the_candle_stop_fails_fees():
    ev, now = _event()
    s = m.evaluate_trend_continuation_setup("up", ev, _long_series(), 74400.0, now,
                                            stop_mode="hybrid")
    assert s["stop"] == 72800.0 and s["stop_source"] == "structure"
    assert s["gates"]["fee_ok"] and s["ready"]
    assert s["risk_pct_used"] == m.RISK_PCT   # ~2.2% stop still sizes at the default 2%
