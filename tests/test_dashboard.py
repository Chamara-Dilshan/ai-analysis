"""The HTML dashboard (dashboard.py) and how it reaches Telegram."""
import json
from datetime import datetime, timedelta, timezone

import bot
import dashboard
import standalone_monitor as m
from test_capped_dedup import _run

NOW = datetime(2026, 10, 25, 6, 7, tzinfo=timezone.utc)      # a Sunday


def _paper(rs, risk=2.0):
    return [{"symbol": "ETHUSDT", "mode": "reversal" if i % 2 else "continuation",
             "direction": "short", "entry": 2600 + i, "risk_pct_used": risk,
             "status": "win" if r > 0 else "loss", "r": r,
             "closed": f"2026-10-{i + 1:02d}T12:00:00+00:00"} for i, r in enumerate(rs)]


def test_dashboard_reports_the_records_and_the_range():
    page = dashboard.build_dashboard(_paper([2.0, -1.0, -1.0]), [], NOW, "abc1234 test",
                                     sequences=[[1, -1, -1, 1] * 25])
    assert page.startswith("<!doctype html>")
    assert "+0.0R" in page and "3 trades" in page            # paper tile
    assert "abc1234 test" in page
    assert 'class="cone"' in page and 'class="line paper"' in page
    data = json.loads(page.split("const D = ")[1].split(";\n")[0])
    assert data["eq"]["paper"] == [2.0, 1.0, 0.0]
    # $8 high, then two -$4 losses: the drawdown walks 0, -4, -8.
    assert data["dd"]["dd"] == [0.0, -4.0, -8.0]


def test_dashboard_with_no_trades_still_renders():
    page = dashboard.build_dashboard([], [], NOW, "", sequences=[[1, -1]])
    assert "No closed trades yet." in page and "unknown version" in page


def test_dollar_ticks_put_the_sign_before_the_dollar():
    s = dashboard._Scale(3, -100.0, 0.0)
    assert ">-$50<" in dashboard._axes(s, "$")


def test_dashboard_command_sends_a_file(monkeypatch, tmp_path):
    for name, fname in (("STATE_PATH", "monitor_state.json"), ("POSITIONS_PATH", "positions.json"),
                        ("PAPER_PATH", "paper_trades.json"), ("REAL_PATH", "real_trades.json"),
                        ("LOCK_PATH", "monitor.lock"), ("LOG_PATH", "monitor_log.txt")):
        monkeypatch.setattr(m, name, tmp_path / fname)
    sent = []
    monkeypatch.setattr(m, "send_document", lambda name, content, caption="": sent.append(name) or True)
    assert "Dashboard sent" in bot.handle("/dashboard", NOW)
    assert sent == ["dashboard-2026-10-25.html"]


def test_the_sunday_run_sends_the_dashboard_with_the_scorecard(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "REAL_PATH", tmp_path / "real_trades.json")
    files = []
    monkeypatch.setattr(m, "send_document", lambda name, content, caption="": files.append(name) or True)
    _run(monkeypatch, tmp_path, state={}, paper=[], mode="quiet", now=NOW)
    assert files == ["dashboard-2026-10-25.html"]
    files.clear()
    _run(monkeypatch, tmp_path, state={}, paper=[], mode="quiet", now=NOW - timedelta(days=1))
    assert files == []
