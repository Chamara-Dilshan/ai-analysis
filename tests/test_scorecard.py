"""
The weekly scorecard (scorecard.py), the order ticket on setup alerts, and the
running version in the heartbeat.
"""
from datetime import datetime, timedelta, timezone

import pytest

import scorecard as sc
import standalone_monitor as m
from test_capped_dedup import _run

NOW = datetime(2026, 10, 25, 6, 7, tzinfo=timezone.utc)      # a Sunday


# --------------------------------------------------------------------------- #
# the expected range
# --------------------------------------------------------------------------- #
def test_band_slides_over_consecutive_trades_not_shuffles():
    # Sums of 2 consecutive: 2, 0, -2 -> streaks are kept intact.
    low, mid, high = sc.expected_band(2, [[1, 1, -1, -1]])
    assert (low, mid, high) == (-2, 0, 2)


def test_band_skips_windows_shorter_than_n():
    assert sc.expected_band(5, [[1, 1], [1, 1, 1, 1, 1]]) == (5, 5, 5)
    assert sc.expected_band(10, [[1, 1]]) is None


def test_band_from_the_real_reference_is_sane():
    seqs = sc.load_reference()
    assert sum(len(s) for s in seqs) > 1000
    low, mid, high = sc.expected_band(17, seqs)
    assert low < 0 < high and low < mid < high


def test_the_stop_rule_needs_enough_trades():
    band = (-10.0, 0.0, 10.0)
    assert "too few to judge" in sc.verdict(20, -15.0, band)
    assert "Plan rule: stop real-money" in sc.verdict(sc.MIN_TRADES_TO_JUDGE, -15.0, band)
    assert sc.verdict(60, 0.0, band).startswith("on track")
    assert sc.verdict(60, 12.0, band).startswith("above")


# --------------------------------------------------------------------------- #
# fills and picking
# --------------------------------------------------------------------------- #
def test_slippage_sign_means_worse_for_the_trade():
    real = [{"direction": "long", "entry": 101.0, "alert_entry": 100.0},
            {"direction": "short", "entry": 99.0, "alert_entry": 100.0},
            {"direction": "long", "entry": 50.0, "alert_entry": None}]
    assert sc.fill_slippage_pct(real) == pytest.approx([1.0, 1.0])


def test_taken_and_skipped_alerts_are_matched_by_alert_entry():
    paper = [{"symbol": "ETHUSDT", "entry": 2616.3, "status": "win", "r": 1.97},
             {"symbol": "BNBUSDT", "entry": 785.48, "status": "loss", "r": -1.1},
             {"symbol": "BTCUSDT", "entry": 80000, "status": "open"}]
    real = [{"symbol": "ETHUSDT", "alert_entry": 2616.3, "entry": 2610}]
    taken, skipped = sc.split_taken(paper, real)
    assert [t["symbol"] for t in taken] == ["ETHUSDT"]
    assert [t["symbol"] for t in skipped] == ["BNBUSDT"]


def test_scorecard_message_end_to_end():
    paper = [{"symbol": "ETHUSDT", "entry": 2616.3, "status": "win", "r": 1.966,
              "closed": (NOW - timedelta(days=2)).isoformat()},
             {"symbol": "BNBUSDT", "entry": 785.48, "status": "loss", "r": -1.1,
              "closed": (NOW - timedelta(days=20)).isoformat()}]
    real = [{"symbol": "ETHUSDT", "direction": "short", "alert_entry": 2616.3,
             "entry": 2610.0, "status": "win", "r": 1.9,
             "closed": (NOW - timedelta(days=2)).isoformat()}]
    msg = sc.weekly_scorecard(paper, real, -4.4, NOW, sequences=[[1, -1, -1, 1] * 25])
    assert "1 closed this week (+2.0R). All time 2 trades, +0.9R" in msg
    assert "Verdict: on track" in msg
    assert "Fills vs alerts: 0.24% worse" in msg          # short filled lower
    assert "Alerts you took: 1 (+1.97R avg). Skipped: 1 (-1.10R avg)." in msg


# --------------------------------------------------------------------------- #
# wired into the monitor
# --------------------------------------------------------------------------- #
def test_scorecard_goes_out_on_sunday_once(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "REAL_PATH", tmp_path / "real_trades.json")
    out = _run(monkeypatch, tmp_path, state={}, paper=[], mode="quiet", now=NOW)
    assert any(s.startswith("WEEKLY SCORECARD") for s in out["sent"])
    again = _run(monkeypatch, tmp_path, state=out["state"], paper=[], mode="quiet", now=NOW)
    assert not any(s.startswith("WEEKLY SCORECARD") for s in again["sent"])


def test_no_scorecard_on_other_days(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "REAL_PATH", tmp_path / "real_trades.json")
    out = _run(monkeypatch, tmp_path, state={}, paper=[], mode="quiet",
               now=NOW - timedelta(days=1))
    assert not any(s.startswith("WEEKLY SCORECARD") for s in out["sent"])


def test_heartbeat_names_the_running_version(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "REAL_PATH", tmp_path / "real_trades.json")
    monkeypatch.setattr(m, "running_version", lambda: "abc1234 Some commit")
    out = _run(monkeypatch, tmp_path, state={}, paper=[], mode="quiet",
               now=NOW - timedelta(days=1))
    beat = [s for s in out["sent"] if s.startswith("Daily check-in")]
    assert beat and "Running abc1234 Some commit." in beat[0]


# --------------------------------------------------------------------------- #
# the order ticket
# --------------------------------------------------------------------------- #
def test_ticket_rounds_down_to_the_lot_step():
    t = m.order_ticket("ETHUSDT", "short", 2616.30, 2692.46, 0.052523, "reversal")
    assert "SELL / SHORT ETHUSDT" in t and "Qty 0.052" in t
    assert "Take-profit 2463.98 on the FULL qty" in t
    assert "Risk if stopped: $3.96" in t                  # never above $4


def test_ticket_rounds_up_only_to_clear_the_100_dollar_minimum():
    # 0.9 SOL is $94 - under Binance's minimum - so it rounds UP to the
    # smallest step-aligned qty that clears $100, and says what that risks.
    t = m.order_ticket("SOLUSDT", "long", 104.3, 100.2, 0.9, "continuation")
    assert "Qty 0.96 " in t and "Risk if stopped: $3.94" in t


def test_a_qty_of_one_step_cannot_be_split(monkeypatch):
    monkeypatch.setitem(m.LOT_STEPS, "SOLUSDT", 1.0)
    t = m.order_ticket("SOLUSDT", "long", 104.3, 100.2, 0.9, "continuation")
    assert "Qty 1 " in t and "qty too small to split" in t


def test_continuation_ticket_halves_the_take_profit():
    t = m.order_ticket("BTCUSDT", "long", 80000, 78000, 0.0021, "continuation")
    assert "Qty 0.002" in t and "on HALF (0.001), trail the rest" in t


def test_setup_alert_carries_the_ticket():
    setup = {"entry": 2616.30, "stop": 2692.46, "direction": "short",
             "kill_zone": "london", "zone": {"bottom": 2600, "top": 2690}}
    r = {"trend_1d": "down", "trend_4h": "down"}
    msg = m.format_setup_alert("ETHUSDT", r, setup, NOW)
    assert "ORDER - SELL / SHORT ETHUSDT" in msg
