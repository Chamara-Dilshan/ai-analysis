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


def _run(monkeypatch, tmp_path, *, state, paper, mode="reversal", now=NOW,
         send_ok=True, positions=None, setup_ready=True):
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
    (tmp_path / "positions.json").write_text(json.dumps(positions or []))

    monkeypatch.setattr(m, "SYMBOLS", ["ETHUSDT"])
    monkeypatch.setattr(m, "REVERSAL_SYMBOLS",
                        ["ETHUSDT"] if mode in ("reversal", "headsup") else [])
    monkeypatch.setattr(m, "CONTINUATION_SYMBOLS",
                        ["ETHUSDT"] if mode == "continuation" else [])

    async def _check_symbol(symbol, since_utc=None):
        return _read()

    async def _news(now_utc):
        return False, "news clear"

    monkeypatch.setattr(m, "check_symbol", _check_symbol)
    monkeypatch.setattr(m, "check_news_blackout", _news)
    def _setup(*a, **k):
        if mode == "reversal" and setup_ready:
            return _ready_setup()
        if mode == "headsup":
            # Aligned and in the zone, but the session window is shut — the
            # exact shape that produces a HEADS UP and nothing else.
            return {"ready": False, "direction": "short", "zone": dict(ZONE),
                    "gates": {"aligned": True, "in_zone": True,
                              "trigger": True, "kill_zone": False,
                              "news_clear": True}}
        return {"ready": False, "gates": {}, "direction": None, "zone": None}

    monkeypatch.setattr(m, "evaluate_setup", _setup)
    monkeypatch.setattr(m, "evaluate_trend_continuation_setup",
                        lambda *a, **k: _ready_cont() if mode == "continuation"
                        else {"ready": False, "gates": {}, "direction": None})
    monkeypatch.setattr(m, "format_setup_alert", lambda *a, **k: "SETUP READY — ETHUSDT SHORT")
    monkeypatch.setattr(m, "format_continuation_alert", lambda *a, **k: "CONTINUATION — ETHUSDT SHORT")
    monkeypatch.setattr(m, "send_telegram",
                        lambda msg: sent.append(msg) or bool(send_ok))
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
        "positions": json.loads((tmp_path / "positions.json").read_text()),
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


# --------------------------------------------------------------------------- #
# A failed push must undo everything that recorded "he was told".
# Same family as the capped bug above: the flag outlived the message.
# None of these had ever fired — the VM has recorded zero send failures —
# so they are latent, which is exactly why they needed tests rather than a log.
# --------------------------------------------------------------------------- #
def test_a_failed_send_drops_the_unsent_paper_trade(monkeypatch, tmp_path):
    out = _run(monkeypatch, tmp_path, state={}, paper=[], send_ok=False)
    assert len(out["sent"]) == 1                  # attempted
    assert out["paper"] == []                     # but not journalled
    assert "dropped 1 unsent paper trade" in out["log"]
    assert "last_setup_key" not in out["state"].get("ETHUSDT", {})


def test_the_retry_after_a_failed_send_journals_the_trade_exactly_once(monkeypatch, tmp_path):
    failed = _run(monkeypatch, tmp_path, state={}, paper=[], send_ok=False,
                  now=NOW - timedelta(hours=1))
    assert failed["paper"] == []

    retry = _run(monkeypatch, tmp_path, state=failed["state"],
                 paper=failed["paper"])
    assert len(retry["sent"]) == 1
    assert len(retry["paper"]) == 1               # one row, not two


def test_a_successful_send_still_journals_the_trade(monkeypatch, tmp_path):
    out = _run(monkeypatch, tmp_path, state={}, paper=[])
    assert len(out["paper"]) == 1
    assert out["paper"][0]["symbol"] == "ETHUSDT"
    assert "dropped" not in out["log"]


def test_a_failed_send_drops_an_unsent_continuation_trade(monkeypatch, tmp_path):
    out = _run(monkeypatch, tmp_path, state={}, paper=[], mode="continuation",
               send_ok=False)
    assert out["paper"] == []
    assert "last_cont_setup_key" not in out["state"].get("ETHUSDT", {})


def test_a_failed_send_does_not_resolve_an_existing_paper_trade_away(monkeypatch, tmp_path):
    # Only trades opened THIS run are dropped. One already on the books must
    # survive, or a send failure would erase history.
    prior = [{"symbol": "BTCUSDT", "mode": "reversal", "direction": "long",
              "entry": 100.0, "stop": 95.0, "target": 110.0,
              "status": "win", "r": 2.0, "risk_pct_used": 2}]
    out = _run(monkeypatch, tmp_path, state={}, paper=prior, send_ok=False)
    assert [t["symbol"] for t in out["paper"]] == ["BTCUSDT"]


