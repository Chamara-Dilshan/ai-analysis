"""
Regression tests for the second audit (2026-09-18). Three confirmed defects,
all of the house shape: no crash, no failing test, a wrong answer that reads
as a healthy one.

  1. update_paper_trade trailed to a swing on the wrong side of price and
     booked a "win" at a level that never traded — always in profit, so it
     silently flattered the paper journal, the one record treated as clean.
  2. send_telegram returned None, so a failed push still marked the alert
     delivered. A setup dedup key never recurs -> suppressed forever.
  3. pick_demand_zone/pick_supply_zone chose the most EXTREME zone, not the
     nearest, so one wide stale FVG captured the choice and its far edge then
     failed the fee gate — reading as "no setup".
"""
from datetime import datetime, timezone

import standalone_monitor as m

NOW = datetime(2026, 9, 18, 12, 7, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# 1. paper journal must not book an untraded exit
# --------------------------------------------------------------------------- #
def _trade(direction="long", entry=100.0, stop=95.0, **kw):
    t = {"symbol": "BTCUSDT", "mode": "continuation", "direction": direction,
         "entry": entry, "stop": stop, "fee_pct": 2.0, "status": "open",
         "target": entry + 2 * (entry - stop) if direction == "long"
         else entry - 2 * (stop - entry)}
    t.update(kw)
    return t


def _r(price, low, high, **kw):
    d = {"price": price, "bar_low": low, "bar_high": high}
    d.update(kw)
    return d


def test_paper_trail_refuses_a_swing_above_price_on_a_long():
    # Spiked to 107, fell back to 101. The confirmed 4H low is 106 — above
    # price. Parking the stop there books +1.15R that never traded.
    t = _trade()
    m.update_paper_trade(t, _r(101.0, 100.8, 101.5, recent_low_4h=106.0), NOW)
    assert t["current_stop"] < 101.0
    assert t["status"] == "open"


def test_paper_trail_refuses_a_swing_below_price_on_a_short():
    t = _trade(direction="short", entry=100.0, stop=105.0)
    m.update_paper_trade(t, _r(99.0, 98.5, 99.5, recent_high_4h=94.0), NOW)
    assert t["current_stop"] > 99.0
    assert t["status"] == "open"


def test_paper_trail_still_tightens_when_the_swing_is_on_the_right_side():
    t = _trade()
    m.update_paper_trade(t, _r(110.0, 109.0, 111.0, recent_low_4h=104.0), NOW)
    assert t["current_stop"] == 104.0


def test_paper_trail_never_loosens():
    t = _trade(current_stop=104.0)
    m.update_paper_trade(t, _r(110.0, 109.0, 111.0, recent_low_4h=99.0), NOW)
    assert t["current_stop"] == 104.0


# --------------------------------------------------------------------------- #
# 2. send_telegram reports delivery truthfully
# --------------------------------------------------------------------------- #
def test_send_telegram_false_without_credentials(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert m.send_telegram("hi") is False


def test_send_telegram_false_on_http_error(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    monkeypatch.setattr(m.httpx, "post",
                        lambda *a, **k: type("R", (), {"status_code": 429})())
    assert m.send_telegram("hi") is False


def test_send_telegram_false_on_exception(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")

    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(m.httpx, "post", boom)
    assert m.send_telegram("hi") is False


def test_send_telegram_true_on_success(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    monkeypatch.setattr(m.httpx, "post",
                        lambda *a, **k: type("R", (), {"status_code": 200})())
    assert m.send_telegram("hi") is True


# --------------------------------------------------------------------------- #
# 3. zone selection picks the nearest, not the most extreme
# --------------------------------------------------------------------------- #
def _struct(obs=(), fvgs=()):
    return {"order_blocks": {"open": [
                {"type": t, "top": hi, "bottom": lo} for t, lo, hi in obs]},
            "fair_value_gaps": {"open": [
                {"type": t, "top": hi, "bottom": lo} for t, lo, hi in fvgs]}}


def test_demand_zone_prefers_the_nearest_not_the_widest_stale_one():
    price = 100.0
    # A real OB just below price, plus a huge stale FVG spanning 40-160.
    s = _struct(obs=[("bullish", 97.0, 99.0)], fvgs=[("bullish", 40.0, 160.0)])
    z = m.pick_demand_zone(s, price)
    assert (z["bottom"], z["top"]) == (97.0, 99.0)


def test_demand_zone_without_the_stale_zone_is_unchanged():
    s = _struct(obs=[("bullish", 97.0, 99.0)])
    assert m.pick_demand_zone(s, 100.0)["top"] == 99.0


def test_supply_zone_prefers_the_nearest_above_price():
    s = _struct(obs=[("bearish", 101.0, 103.0)], fvgs=[("bearish", 105.0, 400.0)])
    z = m.pick_supply_zone(s, 100.0)
    assert (z["bottom"], z["top"]) == (101.0, 103.0)


def test_no_qualifying_zone_still_returns_none():
    assert m.pick_demand_zone(_struct(), 100.0) is None
    assert m.pick_supply_zone(_struct(), 100.0) is None
