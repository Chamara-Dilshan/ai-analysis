"""
Two-way Telegram bot for the monitor. Runs as a long-lived service next to
the hourly cron (see trading-bot.service) and answers the one chat id in
monitor.env; every other sender is ignored.

    /status                     monitor health, open trades, records, drawdown
    /took SYM [ENTRY]           log the alerted setup on SYM as a real trade
    /took SYM long|short ENTRY STOP [reversal|continuation]   log any trade
    /closed SYM PRICE           log your exit; use the average if you scaled out
    /pause [HOURS]              hold back setup alerts (default 24h)
    /resume                     turn setup alerts back on
    /help

Why it exists: the paper journal records every setup the system fires, but
nothing recorded the trades actually taken - so nobody could see whether
real fills, entries and exits match the alerts. real_trades.json is that
record. /took also starts the hourly breakeven / trail / stop management for
the position, exactly as `standalone_monitor.py add` does.
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx

import standalone_monitor as mon

REAL_PATH = mon.HERE / "real_trades.json"
BOT_STATE_PATH = mon.HERE / "bot_state.json"
STALE_RUN = timedelta(hours=2)      # cron runs hourly; two missed is a problem

HELP = (
    "Commands:\n"
    "/status - monitor health, open trades, records\n"
    "/took ETH - log the alerted ETH setup as a real trade\n"
    "/took ETH 2531.5 - same, at your actual fill price\n"
    "/took ETH short 2531.5 2576.4 - log a trade with no alert\n"
    "/closed ETH 2463 - log your exit (average price if you scaled out)\n"
    "/pause 12 - hold back setup alerts for 12h (default 24h)\n"
    "/resume - setup alerts back on\n\n"
    "Stop, breakeven, trail and drawdown messages are never paused."
)

COMMANDS = [("status", "Monitor health, open trades, records"),
            ("took", "Log a trade you took: /took ETH [fill price]"),
            ("closed", "Log your exit: /closed ETH 2463"),
            ("pause", "Hold back setup alerts: /pause [hours]"),
            ("resume", "Turn setup alerts back on"),
            ("help", "List commands")]


# --------------------------------------------------------------------------- #
# the real-trade journal
# --------------------------------------------------------------------------- #
def load_real() -> list:
    if REAL_PATH.exists():
        try:
            return json.loads(REAL_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def save_real(trades: list) -> None:
    REAL_PATH.write_text(json.dumps(trades, indent=2), encoding="utf-8")


def real_summary(trades: list) -> str:
    done = [t for t in trades if t.get("status") in ("win", "loss")]
    open_n = sum(1 for t in trades if t.get("status") == "open")
    if not done:
        return f"Real record: no closed trades yet ({open_n} open)."
    wins = sum(1 for t in done if t["status"] == "win")
    total_r = sum(t["r"] for t in done)
    total_usd = sum(t["usd"] for t in done)
    money = f"{'+' if total_usd >= 0 else '-'}${abs(total_usd):.2f}"
    return (f"Real record: {len(done)} closed, {wins} wins "
            f"({100 * wins / len(done):.0f}%), {total_r:+.2f}R ({money} before "
            f"fees). {open_n} open.")


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def symbol_of(raw: str) -> str:
    sym = raw.upper().strip()
    return sym if sym.endswith("USDT") else sym + "USDT"


def price_of(raw: str) -> float:
    value = float(raw.replace(",", "").replace("$", ""))
    if value <= 0:
        raise ValueError
    return value


def _levels(direction: str, entry: float, stop: float) -> str:
    dist = abs(entry - stop)
    sign = 1 if direction == "long" else -1
    return (f"Breakeven alert at {round(entry + sign * dist, 4)} (1R), "
            f"1:2 at {round(entry + sign * 2 * dist, 4)}.")


# --------------------------------------------------------------------------- #
# commands - each runs under mon.state_lock(), so the hourly run never
# overwrites what a command just wrote, or the reverse
# --------------------------------------------------------------------------- #
def cmd_status(now: datetime) -> str:
    state = mon.load_state()
    out = []

    last = state.get("last_run")
    if last:
        age = now - datetime.fromisoformat(last)
        mins = int(age.total_seconds() // 60)
        health = "healthy"
        if state.get("consecutive_failures"):
            health = f"WARNING: {state['consecutive_failures']} degraded run(s) in a row"
        if age > STALE_RUN:
            health = f"WARNING: no completed run for {mins // 60}h - check the VM"
        out.append(f"Monitor: last run {mins} min ago - {health}.")
    else:
        out.append("Monitor: no completed run recorded yet.")

    until = mon.paused_until(state, now)
    out.append(f"Setup alerts: PAUSED until {until.strftime('%m-%d %H:%M')} UTC."
               if until else "Setup alerts: on.")

    real = load_real()
    open_real = [t for t in real if t.get("status") == "open"]
    tracked = {p["symbol"]: p for p in mon.load_positions() if not p.get("closed")}
    if open_real:
        out.append("\nYour open trades:")
        for t in open_real:
            p = tracked.get(t["symbol"])
            if p is None:
                note = "monitor saw it exit - log it with /closed"
            else:
                flags = [n for k, n in (("breakeven_done", "at breakeven"),
                                        ("partial_done", "half taken"))
                         if p.get(k)]
                stop = p.get("trail_stop") or (p["entry"] if p.get("breakeven_done") else p["stop"])
                note = f"stop {stop}" + (f", {', '.join(flags)}" if flags else "")
            out.append(f"- {t['symbol']} {t['direction'].upper()} @ {t['entry']} ({note})")
    else:
        out.append("\nYour open trades: none.")

    paper = mon.load_paper()
    _, dd = mon.paper_drawdown(paper)
    out.append("\n" + mon.paper_summary(paper))
    out.append(f"Drawdown: ${-dd:.2f} below the high (review at "
               f"${mon.DRAWDOWN_REVIEW_USD:.0f}, stop at ${mon.DRAWDOWN_STOP_USD:.0f}).")
    out.append(real_summary(real))
    return "\n".join(out)


def _latest_open_paper(sym: str):
    opens = [t for t in mon.load_paper()
             if t.get("symbol") == sym and t.get("status") == "open"]
    return max(opens, key=lambda t: t.get("opened", ""), default=None)


def cmd_took(args: list, now: datetime) -> str:
    if not args:
        return "Which symbol? e.g. /took ETH  or  /took ETH 2531.5"
    sym = symbol_of(args[0])

    if len(args) in (4, 5):
        direction = args[1].lower()
        if direction not in ("long", "short"):
            return "Direction must be long or short."
        entry, stop = price_of(args[2]), price_of(args[3])
        mode = args[4].lower() if len(args) == 5 else "reversal"
        if mode not in ("reversal", "continuation"):
            return "Mode must be reversal or continuation."
        alert_entry, risk_pct, source = None, mon.RISK_PCT, "manual"
    elif len(args) in (1, 2):
        alert = _latest_open_paper(sym)
        if alert is None:
            return (f"No open alert on {sym} to match. Log it by hand:\n"
                    f"/took {sym.replace('USDT', '')} long|short ENTRY STOP")
        direction, stop = alert["direction"], alert["stop"]
        mode = alert.get("mode", "reversal")
        alert_entry = alert["entry"]
        entry = price_of(args[1]) if len(args) == 2 else alert_entry
        risk_pct, source = alert.get("risk_pct_used", mon.RISK_PCT), "alert"
    else:
        return HELP

    if (direction == "long" and stop >= entry) or (direction == "short" and stop <= entry):
        return (f"Entry {entry} is on the wrong side of the stop {stop} for a "
                f"{direction} - price has already passed the stop. Not logged.")

    real = load_real()
    if any(t["symbol"] == sym and t.get("status") == "open" for t in real):
        return f"You already have an open {sym} trade logged. Close it first: /closed {sym.replace('USDT', '')} PRICE"
    positions = mon.load_positions()
    if any(p["symbol"] == sym and not p.get("closed") for p in positions):
        return f"{sym} is already being tracked. Close it first: /closed {sym.replace('USDT', '')} PRICE"

    opened = now.isoformat()
    positions.append({"symbol": sym, "direction": direction, "entry": entry,
                      "stop": stop, "mode": mode, "opened": opened})
    real.append({"symbol": sym, "direction": direction, "mode": mode,
                 "entry": entry, "alert_entry": alert_entry, "stop": stop,
                 "risk_pct": risk_pct, "source": source, "opened": opened,
                 "status": "open"})
    mon.save_positions(positions)
    save_real(real)

    slip = ""
    if alert_entry is not None and entry != alert_entry:
        worse = (entry > alert_entry) if direction == "long" else (entry < alert_entry)
        slip = (f"\nFill vs alert: {entry} vs {alert_entry} "
                f"({'worse' if worse else 'better'} by {abs(entry - alert_entry):.4g}).")
    risk_usd = mon.CAPITAL * risk_pct / 100
    return (f"Logged: {sym} {direction.upper()} @ {entry}, stop {stop} "
            f"({mode}, risk ${risk_usd:.2f} at {risk_pct:g}%).{slip}\n"
            f"{_levels(direction, entry, stop)}\n"
            f"I'll message breakeven, trail and stop moves every hour.\n"
            f"When you exit: /closed {sym.replace('USDT', '')} PRICE")


def cmd_closed(args: list, now: datetime) -> str:
    if len(args) != 2:
        return "Usage: /closed ETH 2463  (your exit price; the average if you scaled out)"
    sym, exit_price = symbol_of(args[0]), price_of(args[1])
    real = load_real()
    opens = [t for t in real if t["symbol"] == sym and t.get("status") == "open"]
    if not opens:
        others = ", ".join(t["symbol"] for t in real if t.get("status") == "open") or "none"
        return f"No open {sym} trade logged. Open ones: {others}."
    t = max(opens, key=lambda x: x["opened"])

    long = t["direction"] == "long"
    risk_dist = abs(t["entry"] - t["stop"])
    r = ((exit_price - t["entry"]) if long else (t["entry"] - exit_price)) / risk_dist
    usd = r * mon.CAPITAL * t["risk_pct"] / 100
    t.update(status="win" if r > 0 else "loss", exit=exit_price,
             r=round(r, 3), usd=round(usd, 2), closed=now.isoformat())

    positions = mon.load_positions()
    mon.save_positions([p for p in positions if p["symbol"] != sym])
    save_real(real)

    money = f"{'+' if usd >= 0 else '-'}${abs(usd):.2f}"
    return (f"Closed: {sym} {t['direction'].upper()} {t['entry']} -> {exit_price} = "
            f"{r:+.2f}R ({money} before fees).\n{real_summary(real)}")


def cmd_pause(args: list, now: datetime) -> str:
    try:
        hours = float(args[0]) if args else 24.0
    except ValueError:
        return "Usage: /pause 12  (hours; default 24)"
    if not 0 < hours <= 24 * 14:
        return "Pause for between a few minutes and 14 days (336 hours)."
    state = mon.load_state()
    until = now + timedelta(hours=hours)
    state["paused_until"] = until.isoformat()
    mon.save_state(state)
    return (f"Setup alerts paused until {until.strftime('%Y-%m-%d %H:%M')} UTC. "
            f"Setups still go in the paper journal. Stop, breakeven, trail and "
            f"drawdown messages still come through. /resume to undo.")


def cmd_resume(now: datetime) -> str:
    state = mon.load_state()
    was = mon.paused_until(state, now)
    state.pop("paused_until", None)
    mon.save_state(state)
    return "Setup alerts back on." if was else "Setup alerts were already on."


def handle(text: str, now: datetime = None) -> str:
    """Answer one message. Never raises: a bad command gets a reply, not a
    crashed service."""
    now = now or datetime.now(timezone.utc)
    parts = (text or "").strip().split()
    if not parts or not parts[0].startswith("/"):
        return HELP
    cmd, args = parts[0][1:].split("@")[0].lower(), parts[1:]
    try:
        with mon.state_lock():
            if cmd == "status":
                return cmd_status(now)
            if cmd == "took":
                return cmd_took(args, now)
            if cmd == "closed":
                return cmd_closed(args, now)
            if cmd == "pause":
                return cmd_pause(args, now)
            if cmd == "resume":
                return cmd_resume(now)
    except ValueError:
        return "Couldn't read a price in that. Example: /closed ETH 2463.5"
    except TimeoutError:
        return "The hourly check is busy - try again in a minute."
    except Exception as e:
        mon.log(f"(bot: {cmd} failed: {e!r})")
        return f"Something went wrong running /{cmd}; nothing was changed by the failed part. Check the VM log."
    return HELP


# --------------------------------------------------------------------------- #
# the service loop
# --------------------------------------------------------------------------- #
def _api(token: str, method: str, **kwargs):
    return httpx.post(f"https://api.telegram.org/bot{token}/{method}", **kwargs)


def _load_offset() -> int:
    try:
        return json.loads(BOT_STATE_PATH.read_text())["offset"]
    except Exception:
        return 0


def _save_offset(offset: int) -> None:
    BOT_STATE_PATH.write_text(json.dumps({"offset": offset}))


def run() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set", file=sys.stderr)
        return 1
    try:
        _api(token, "setMyCommands", timeout=15,
             json={"commands": [{"command": c, "description": d} for c, d in COMMANDS]})
    except Exception:
        pass        # the menu is a nicety; commands work without it

    offset = _load_offset()
    print(f"bot up, offset {offset}", flush=True)
    while True:
        try:
            resp = _api(token, "getUpdates", timeout=65,
                        json={"offset": offset, "timeout": 50,
                              "allowed_updates": ["message"]})
            updates = resp.json().get("result", []) if resp.status_code == 200 else []
            if resp.status_code != 200:
                print(f"getUpdates HTTP {resp.status_code}", flush=True)
                time.sleep(10)
        except Exception as e:
            # Never print the exception's URL - it carries the token.
            print(f"getUpdates failed: {type(e).__name__}", flush=True)
            time.sleep(10)
            continue
        for upd in updates:
            # Saved BEFORE handling: a crash mid-command must not replay it
            # on restart and log the same trade twice. At most once.
            offset = upd["update_id"] + 1
            _save_offset(offset)
            msg = upd.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) != str(chat_id):
                print(f"ignored message from chat {(msg.get('chat') or {}).get('id')}", flush=True)
                continue
            text = msg.get("text", "")
            reply = handle(text)
            print(f"{text.split()[0] if text.split() else '(empty)'} -> {reply.splitlines()[0]}", flush=True)
            mon.send_telegram(reply)


if __name__ == "__main__":
    raise SystemExit(run())
