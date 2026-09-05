"""Per-gate exits, per-gate symbol lists, concurrency cap (added 2026-09-01).

Out-of-sample 2023-01 -> 2025-02 the two gates wanted opposite exits:
trailing HURT the reversal gate (-3.7R) and HELPED continuation (+38.9R).
Continuation was positive on all five symbols in both directions; reversal
only on BTC/ETH/BNB. These tests pin that behaviour so it cannot silently
drift back to a single policy.
"""
from datetime import datetime, timezone

import standalone_monitor as m

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


# --- symbol lists -----------------------------------------------------------

def test_symbol_lists_follow_the_out_of_sample_results():
    assert "SOLUSDT" in m.CONTINUATION_SYMBOLS and "SOLUSDT" not in m.REVERSAL_SYMBOLS
    assert "XRPUSDT" in m.CONTINUATION_SYMBOLS and "XRPUSDT" not in m.REVERSAL_SYMBOLS
    assert "BNBUSDT" in m.REVERSAL_SYMBOLS and "BNBUSDT" in m.CONTINUATION_SYMBOLS
    assert set(m.SYMBOLS) == set(m.REVERSAL_SYMBOLS) | set(m.CONTINUATION_SYMBOLS)


# --- paper journal: continuation trails, reversal does not --------------------

def _cont_paper(entry=100.0, stop=90.0, fee_pct=5.0):
    setup = {"direction": "long", "entry": entry, "stop": stop, "fee_pct": fee_pct}
    return m.record_paper_trade("BTCUSDT", setup, NOW, mode="continuation")


def test_continuation_paper_trade_loss_before_1r():
    t = _cont_paper()
    assert m.update_paper_trade(t, {"price": 89.0, "bar_low": 89.0, "bar_high": 101.0}, NOW)
    assert t["status"] == "loss" and t["r"] == -1.0 - 0.05


def test_continuation_paper_trade_moves_to_breakeven_at_1r():
    t = _cont_paper()
    assert not m.update_paper_trade(t, {"price": 110.0, "bar_low": 101.0, "bar_high": 110.5}, NOW)
    assert t["breakeven_done"] and t["current_stop"] == 100.0 and t["status"] == "open"


def test_continuation_paper_trade_takes_half_at_2r_and_stays_open():
    t = _cont_paper()
    assert not m.update_paper_trade(t, {"price": 120.0, "bar_low": 105.0, "bar_high": 120.5}, NOW)
    assert t["partial_done"] and t["partial_r"] == 1.0 and t["current_stop"] == 100.0
    assert t["status"] == "open"


def test_continuation_paper_trade_trails_4h_swing_never_backwards():
    t = _cont_paper()
    m.update_paper_trade(t, {"price": 120.0, "bar_low": 105.0, "bar_high": 120.5}, NOW)
    m.update_paper_trade(t, {"price": 125.0, "bar_low": 118.0, "bar_high": 126.0,
                             "recent_low_4h": 115.0}, NOW)
    assert t["current_stop"] == 115.0
    m.update_paper_trade(t, {"price": 124.0, "bar_low": 118.0, "bar_high": 125.0,
                             "recent_low_4h": 112.0}, NOW)
    assert t["current_stop"] == 115.0   # a lower swing must not loosen the stop


def test_continuation_paper_trade_banks_trailed_profit_on_stop():
    t = _cont_paper()
    m.update_paper_trade(t, {"price": 120.0, "bar_low": 105.0, "bar_high": 120.5}, NOW)
    m.update_paper_trade(t, {"price": 125.0, "bar_low": 118.0, "bar_high": 126.0,
                             "recent_low_4h": 115.0}, NOW)
    assert m.update_paper_trade(t, {"price": 114.0, "bar_low": 114.0, "bar_high": 119.0}, NOW)
    # half locked at 2R (=1.0R) + half trailed out at 115 (1.5R * 0.5) - fee
    assert t["status"] == "win"
    assert abs(t["r"] - (1.0 + 0.5 * 1.5 - 0.05)) < 1e-9


