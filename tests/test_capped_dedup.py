"""
Regression tests for the 2026-10-09 defect: a setup that was CAPPED (valid,
but the book was full) still wrote its alert-dedup key, so when a slot freed
up while the setup was still live the send was silently suppressed.

The real cost, from the VM's own log and state file:
  13:07 UTC  [SETUP CAPPED] ETHUSDT short (2 open, cap 2)
             -> wrote last_setup_key = "short:2543.15:2576.36"
  14:07 UTC  a slot freed, every gate still passed, and the key written an
             hour earlier suppressed the alert. Price reached the 1:2 target
             within the next candle.

The rule these tests pin down: the dedup key records what was SENT, never
what was merely evaluated.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

import standalone_monitor as m

NOW = datetime(2026, 10, 8, 14, 7, tzinfo=timezone.utc)
ZONE = {"top": 2576.36, "bottom": 2543.15, "kind": "fvg"}
SETUP_KEY = "short:2543.15:2576.36"
CONT_EVENT = {"time": "2026-10-08T12:00:00+00:00", "level": 2535.19,
              "type": "BOS", "direction": "bearish"}
CONT_KEY = "cont:short:2026-10-08T12:00:00+00:00:2535.19"


# --------------------------------------------------------------------------- #
# harness: drive main() with the network, Telegram and disk all stubbed out
# --------------------------------------------------------------------------- #
def _read():
    return {"price": 2528.52, "funding_pct": 0.00139,
            "bar_low": 2516.52, "bar_high": 2535.53,
            "trend_1d": "down", "trend_4h": "down", "trend_1h": "down",
            "event_1h": None, "event_4h": CONT_EVENT, "event_4h_time": None,
            "event_1d_time": None, "candles_1h": [], "candles_4h": [],
            "demand": None, "supply": dict(ZONE),
            "bull_confirm": 2806.76, "bear_confirm": 2357.0,
            "recent_low_4h": None, "recent_high_4h": None}


def _ready_setup():
    return {"ready": True, "direction": "short", "zone": dict(ZONE),
            "gates": {"aligned": True, "in_zone": True, "trigger": True,
                      "kill_zone": True, "news_clear": True, "fee_ok": True},
            "entry": 2528.52, "stop": 2576.36, "target": 2432.84}


def _ready_cont():
    return {"ready": True, "direction": "short", "event": dict(CONT_EVENT),
            "gates": {"trend_aligned_bos": True, "fresh": True,
                      "news_clear": True, "stop_valid": True, "fee_ok": True},
            "entry": 2528.52, "stop": 2576.36, "target": 2432.84}


def _run(monkeypatch, tmp_path, *, state, paper, mode="reversal", now=NOW):
    """Run one main() tick and report what it sent and what state it kept."""
    sent = []

    for name, fname in (("STATE_PATH", "monitor_state.json"),
                        ("POSITIONS_PATH", "positions.json"),
                        ("PAPER_PATH", "paper_trades.json"),
                        ("LOG_PATH", "monitor_log.txt"),
                        ("ALERT_PATH", "latest_alert.txt")):
        monkeypatch.setattr(m, name, tmp_path / fname)

    (tmp_path / "monitor_state.json").write_text(json.dumps(state))
    (tmp_path / "paper_trades.json").write_text(json.dumps(paper))
    (tmp_path / "positions.json").write_text("[]")

    monkeypatch.setattr(m, "SYMBOLS", ["ETHUSDT"])
    monkeypatch.setattr(m, "REVERSAL_SYMBOLS",
                        ["ETHUSDT"] if mode == "reversal" else [])
    monkeypatch.setattr(m, "CONTINUATION_SYMBOLS",
                        ["ETHUSDT"] if mode == "continuation" else [])

    async def _check_symbol(symbol, since_utc=None):
        return _read()

    async def _news(now_utc):
        return False, "news clear"

    monkeypatch.setattr(m, "check_symbol", _check_symbol)
    monkeypatch.setattr(m, "check_news_blackout", _news)
    monkeypatch.setattr(m, "evaluate_setup",
                        lambda *a, **k: _ready_setup() if mode == "reversal"
                        else {"ready": False, "gates": {}, "direction": None,
                              "zone": None})
    monkeypatch.setattr(m, "evaluate_trend_continuation_setup",
                        lambda *a, **k: _ready_cont() if mode == "continuation"
                        else {"ready": False, "gates": {}, "direction": None})
    monkeypatch.setattr(m, "format_setup_alert", lambda *a, **k: "SETUP READY — ETHUSDT SHORT")
    monkeypatch.setattr(m, "format_continuation_alert", lambda *a, **k: "CONTINUATION — ETHUSDT SHORT")
    monkeypatch.setattr(m, "send_telegram", lambda msg: sent.append(msg) or True)
    monkeypatch.setattr(m, "show_toast", lambda *a, **k: None)
    monkeypatch.setattr(m, "ping_healthcheck", lambda ok=True: None)

    class _FrozenNow(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(m, "datetime", _FrozenNow)

    asyncio.run(m.main())
    return {
        "sent": sent,
        "state": json.loads((tmp_path / "monitor_state.json").read_text()),
        "paper": json.loads((tmp_path / "paper_trades.json").read_text()),
        "log": (tmp_path / "monitor_log.txt").read_text(),
    }


def _full_book():
    # Two distinct symbols hold the two slots. Neither is ETHUSDT, so the
    # cap — not a duplicate-symbol rule — is what blocks the setup.
    return [{"symbol": "BTCUSDT", "status": "open", "r": 0.0},
            {"symbol": "BNBUSDT", "status": "open", "r": 0.0}]


# --------------------------------------------------------------------------- #
# the defect itself: capped must not claim the dedup key
# --------------------------------------------------------------------------- #
def test_a_capped_reversal_setup_does_not_record_itself_as_alerted(monkeypatch, tmp_path):
    out = _run(monkeypatch, tmp_path, state={}, paper=_full_book())
    assert out["sent"] == []                      # correctly withheld
    assert "SETUP CAPPED" in out["log"]
    # ...but it must leave no trace that would suppress the next chance.
    assert "last_setup_key" not in out["state"].get("ETHUSDT", {})
    assert "last_setup_at" not in out["state"].get("ETHUSDT", {})


def test_the_alert_fires_the_hour_a_slot_frees(monkeypatch, tmp_path):
    # Exactly the 2026-10-08 sequence: capped at 13:07, free at 14:07.
    capped = _run(monkeypatch, tmp_path, state={}, paper=_full_book(),
                  now=NOW - timedelta(hours=1))
    assert capped["sent"] == []

    freed = _run(monkeypatch, tmp_path, state=capped["state"],
                 paper=[{"symbol": "BTCUSDT", "status": "win", "r": 2.0},
                        {"symbol": "BNBUSDT", "status": "loss", "r": -1.0}])
    assert len(freed["sent"]) == 1
    assert "SETUP READY" in freed["sent"][0]
    assert freed["state"]["ETHUSDT"]["last_setup_key"] == SETUP_KEY


def test_a_capped_continuation_setup_does_not_record_itself_as_alerted(monkeypatch, tmp_path):
    # Worse than the reversal case: cont_key is pinned to one 4H event and
    # never goes stale, so a key written here silenced that break forever.
    out = _run(monkeypatch, tmp_path, state={}, paper=_full_book(),
               mode="continuation")
    assert out["sent"] == []
    assert "CONTINUATION CAPPED" in out["log"]
    assert "last_cont_setup_key" not in out["state"].get("ETHUSDT", {})


def test_a_freed_slot_releases_the_continuation_alert(monkeypatch, tmp_path):
    capped = _run(monkeypatch, tmp_path, state={}, paper=_full_book(),
                  mode="continuation", now=NOW - timedelta(hours=1))
    assert capped["sent"] == []

    freed = _run(monkeypatch, tmp_path, state=capped["state"], paper=[],
                 mode="continuation")
    assert len(freed["sent"]) == 1
    assert freed["state"]["ETHUSDT"]["last_cont_setup_key"] == CONT_KEY


# --------------------------------------------------------------------------- #
# the fix must not reopen the noise the dedup exists to prevent
# --------------------------------------------------------------------------- #
def test_a_sent_setup_still_suppresses_the_same_setup_next_hour(monkeypatch, tmp_path):
    first = _run(monkeypatch, tmp_path, state={}, paper=[],
                 now=NOW - timedelta(hours=1))
    assert len(first["sent"]) == 1

    again = _run(monkeypatch, tmp_path, state=first["state"],
                 paper=first["paper"])
    assert again["sent"] == []     # same zone, under 6h — stays quiet


def test_a_sent_setup_may_repeat_once_it_goes_stale(monkeypatch, tmp_path):
    first = _run(monkeypatch, tmp_path, state={}, paper=[],
                 now=NOW - timedelta(hours=7))
    assert len(first["sent"]) == 1

    later = _run(monkeypatch, tmp_path, state=first["state"], paper=[])
    assert len(later["sent"]) == 1   # >6h old, allowed to alert again


def test_a_sent_continuation_still_suppresses_the_same_4h_event(monkeypatch, tmp_path):
    first = _run(monkeypatch, tmp_path, state={}, paper=[], mode="continuation",
                 now=NOW - timedelta(hours=1))
    assert len(first["sent"]) == 1

    again = _run(monkeypatch, tmp_path, state=first["state"], paper=[],
                 mode="continuation")
    assert again["sent"] == []       # same break, never re-sent


def test_capped_setup_records_no_paper_trade(monkeypatch, tmp_path):
    # A withheld alert must not quietly enter the journal either, or the
    # record would claim a trade he was never told about.
    out = _run(monkeypatch, tmp_path, state={}, paper=_full_book())
    assert [t for t in out["paper"] if t["symbol"] == "ETHUSDT"] == []
