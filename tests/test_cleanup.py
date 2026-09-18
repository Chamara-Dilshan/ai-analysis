"""
Regression tests for the 2026-09-19 cleanup: the last four known-open items
from the two audits. None could fabricate a trade, which is why they waited.
"""
from datetime import datetime, timezone

import backtest as B
import standalone_monitor as m

NOW = datetime(2026, 9, 19, 12, 7, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# paper_summary converts at each trade's real risk %, not a flat 2%
# --------------------------------------------------------------------------- #
def _done(r, risk_pct=None, symbol="BTCUSDT"):
    t = {"symbol": symbol, "status": "win" if r > 0 else "loss", "r": r}
    if risk_pct is not None:
        t["risk_pct_used"] = risk_pct
    return t


def test_summary_uses_each_trades_own_risk_pct():
    # +2R at 2% is $8; +2R at 6% is $24. A flat conversion would say $16.
    out = m.paper_summary([_done(2.0, 2.0), _done(2.0, 6.0)])
    assert "+$32.00" in out


def test_summary_falls_back_to_default_risk_when_absent():
    # Older journal entries predate risk_pct_used.
    out = m.paper_summary([_done(2.0)])
    assert f"+${2.0 * m.CAPITAL * m.RISK_PCT / 100:.2f}" in out


def test_summary_r_and_dollars_can_diverge():
    # Positive R, negative dollars: the win was small-risk, the loss large.
    out = m.paper_summary([_done(2.0, 1.0), _done(-1.5, 8.0)])
    assert "+0.5R" in out and "-$20.00" in out


def test_summary_handles_no_resolved_trades():
    assert "no trades resolved yet" in m.paper_summary([{"symbol": "X", "status": "open"}])


# --------------------------------------------------------------------------- #
# cli add refuses to silently replace; close frees the paper slot
# --------------------------------------------------------------------------- #
def test_add_refuses_to_overwrite_a_live_position(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(m, "POSITIONS_PATH", tmp_path / "positions.json")
    monkeypatch.setattr(m, "PAPER_PATH", tmp_path / "paper.json")
    assert m.cli(["add", "BTCUSDT", "long", "100", "95"]) == 0
    # Simulate the trade having progressed.
    pos = m.load_positions()
    pos[0].update(breakeven_done=True, trail_stop=104.0)
    m.save_positions(pos)

    rc = m.cli(["add", "BTCUSDT", "long", "200", "190"])
    assert rc == 2
    assert "already tracked" in capsys.readouterr().out
    kept = m.load_positions()
    assert len(kept) == 1
    assert kept[0]["entry"] == 100 and kept[0]["trail_stop"] == 104.0


def test_close_releases_the_paper_slot(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "POSITIONS_PATH", tmp_path / "positions.json")
    monkeypatch.setattr(m, "PAPER_PATH", tmp_path / "paper.json")
    m.save_positions([{"symbol": "BNBUSDT", "direction": "long",
                       "entry": 700.0, "stop": 690.0, "mode": "reversal"}])
    m.save_paper([{"symbol": "BNBUSDT", "status": "open", "r": 0.0}])
    assert m.open_slots(m.load_paper(), m.load_positions()) == 1

    assert m.cli(["close", "BNBUSDT"]) == 0
    assert m.open_slots(m.load_paper(), m.load_positions()) == 0
    assert m.load_paper()[0]["status"] == "closed_manually"


def test_close_on_an_untracked_symbol_still_reports_cleanly(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "POSITIONS_PATH", tmp_path / "positions.json")
    monkeypatch.setattr(m, "PAPER_PATH", tmp_path / "paper.json")
    m.save_positions([])
    assert m.cli(["close", "ETHUSDT"]) == 1


# --------------------------------------------------------------------------- #
# the backtest clock matches the VM's :07 cron
# --------------------------------------------------------------------------- #
def test_backtest_uses_the_cron_offset():
    import inspect
    assert B.CRON_OFFSET.total_seconds() == 7 * 60
    assert "+ CRON_OFFSET" in inspect.getsource(B.backtest)


# --------------------------------------------------------------------------- #
# entered_demand is no longer dead code
# --------------------------------------------------------------------------- #
def test_entered_demand_uses_proximity_not_impossible_containment():
    zone = {"top": 99.0, "bottom": 97.0, "kind": "ob"}
    # Price just above an OPEN zone — containment is impossible by definition,
    # proximity is the real question. A wick into it must count.
    assert m.reached_zone(zone, 99.2, 98.5, 99.4) is True
    assert m.reached_zone(zone, 120.0, 119.0, 121.0) is False