def test_reversal_paper_trade_still_takes_fixed_2r():
    setup = {"direction": "long", "entry": 100.0, "stop": 90.0, "fee_pct": 5.0}
    t = m.record_paper_trade("BTCUSDT", setup, NOW)          # default: reversal
    assert m.update_paper_trade(t, {"price": 121.0, "bar_low": 105.0, "bar_high": 121.0}, NOW)
    assert t["status"] == "win" and t["r"] == 2.0 - 0.05


# --- live position management: same split -------------------------------------

def test_manage_reversal_position_closes_fully_at_2r():
    pos = {"symbol": "BTCUSDT", "direction": "long", "entry": 100.0, "stop": 90.0,
           "mode": "reversal"}
    msgs = m.manage_position(pos, {"price": 120.0, "event_1h": None}, NOW)
    assert any("FULL position" in x for x in msgs)
    assert pos["closed"]


def test_manage_continuation_position_takes_half_and_keeps_running():
    pos = {"symbol": "BTCUSDT", "direction": "long", "entry": 100.0, "stop": 90.0,
           "mode": "continuation"}
    msgs = m.manage_position(pos, {"price": 120.0, "event_1h": None}, NOW)
    assert any("take half off" in x for x in msgs)
    assert not pos.get("closed")


def test_manage_continuation_position_moves_stop_behind_new_4h_swing():
    pos = {"symbol": "BTCUSDT", "direction": "long", "entry": 100.0, "stop": 90.0,
           "mode": "continuation", "breakeven_done": True}
    msgs = m.manage_position(pos, {"price": 118.0, "event_1h": None, "recent_low_4h": 112.0}, NOW)
    assert pos["trail_stop"] == 112.0
    assert any("MOVE STOP" in x for x in msgs)
    # Same swing again: nothing new to say.
    assert not m.manage_position(pos, {"price": 118.0, "event_1h": None, "recent_low_4h": 112.0}, NOW)


def test_manage_stop_hit_uses_the_trailed_level_and_reports_profit():
    pos = {"symbol": "BTCUSDT", "direction": "long", "entry": 100.0, "stop": 90.0,
           "mode": "continuation", "breakeven_done": True, "partial_done": True,
           "trail_stop": 112.0}
    msgs = m.manage_position(pos, {"price": 111.0, "event_1h": None}, NOW)
    assert pos["closed"]
    assert any("STOP HIT" in x and "in profit" in x for x in msgs)


# --- concurrency cap ---------------------------------------------------------

def test_open_slots_counts_distinct_symbols_across_paper_and_tracked():
    paper = [{"symbol": "BTCUSDT", "status": "open"}, {"symbol": "ETHUSDT", "status": "win"},
             {"symbol": "SOLUSDT", "status": "open"}]
    positions = [{"symbol": "BTCUSDT"}, {"symbol": "XRPUSDT", "closed": True}]
    assert m.open_slots(paper, positions) == 2
    assert m.MAX_OPEN_POSITIONS == 2


# --- trailing starts at entry, matching the backtest (found live 2026-09-05) --

def test_manage_continuation_trails_before_breakeven():
    # Original stop 90, entry 100. A confirmed 4H swing low at 94 is above
    # the stop but below entry: the stop should tighten to it now, not wait
    # for 1R. The backtest that produced the +38.9R did exactly this.
    pos = {"symbol": "BTCUSDT", "direction": "long", "entry": 100.0, "stop": 90.0,
           "mode": "continuation"}
    msgs = m.manage_position(pos, {"price": 98.0, "event_1h": None, "recent_low_4h": 94.0}, NOW)
    assert pos["trail_stop"] == 94.0
    assert any("MOVE STOP" in x for x in msgs)


def test_manage_breakeven_beats_a_lower_trailed_stop():
    # Trailed to 94 before 1R, then breakeven reached: the effective stop
    # must be entry (100), never the lower 94.
    pos = {"symbol": "BTCUSDT", "direction": "long", "entry": 100.0, "stop": 90.0,
           "mode": "continuation", "breakeven_done": True, "trail_stop": 94.0}
    msgs = m.manage_position(pos, {"price": 99.0, "event_1h": None}, NOW)
    assert pos["closed"] and any("STOP HIT" in x and "100.0" in x for x in msgs)
