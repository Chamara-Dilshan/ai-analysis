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

# The plan's trading windows, UTC. A setup outside these is prep, not a trade.
KILL_ZONES = (("London", 7, 9), ("New York", 13, 15))

# How recent a 1H structure event must be to count as "the trigger" rather
# than old history. Three 1H candles keeps it current without demanding the
# alert land in the same hour the trigger printed.
TRIGGER_MAX_AGE = timedelta(hours=3)

# Capital plan — must match risk.py's defaults and the MCP server.
CAPITAL, RISK_PCT, LEVERAGE = 200, 2, 5

# One "still alive" message per day, so silence is never ambiguous: no
# heartbeat means the monitor is down, not that the market is quiet.
HEARTBEAT_HOUR_UTC = 6

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
        # The token is in the request URL, and some exception strings carry the
        # URL. Scrub it — this log can end up in a CI run log or an uploaded
        # artifact, which may be world-readable.
        detail = str(e).replace(token, "<token>")
        log(f"(telegram failed: {type(e).__name__}: {detail})")


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


def pick_supply_zone(struct_4h: dict, price: float):
    """Mirror of pick_demand_zone — nearest bearish OB/FVG above price, for
    when the higher timeframes are aligned short."""
    zones = []
    for ob in struct_4h["order_blocks"]["open"]:
        if ob["type"] == "bearish" and ob["bottom"] > price:
            zones.append((ob["top"], ob["bottom"]))
    for fvg in struct_4h["fair_value_gaps"]["open"]:
        if fvg["type"] == "bearish" and fvg["bottom"] > price:
            zones.append((fvg["top"], fvg["bottom"]))
    if not zones:
        return None
    top, bottom = min(zones, key=lambda z: z[1])
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


def latest_event(struct: dict):
    """The most recent structure event as a whole dict, so callers can see
    direction and not just timing."""
    events = struct.get("recent_structure_events") or []
    return events[0] if events else None


def is_newer(candidate, previous) -> bool:
    """True if candidate is a structure-event timestamp strictly newer than
    previous. Guards against the swing engine revising its "latest" event
    backwards, which a simple inequality mistakes for fresh news."""
    if not candidate:
        return False
    if not previous:
        return True
    try:
        return datetime.fromisoformat(candidate) > datetime.fromisoformat(previous)
    except Exception:
        return candidate != previous


def active_kill_zone(now_utc: datetime):
    """Name of the kill zone we are inside, or None. Weekends never qualify —
    the plan does not trade them."""
    if now_utc.weekday() >= 5:
        return None
    for name, start, end in KILL_ZONES:
        if start <= now_utc.hour < end:
            return name
    return None


def evaluate_setup(r: dict, now_utc: datetime):
    """Check the full entry checklist, not just whether a level moved.

    A crossed level is an event; a setup is an event that also happens to be
    in the right direction, at the right price, at the right time. This
    returns None unless every gate passes, plus a dict describing which gates
    failed so a near-miss can still be logged usefully.
    """
    gates = {}

    # Gate: higher timeframes agree. Trading against 1D or 4H is the single
    # most expensive mistake available here.
    aligned_long = r["trend_1d"] == "up" and r["trend_4h"] == "up"
    aligned_short = r["trend_1d"] == "down" and r["trend_4h"] == "down"
    gates["aligned"] = aligned_long or aligned_short
    direction = "long" if aligned_long else "short" if aligned_short else None

    # Gate: price is in the 4H zone we care about. For longs that is a
    # bullish OB/FVG below price (discount); shorts are the mirror.
    zone = r["demand"] if direction == "long" else r["supply"]
    in_zone = bool(zone and zone["bottom"] <= r["price"] <= zone["top"])
    gates["in_zone"] = in_zone

    # Gate: the 1H has actually turned inside that zone. Arriving at a level
    # proves nothing on its own — this is the trigger.
    ev = r["event_1h"]
    trigger_ok = False
    if ev and direction:
        want = "bullish" if direction == "long" else "bearish"
        try:
            ev_time = datetime.fromisoformat(ev["time"])
            fresh = (now_utc - ev_time) <= TRIGGER_MAX_AGE
        except Exception:
            fresh = False
        trigger_ok = ev["direction"] == want and fresh
    gates["trigger"] = trigger_ok

    # Gate: the plan's session windows.
    kz = active_kill_zone(now_utc)
    gates["kill_zone"] = kz is not None

    if not all(gates.values()):
        return {"ready": False, "gates": gates, "direction": direction,
                "zone": zone, "kill_zone": kz}

    # Every gate passed — build the actual trade parameters. Stop goes just
    # beyond the zone that is supposed to hold; if it does not hold, the
    # reason for the trade is gone.
    if direction == "long":
        entry, stop = r["price"], round(zone["bottom"] * 0.999, 2)
    else:
        entry, stop = r["price"], round(zone["top"] * 1.001, 2)

    return {"ready": True, "gates": gates, "direction": direction,
            "zone": zone, "kill_zone": kz, "entry": entry, "stop": stop}


