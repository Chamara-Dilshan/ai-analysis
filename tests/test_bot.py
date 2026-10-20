"""
The Telegram command bot (bot.py): /took, /closed, /status, /pause, /resume,
and the monitor's side of /pause. The real-trade journal it writes is the
only record of what was actually traded, so the arithmetic and the refusals
matter as much as the happy path.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

import bot
import standalone_monitor as m
from test_capped_dedup import _run

NOW = datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def files(tmp_path, monkeypatch):
    for name, fname in (("STATE_PATH", "monitor_state.json"),
                        ("POSITIONS_PATH", "positions.json"),
                        ("PAPER_PATH", "paper_trades.json"),
                        ("LOG_PATH", "monitor_log.txt"),
                        ("LOCK_PATH", "monitor.lock")):
        monkeypatch.setattr(m, name, tmp_path / fname)
    monkeypatch.setattr(bot, "REAL_PATH", tmp_path / "real_trades.json")

    def write(name, data):
        (tmp_path / name).write_text(json.dumps(data))

    def read(name):
        p = tmp_path / name
        return json.loads(p.read_text()) if p.exists() else None

    return write, read


def _alert(symbol="ETHUSDT", direction="short", entry=2616.30, stop=2692.46,
           mode="reversal", risk=2.0):
    return {"symbol": symbol, "mode": mode, "direction": direction,
            "entry": entry, "stop": stop, "risk_pct_used": risk,
            "opened": "2026-10-21T07:07:00+00:00", "status": "open"}


# --------------------------------------------------------------------------- #
# /took
# --------------------------------------------------------------------------- #
def test_took_copies_the_alert_and_starts_managing_it(files):
    write, read = files
    write("paper_trades.json", [_alert()])
    reply = bot.handle("/took eth", NOW)
    assert reply.startswith("Logged: ETHUSDT SHORT @ 2616.3")
    pos, = read("positions.json")
    assert (pos["symbol"], pos["direction"], pos["stop"], pos["mode"]) == \
        ("ETHUSDT", "short", 2692.46, "reversal")
    real, = read("real_trades.json")
    assert real["status"] == "open" and real["source"] == "alert"


def test_took_records_the_real_fill_and_says_how_it_compares(files):
    write, read = files
    write("paper_trades.json", [_alert()])
    reply = bot.handle("/took ETH 2,610", NOW)
    assert "worse by 6.3" in reply                 # a short filled lower
    real, = read("real_trades.json")
    assert (real["entry"], real["alert_entry"]) == (2610.0, 2616.30)


def test_took_without_an_alert_explains_the_manual_form(files):
    _, read = files
    reply = bot.handle("/took BTC", NOW)
    assert "/took BTC long|short ENTRY STOP" in reply
    assert read("real_trades.json") is None


def test_took_by_hand_with_no_alert(files):
    _, read = files
    reply = bot.handle("/took sol long 104.3 100.2 continuation", NOW)
    assert reply.startswith("Logged: SOLUSDT LONG @ 104.3")
    assert read("positions.json")[0]["mode"] == "continuation"
    assert read("real_trades.json")[0]["source"] == "manual"


def test_took_refuses_a_fill_already_past_the_stop(files):
    write, read = files
    write("paper_trades.json", [_alert()])
    reply = bot.handle("/took ETH 2700", NOW)
    assert "wrong side" in reply
    assert read("positions.json") is None


def test_took_refuses_to_double_log(files):
    write, read = files
    write("paper_trades.json", [_alert()])
    bot.handle("/took ETH", NOW)
    reply = bot.handle("/took ETH", NOW)
    assert "already" in reply
    assert len(read("real_trades.json")) == 1 and len(read("positions.json")) == 1


# --------------------------------------------------------------------------- #
# /closed
# --------------------------------------------------------------------------- #
def test_closed_scores_the_trade_in_r_and_dollars(files):
    write, read = files
    write("paper_trades.json", [_alert()])
    bot.handle("/took ETH", NOW)
    reply = bot.handle("/closed ETH 2463.98", NOW + timedelta(hours=33))
    real, = read("real_trades.json")
    # The real 2026-10-07 trade: 152.32 / 76.16 = exactly 2R, $8 at 2% of $200.
    assert real["status"] == "win" and real["r"] == pytest.approx(2.0, abs=1e-3)
    assert real["usd"] == pytest.approx(8.0, abs=0.01)
    assert "+2.00R" in reply and "+$8.00" in reply
    assert read("positions.json") == []              # management stops


def test_closed_a_loser(files):
    write, read = files
    bot.handle("/took btc long 80000 79000", NOW)
    bot.handle("/closed btc 79000", NOW)
    real, = read("real_trades.json")
    assert real["status"] == "loss" and real["r"] == pytest.approx(-1.0)


def test_closed_works_after_the_monitor_already_saw_the_stop(files):
    # The hourly run drops a stopped position from positions.json; the real
    # trade stays open until he logs his actual exit.
    write, read = files
    bot.handle("/took btc long 80000 79000", NOW)
    write("positions.json", [])
    assert "monitor saw it exit" in bot.handle("/status", NOW)
    bot.handle("/closed btc 78950", NOW)
    assert read("real_trades.json")[0]["status"] == "loss"


def test_closed_with_nothing_open(files):
    assert "No open ETHUSDT trade" in bot.handle("/closed ETH 2400", NOW)


def test_a_bad_price_gets_a_reply_not_a_crash(files):
    assert "Couldn't read a price" in bot.handle("/closed ETH abc", NOW)


# --------------------------------------------------------------------------- #
# /status, /pause, /resume, help
# --------------------------------------------------------------------------- #
def test_status_reports_health_and_both_records(files):
    write, _ = files
    write("monitor_state.json", {"last_run": (NOW - timedelta(minutes=53)).isoformat()})
    write("paper_trades.json", [])
    reply = bot.handle("/status", NOW)
    assert "last run 53 min ago - healthy" in reply
    assert "Setup alerts: on." in reply
    assert "Real record: no closed trades yet" in reply


def test_status_flags_a_stalled_monitor(files):
    write, _ = files
    write("monitor_state.json", {"last_run": (NOW - timedelta(hours=3)).isoformat()})
    assert "no completed run for 3h" in bot.handle("/status", NOW)


def test_pause_and_resume(files):
    _, read = files
    assert "paused until 2026-10-21 21:00" in bot.handle("/pause 12", NOW)
    assert m.paused_until(read("monitor_state.json"), NOW) is not None
    assert "PAUSED" in bot.handle("/status", NOW)
    assert bot.handle("/resume", NOW) == "Setup alerts back on."
    assert "paused_until" not in read("monitor_state.json")


def test_unknown_text_gets_help(files):
    assert bot.handle("hello", NOW).startswith("Commands:")
    assert bot.handle("/nonsense", NOW).startswith("Commands:")


# --------------------------------------------------------------------------- #
# the monitor's side of /pause
# --------------------------------------------------------------------------- #
def test_a_paused_monitor_holds_back_the_setup_but_still_journals_it(monkeypatch, tmp_path):
    until = (datetime(2026, 10, 8, 14, 7, tzinfo=timezone.utc) + timedelta(hours=5)).isoformat()
    out = _run(monkeypatch, tmp_path, state={"paused_until": until}, paper=[])
    assert not any(s.startswith("SETUP READY") for s in out["sent"])
    assert len(out["paper"]) == 1                    # evidence still recorded
    assert "held back 1 setup alert" in out["log"]


def test_an_expired_pause_alerts_normally_and_offers_took(monkeypatch, tmp_path):
    out = _run(monkeypatch, tmp_path,
               state={"paused_until": "2026-10-08T13:00:00+00:00"}, paper=[])
    setup = [s for s in out["sent"] if s.startswith("SETUP READY")]
    assert setup and "/took ETHUSDT" in setup[0]
