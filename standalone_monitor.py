"""
standalone_monitor.py — hourly BTC+ETH level check, fully independent of any
Claude session. Meant to be invoked hourly by Windows Task Scheduler.

Reuses the SAME tested engine as the MCP server (not a reimplementation):
  - server._get / server._fetch_klines  (HTTP fetch + retry/backoff/cache)
  - structure.analyze                    (ICT/SMC swings/BOS/CHoCH/FVG/OB engine)
  - risk.position_size                   (plan position-sizing math)

Honest caveat: level-picking here is a simpler MECHANICAL rule than a live
chat analysis applies —
  - demand/support zone = nearest bullish 4H order-block/FVG below price
  - bull-confirm level  = the highest recent 4H swing high
  - bear-confirm level  = the lowest recent 4H swing low
Treat this as a first-pass screener. When it fires, go ask Claude for the
full multi-timeframe read rather than trusting this alone.

State persists in monitor_state.json so a fresh hourly process only alerts on
a NEW condition, not a still-standing one (mirrors the chat monitor's
behavior). Every run appends to monitor_log.txt — that file is the reliable
record. A trigger additionally writes latest_alert.txt and best-effort fires
a native Windows toast (toast rendering depends on OS notification settings;
never assume it worked — check monitor_log.txt).
"""
import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

import server
import structure
import risk

HERE = Path(__file__).parent
STATE_PATH = HERE / "monitor_state.json"
LOG_PATH = HERE / "monitor_log.txt"
ALERT_PATH = HERE / "latest_alert.txt"

SYMBOLS = ["BTCUSDT", "ETHUSDT"]
SL_OFFSET = timedelta(hours=5, minutes=30)

# When running headless (CI, VPS cron) there is no console and no Windows
# toast — echo the log to stdout so the run output is the record instead.
ECHO_STDOUT = os.environ.get("MONITOR_ECHO_STDOUT") == "1"


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def log(text: str) -> None:
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(text.rstrip("\n") + "\n")
    if ECHO_STDOUT:
        try:
            print(text.rstrip("\n"), flush=True)
        except Exception:
            pass