# --------------------------------------------------------------------------- #
# heads-up key and heartbeat stamp
# --------------------------------------------------------------------------- #
def test_a_failed_heads_up_send_is_retried(monkeypatch, tmp_path):
    failed = _run(monkeypatch, tmp_path, state={}, paper=[], mode="headsup",
                  send_ok=False)
    assert len(failed["sent"]) == 1
    assert "last_headsup_key" not in failed["state"].get("ETHUSDT", {})

    retry = _run(monkeypatch, tmp_path, state=failed["state"], paper=[],
                 mode="headsup")
    assert len(retry["sent"]) == 1                # he finally hears about it
    assert retry["state"]["ETHUSDT"]["last_headsup_key"] == str(ZONE)


def test_a_delivered_heads_up_is_not_repeated(monkeypatch, tmp_path):
    first = _run(monkeypatch, tmp_path, state={}, paper=[], mode="headsup")
    again = _run(monkeypatch, tmp_path, state=first["state"], paper=[],
                 mode="headsup")
    assert again["sent"] == []                    # same zone, stays quiet


def test_a_failed_heartbeat_is_not_marked_as_sent(monkeypatch, tmp_path):
    at_heartbeat = NOW.replace(hour=m.HEARTBEAT_HOUR_UTC)
    failed = _run(monkeypatch, tmp_path, state={}, paper=[], mode="headsup",
                  now=at_heartbeat, send_ok=False)
    assert any("Daily check-in" in s for s in failed["sent"])
    assert "last_heartbeat" not in failed["state"]

    retry = _run(monkeypatch, tmp_path, state=failed["state"], paper=[],
                 mode="headsup", now=at_heartbeat)
    assert any("Daily check-in" in s for s in retry["sent"])
    assert retry["state"]["last_heartbeat"] == at_heartbeat.date().isoformat()


def test_a_delivered_heartbeat_is_not_repeated(monkeypatch, tmp_path):
    at_heartbeat = NOW.replace(hour=m.HEARTBEAT_HOUR_UTC)
    first = _run(monkeypatch, tmp_path, state={}, paper=[], mode="headsup",
                 now=at_heartbeat)
    again = _run(monkeypatch, tmp_path, state=first["state"], paper=[],
                 mode="headsup", now=at_heartbeat)
    assert not any("Daily check-in" in s for s in again["sent"])


# --------------------------------------------------------------------------- #
# position milestones: the announcement flag must not outlive the message,
# but "closed" must survive, because a stop that was hit was hit either way
# --------------------------------------------------------------------------- #
def _open_long():
    # price in _read() is 2528.52, so this sits at +1.57R — breakeven fires.
    return [{"symbol": "ETHUSDT", "direction": "long", "entry": 2450.0,
             "stop": 2400.0, "target": 2550.0, "mode": "reversal"}]


def test_a_failed_breakeven_message_is_announced_again(monkeypatch, tmp_path):
    failed = _run(monkeypatch, tmp_path, state={}, paper=[], mode="headsup",
                  positions=_open_long(), send_ok=False)
    assert any("BREAKEVEN" in s.upper() for s in failed["sent"])
    # Flag rolled back, so the next run re-announces instead of going quiet.
    assert not failed["positions"][0].get("breakeven_done")

    retry = _run(monkeypatch, tmp_path, state=failed["state"], paper=[],
                 mode="headsup", positions=failed["positions"])
    assert any("BREAKEVEN" in s.upper() for s in retry["sent"])
    assert retry["positions"][0]["breakeven_done"] is True


def test_a_delivered_breakeven_message_is_not_repeated(monkeypatch, tmp_path):
    first = _run(monkeypatch, tmp_path, state={}, paper=[], mode="headsup",
                 positions=_open_long())
    assert first["positions"][0]["breakeven_done"] is True
    again = _run(monkeypatch, tmp_path, state=first["state"], paper=[],
                 mode="headsup", positions=first["positions"])
    assert not any("BREAKEVEN" in s.upper() for s in again["sent"])


def test_a_stopped_out_trade_stays_closed_even_if_the_message_fails(monkeypatch, tmp_path):
    # The one flag that must NOT roll back. Reopening it would leave the
    # position tracked forever, because last_run has already moved past the
    # hour in which the stop was touched.
    stopped = [{"symbol": "ETHUSDT", "direction": "long", "entry": 2600.0,
                "stop": 2590.0, "target": 2620.0, "mode": "reversal"}]
    out = _run(monkeypatch, tmp_path, state={}, paper=[], mode="headsup",
               positions=stopped, send_ok=False)
    assert out["positions"] == []                 # dropped = stayed closed