def format_setup_alert(symbol: str, r: dict, setup: dict, now_utc: datetime) -> str:
    """The message Chamara actually wants: what triggered, the numbers, and
    the risk-management levels — not just 'a level moved'."""
    entry, stop = setup["entry"], setup["stop"]
    d = setup["direction"].upper()
    try:
        m = risk.position_size(entry=entry, stop_loss=stop, capital=CAPITAL,
                                risk_pct=RISK_PCT, leverage=LEVERAGE)
    except Exception as e:
        return f"SETUP READY {symbol} {d} @ {entry} / stop {stop} (math failed: {e})"

    verdict = "PASS" if m["passes_plan"] else "FAIL"
    warn = ""
    if m.get("warnings"):
        warn = "\nWarnings: " + "; ".join(str(w) for w in m["warnings"])

    # 1R from entry — where the stop moves to break even.
    r_dist = abs(entry - stop)
    breakeven = round(entry + r_dist, 2) if d == "LONG" else round(entry - r_dist, 2)

    return (
        f"SETUP READY — {symbol} {d}\n"
        f"All gates passed ({setup['kill_zone']} kill zone).\n\n"
        f"Entry {entry}  |  Stop {stop}  |  Plan check: {verdict}\n"
        f"Zone {setup['zone']['bottom']}-{setup['zone']['top']}\n"
        f"Size {m['position']['size_btc']} (${m['position']['notional_usd']}), "
        f"margin@{LEVERAGE}x ${m['position']['margin_by_leverage_usd'][f'{LEVERAGE}x']}\n"
        f"Risk ${m['risk_amount_usd']} ({m['inputs']['risk_pct']}%), "
        f"approx liq {m['approx_liquidation_price']}\n"
        f"Targets 1:2={m['plan_target_prices']['1:2']}  "
        f"1:3={m['plan_target_prices']['1:3']}\n"
        f"Fees ~{m['fees']['pct_of_risk']}% of risk\n\n"
        f"Manage: breakeven stop at {breakeven} (1R), then trail behind each "
        f"new 1H swing. Half off at 1:2. Exit on an opposing 1H CHoCH rather "
        f"than waiting for the stop.\n"
        f"1D {r['trend_1d']} / 4H {r['trend_4h']} — aligned.\n"
        f"{warn}\n"
        f"Math only, NOT a recommendation. You decide.\n"
        f"{footer(now_utc)}"
    )


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
    # 1H is where the entry trigger lives — without it the monitor can only
    # report that a level moved, never that a setup formed.
    candles_1h = await server._fetch_klines(symbol, "1h", 200)
    struct_4h = structure.analyze(candles_4h, swing_strength=3)
    struct_1d = structure.analyze(candles_1d, swing_strength=2)
    struct_1h = structure.analyze(candles_1h, swing_strength=2)

    return {
        "price": price,
        "funding_pct": funding_pct,
        "trend_1d": struct_1d.get("trend"),
        "trend_4h": struct_4h.get("trend"),
        "trend_1h": struct_1h.get("trend"),
        "event_1h": latest_event(struct_1h),
        "supply": pick_supply_zone(struct_4h, price),
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
    alert_lines = []      # everything notable, for the log
    telegram_msgs = []    # only what is worth interrupting him for
    summary = []          # one line per symbol, for the daily heartbeat

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

        # Only fire on a STRICTLY NEWER event. Swing detection needs candles
        # either side to confirm, so a provisional event can appear and then
        # vanish, making "latest" jump backwards in time. A plain != treated
        # that regression as news and re-alerted on an event already seen.
        if is_newer(r["event_4h_time"], sym_state.get("last_4h_event")):
            if sym_state.get("last_4h_event") is not None:
                triggers.append(("new_4h_event", r["event_4h_time"]))
            sym_state["last_4h_event"] = r["event_4h_time"]
        if is_newer(r["event_1d_time"], sym_state.get("last_1d_event")):
            if sym_state.get("last_1d_event") is not None:
                triggers.append(("new_1d_event", r["event_1d_time"]))
            sym_state["last_1d_event"] = r["event_1d_time"]

        # The part that actually answers "is there a trade?" — checked every
        # run, alerted only when every gate passes.
        setup = evaluate_setup(r, now)
        gate_str = ",".join(k for k, v in setup["gates"].items() if v) or "none"

        if setup["ready"]:
            key = f"{setup['direction']}:{setup['zone']['bottom']}:{setup['zone']['top']}"
            last_key = sym_state.get("last_setup_key")
            last_at = sym_state.get("last_setup_at")
            stale = True
            if last_at:
                try:
                    stale = (now - datetime.fromisoformat(last_at)) > timedelta(hours=6)
                except Exception:
                    stale = True
            # Re-sending the same setup every hour would train him to ignore
            # the alerts, which defeats the point.
            if key != last_key or stale:
                telegram_msgs.append(format_setup_alert(symbol, r, setup, now))
                sym_state["last_setup_key"] = key
                sym_state["last_setup_at"] = now.isoformat()
            alert_lines.append(f"[SETUP READY] {symbol} {setup['direction']}")
        elif setup["gates"].get("aligned") and setup["gates"].get("in_zone"):
            # Worth a look but not a trade: price reached the zone with the
            # higher timeframes agreeing, still waiting on the 1H trigger
            # and/or the session window.
            missing = [k for k, v in setup["gates"].items() if not v]
            if sym_state.get("last_headsup_key") != str(setup["zone"]):
                telegram_msgs.append(
                    f"HEADS UP — {symbol} {setup['direction']} zone reached\n"
                    f"Price ${price} inside {setup['zone']['bottom']}-"
                    f"{setup['zone']['top']}, 1D/4H aligned.\n"
                    f"Still waiting on: {', '.join(missing)}.\n"
                    f"Not a trade yet — no entry until those pass.\n"
                    f"{footer(now)}"
                )
                sym_state["last_headsup_key"] = str(setup["zone"])
            alert_lines.append(f"[HEADS UP] {symbol} in {setup['direction']} zone")
        else:
            sym_state.pop("last_headsup_key", None)

        demand_str = (f"{r['demand']['bottom']}-{r['demand']['top']}"
                      if r["demand"] else "none nearby")
        lines.append(
            f"{symbol}: ${price} | 1D {r['trend_1d']} / 4H {r['trend_4h']}"
            f" / 1H {r['trend_1h']} | "
            f"funding {r['funding_pct']}% | support {demand_str} | "
            f"bull>{r['bull_confirm']} | bear<{r['bear_confirm']}"
        )
        lines.append(f"  setup gates passed: {gate_str}")
        summary.append(
            f"{symbol} ${price} — 1D {r['trend_1d']}/4H {r['trend_4h']}"
            f"/1H {r['trend_1h']}, zone {demand_str}"
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

    # One heartbeat a day. Without it, a dead monitor and a quiet market look
    # identical from the phone — which is exactly how 19 green-but-empty CI
    # runs went unnoticed.
    if now.hour == HEARTBEAT_HOUR_UTC and state.get("last_heartbeat") != now.date().isoformat():
        telegram_msgs.append(
            "Daily check-in — monitor is alive.\n\n"
            + "\n".join(summary)
            + "\n\nNo setup alert today means no setup, not a failure.\n"
            + footer(now)
        )
        state["last_heartbeat"] = now.date().isoformat()

    lines.append(footer(now))
    log("\n".join(lines))
    save_state(state)

    if alert_lines:
        alert_text = "\n".join(alert_lines) + "\n" + footer(now)
        ALERT_PATH.write_text(alert_text, encoding="utf-8")
        show_toast("BTC/ETH monitor", alert_lines[0])
        log("--- ALERT ---\n" + alert_text)

    # Structure events and level crossings stay in the log. Only setups,
    # heads-ups and the heartbeat are worth a phone notification — an alert
    # that fires on every wiggle is an alert that gets muted.
    for msg in telegram_msgs:
        send_telegram(msg)
        log("--- TELEGRAM ---\n" + msg)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        log(f"FATAL: {e}")
        raise