def send_telegram(message: str) -> None:
    """Best-effort Telegram push, for runs where nobody is looking at this
    machine. Credentials come from the environment only — never hard-code a
    token here. Same contract as show_toast: a notification failure must
    never affect the monitor."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message,
                  "disable_web_page_preview": True},
            timeout=15,
        )
        if resp.status_code != 200:
            log(f"(telegram failed: HTTP {resp.status_code})")
    except Exception as e:
        log(f"(telegram failed: {e})")


def show_toast(title: str, message: str) -> None:
    """Best-effort native Windows toast, no extra pip dependency. Never let a
    notification failure affect the monitor — monitor_log.txt is the source
    of truth regardless of whether this visually renders."""
    if sys.platform != "win32":
        return

    def esc(s: str) -> str:
        return s.replace("`", "``").replace('"', '`"').replace("$", "`$")

    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "[Windows.UI.Notifications.ToastNotificationManager,"
        " Windows.UI.Notifications, ContentType=WindowsRuntime] > $null;"
        "[Windows.Data.Xml.Dom.XmlDocument,"
        " Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] > $null;"
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument;"
        f"$xml.LoadXml(\"<toast><visual><binding template='ToastGeneric'>"
        f"<text>{esc(title)}</text><text>{esc(message)}</text>"
        f"</binding></visual></toast>\");"
        "$toast = New-Object Windows.UI.Notifications.ToastNotification $xml;"
        "[Windows.UI.Notifications.ToastNotificationManager]::"
        "CreateToastNotifier('BTC-ETH Monitor').Show($toast)"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
            capture_output=True, timeout=15,
        )
    except Exception:
        pass


def footer(now_utc: datetime) -> str:
    sl = now_utc + SL_OFFSET
    next_close_hour = ((now_utc.hour // 4) + 1) * 4
    next_close = now_utc.replace(minute=0, second=0, microsecond=0)
    if next_close_hour >= 24:
        next_close = next_close.replace(hour=0) + timedelta(days=1)
    else:
        next_close = next_close.replace(hour=next_close_hour)
    mins_left = int((next_close - now_utc).total_seconds() // 60)
    sl_note = " (+1d)" if sl.date() != now_utc.date() else ""
    return (
        f"UTC {now_utc.strftime('%H:%M')} | "
        f"SL {sl.strftime('%H:%M')}{sl_note} (UTC+5:30) | "
        f"next 4H close ~{mins_left} min"
    )


def pick_demand_zone(struct_4h: dict, price: float):
    zones = []
    for ob in struct_4h["order_blocks"]["open"]:
        if ob["type"] == "bullish" and ob["top"] < price:
            zones.append((ob["top"], ob["bottom"]))
    for fvg in struct_4h["fair_value_gaps"]["open"]:
        if fvg["type"] == "bullish" and fvg["top"] < price:
            zones.append((fvg["top"], fvg["bottom"]))
    if not zones:
        return None
    top, bottom = max(zones, key=lambda z: z[0])
    return {"top": top, "bottom": bottom}


def pick_bull_confirm(struct_4h: dict):
    highs = [h["price"] for h in struct_4h["swings"]["recent_highs"]]
    return max(highs) if highs else None


def pick_bear_confirm(struct_4h: dict):
    lows = [l["price"] for l in struct_4h["swings"]["recent_lows"]]
    return min(lows) if lows else None


def latest_event_time(struct: dict):
    events = struct.get("recent_structure_events") or []
    return events[0]["time"] if events else None


async def check_symbol(symbol: str) -> dict:
    """Fetch + analyze one symbol via the same tested helpers the MCP server
    uses. Returns a plain result dict; does not touch persisted state."""
    price_data = await server._get(
        server.BINANCE_BASE_URL, "/fapi/v1/ticker/24hr", {"symbol": symbol},
    )
    price = float(price_data["lastPrice"])

    funding_data = await server._get(
        server.BINANCE_BASE_URL, "/fapi/v1/premiumIndex", {"symbol": symbol},
    )
    funding_pct = round(float(funding_data["lastFundingRate"]) * 100, 5)

    candles_4h = await server._fetch_klines(symbol, "4h", 200)
    candles_1d = await server._fetch_klines(symbol, "1d", 180)
    struct_4h = structure.analyze(candles_4h, swing_strength=3)
    struct_1d = structure.analyze(candles_1d, swing_strength=2)

    return {
        "price": price,
        "funding_pct": funding_pct,
        "trend_1d": struct_1d.get("trend"),
        "trend_4h": struct_4h.get("trend"),
        "demand": pick_demand_zone(struct_4h, price),
        "bull_confirm": pick_bull_confirm(struct_4h),
        "bear_confirm": pick_bear_confirm(struct_4h),
        "event_1d_time": latest_event_time(struct_1d),
        "event_4h_time": latest_event_time(struct_4h),
        "recent_low_4h": (struct_4h["swings"]["recent_lows"][0]["price"]
                           if struct_4h["swings"]["recent_lows"] else None),
        "recent_high_4h": (struct_4h["swings"]["recent_highs"][0]["price"]
                            if struct_4h["swings"]["recent_highs"] else None),
    }


def format_math(label: str, entry: float, stop: float) -> str:
    try:
        m = risk.position_size(entry=entry, stop_loss=stop, capital=200,
                                risk_pct=2, leverage=5)
        verdict = "PASS" if m["passes_plan"] else "FAIL"
        return (
            f"    {label} math (plan check, NOT a recommendation): "
            f"{m['direction'].upper()} @ {entry} / stop {stop} -> {verdict}; "
            f"size {m['position']['size_btc']} BTC "
            f"(${m['position']['notional_usd']}), "
            f"margin@5x ${m['position']['margin_by_leverage_usd']['5x']}, "
            f"approx liq {m['approx_liquidation_price']}, "
            f"targets 1:2={m['plan_target_prices']['1:2']} "
            f"1:3={m['plan_target_prices']['1:3']}"
        )
    except Exception as e:
        return f"    (math skipped: {e})"


async def main() -> None:
    now = datetime.now(timezone.utc)
    state = load_state()
    lines = [f"=== {now.isoformat()} ==="]
    alert_lines = []

    for symbol in SYMBOLS:
        sym_state = state.get(symbol, {})
        try:
            r = await check_symbol(symbol)
        except Exception as e:
            lines.append(f"{symbol}: ERROR fetching/analyzing - {e}")
            state[symbol] = sym_state
            continue

        price = r["price"]
        triggers = []

        was_in = sym_state.get("was_in_demand", False)
        in_now = bool(r["demand"] and r["demand"]["bottom"] <= price <= r["demand"]["top"])
        if in_now and not was_in:
            triggers.append(("entered_demand", None))
        sym_state["was_in_demand"] = in_now

        broke_bull = sym_state.get("broke_bull_confirm", False)
        bull_now = bool(r["bull_confirm"] and price > r["bull_confirm"])
        if bull_now and not broke_bull:
            triggers.append(("broke_bull_confirm", None))
        sym_state["broke_bull_confirm"] = bull_now

        broke_bear = sym_state.get("broke_bear_confirm", False)
        bear_now = bool(r["bear_confirm"] and price < r["bear_confirm"])
        if bear_now and not broke_bear:
            triggers.append(("broke_bear_confirm", None))
        sym_state["broke_bear_confirm"] = bear_now

        if r["event_4h_time"] and r["event_4h_time"] != sym_state.get("last_4h_event"):
            if sym_state.get("last_4h_event") is not None:
                triggers.append(("new_4h_event", r["event_4h_time"]))
            sym_state["last_4h_event"] = r["event_4h_time"]
        if r["event_1d_time"] and r["event_1d_time"] != sym_state.get("last_1d_event"):
            if sym_state.get("last_1d_event") is not None:
                triggers.append(("new_1d_event", r["event_1d_time"]))
            sym_state["last_1d_event"] = r["event_1d_time"]

        demand_str = (f"{r['demand']['bottom']}-{r['demand']['top']}"
                      if r["demand"] else "none nearby")
        lines.append(
            f"{symbol}: ${price} | 1D {r['trend_1d']} / 4H {r['trend_4h']} | "
            f"funding {r['funding_pct']}% | support {demand_str} | "
            f"bull>{r['bull_confirm']} | bear<{r['bear_confirm']}"
        )

        for kind, event_time in triggers:
            if kind == "entered_demand":
                alert_lines.append(
                    f"[ALERT] {symbol} entered support zone {demand_str} @ ${price}"
                )
                alert_lines.append(format_math("Support-bounce", price, r["demand"]["bottom"]))
            elif kind == "broke_bull_confirm":
                alert_lines.append(
                    f"[ALERT] {symbol} broke ABOVE {r['bull_confirm']} @ ${price} (bullish)"
                )
                if r["recent_low_4h"]:
                    stop = r["recent_low_4h"]
                elif r["demand"]:
                    stop = r["demand"]["bottom"]
                else:
                    stop = round(price * 0.98, 2)
                alert_lines.append(format_math("Breakout", price, stop))
            elif kind == "broke_bear_confirm":
                alert_lines.append(
                    f"[ALERT] {symbol} broke BELOW {r['bear_confirm']} @ ${price} (bearish)"
                )
                stop = r["recent_high_4h"] or round(price * 1.02, 2)
                alert_lines.append(format_math("Breakdown", price, stop))
            elif kind in ("new_4h_event", "new_1d_event"):
                tf = "4H" if kind == "new_4h_event" else "1D"
                alert_lines.append(
                    f"[ALERT] {symbol} new {tf} structure event at {event_time}"
                )

        state[symbol] = sym_state

    lines.append(footer(now))
    log("\n".join(lines))
    save_state(state)

    if alert_lines:
        alert_text = "\n".join(alert_lines) + "\n" + footer(now)
        ALERT_PATH.write_text(alert_text, encoding="utf-8")
        show_toast("BTC/ETH level alert", alert_lines[0])
        send_telegram("BTC/ETH level alert\n\n" + alert_text)
        log("--- ALERT ---\n" + alert_text)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        log(f"FATAL: {e}")
        raise
