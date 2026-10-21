"""
The plan's drawdown rule (Section 7, 2026-10-20): review at -$60 below the
paper record's high, paper-only at -$100. It replaced the daily/weekly/
monthly loss breaks after a portfolio replay showed those breaks cost money
without shrinking the drop — the system's real risk is a slow bleed over
months that no calendar limit sees.
"""
import standalone_monitor as m
from test_capped_dedup import _run


def _closed(r, day, risk=2.0):
    status = "win" if r > 0 else "loss"
    return {"symbol": "BTCUSDT", "status": status, "r": r, "risk_pct_used": risk,
            "opened": f"2026-09-{day:02d}T07:07:00+00:00",
            "closed": f"2026-09-{day:02d}T12:07:00+00:00"}


def _losses(n, start_day=1):
    # At 2% of $200 every -1R is -$4.
    return [_closed(-1.0, start_day + i) for i in range(n)]


# --------------------------------------------------------------------------- #
# the measurement
# --------------------------------------------------------------------------- #
def test_drawdown_is_measured_from_the_high_not_from_zero():
    # +$40 high, then -$24: equity +$16, but the drop that matters is $24.
    trades = [_closed(5.0, 1), _closed(5.0, 2)] + _losses(6, start_day=3)
    equity, dd = m.paper_drawdown(trades)
    assert round(equity, 2) == 16.0
    assert round(dd, 2) == -24.0


def test_drawdown_walks_trades_in_the_order_they_closed():
    # Listed out of order. Closed in order: win, then six losses -> high was
    # +$20, drop is $24. Walking the list order instead would see the losses
    # first and report a smaller drop from a $0 high.
    trades = _losses(6, start_day=3) + [_closed(5.0, 1)]
    _, dd = m.paper_drawdown(trades)
    assert round(dd, 2) == -24.0


def test_drawdown_uses_each_trades_real_risk():
    # A continuation trade sized up to 4% loses $8 per R, not $4.
    _, dd = m.paper_drawdown([_closed(-1.0, 1, risk=4.0)])
    assert round(dd, 2) == -8.0


def test_open_trades_do_not_count():
    trades = _losses(3) + [{"symbol": "ETHUSDT", "status": "open", "r": 0.0}]
    _, dd = m.paper_drawdown(trades)
    assert round(dd, 2) == -12.0


# --------------------------------------------------------------------------- #
# the levels
# --------------------------------------------------------------------------- #
def test_no_alert_above_the_review_line():
    assert m.drawdown_check(_losses(14), None) == (None, None)       # -$56


def test_review_line_alerts_once():
    level, msg = m.drawdown_check(_losses(15), None)                 # -$60
    assert level == "review" and "DRAWDOWN REVIEW" in msg
    assert m.drawdown_check(_losses(20), "review") == ("review", None)


def test_stop_line_escalates_from_review():
    level, msg = m.drawdown_check(_losses(25), "review")             # -$100
    assert level == "stop" and "DRAWDOWN STOP" in msg


def test_a_partial_recovery_does_not_re_arm_the_alert():
    # Back to -$40: between half the review line and the line. Holding the
    # level stops a record hovering near -$60 from pinging every hour.
    trades = _losses(15) + [_closed(5.0, 20)]                        # -$40
    assert m.drawdown_check(trades, "review") == ("review", None)


def test_a_real_recovery_clears_the_level():
    trades = _losses(15) + [_closed(5.0, 20), _closed(5.0, 21)]      # -$20
    assert m.drawdown_check(trades, "review") == (None, None)


# --------------------------------------------------------------------------- #
# wired into the hourly run
# --------------------------------------------------------------------------- #
def test_the_monitor_sends_the_review_alert_and_remembers_it(monkeypatch, tmp_path):
    out = _run(monkeypatch, tmp_path, state={}, paper=_losses(15), mode="quiet")
    assert any("DRAWDOWN REVIEW" in s for s in out["sent"])
    assert out["state"]["drawdown_alert"] == "review"

    again = _run(monkeypatch, tmp_path, state=out["state"], paper=_losses(15), mode="quiet")
    assert not any("DRAWDOWN" in s for s in again["sent"])


def test_a_failed_push_does_not_mark_the_drawdown_as_told(monkeypatch, tmp_path):
    out = _run(monkeypatch, tmp_path, state={}, paper=_losses(15), mode="quiet",
               send_ok=False)
    assert "drawdown_alert" not in out["state"]
