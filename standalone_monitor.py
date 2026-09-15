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

Two independent setup gates run every hour, either of which can fire on its
own (added 2026-08-29, see evaluate_setup / evaluate_trend_continuation_setup):
  - reversal:     wait for a pullback into a demand/supply zone, tight stop.
  - continuation: enter on a fresh trend-aligned 4H BOS immediately, no
                   pullback wait, wider stop at the breakout candle's own
                   low/high. Added after backtesting showed the reversal-only
                   gate missed almost the entire 2026-08-19->08-28 rally.

The two gates are REGIME-COMPLEMENTARY, which is the real reason to run both.
Out-of-sample, corrected harness (the pre-2026-09-15 figures were inflated by
a look-ahead bug — see the fix in backtest.py):
  - continuation, 5 symbols: +115.4R in trending 2021-23, +12.8R in choppy
    2023-25. It earns in sustained trends and treads water in whipsaw.
  - reversal, BTC/ETH/BNB: -10.6R in that same trending window, +27.6R in the
    choppy one. Exactly the mirror.
Neither gate is "better". In a trend, breakouts continue and pullback entries
get run over; in chop, breakouts fail and price reverts to its zones. Running
both means one of them is usually in its element. Recent real-world numbers
are thin for both (2025-02->2026-09: reversal +3.0R), so treat any claim of a
strong edge with suspicion. Full tables in crypto-analysis-output-format memory.

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
import news

HERE = Path(__file__).parent
STATE_PATH = HERE / "monitor_state.json"
POSITIONS_PATH = HERE / "positions.json"
PAPER_PATH = HERE / "paper_trades.json"
LOG_PATH = HERE / "monitor_log.txt"
ALERT_PATH = HERE / "latest_alert.txt"

# Per-gate symbol lists, from out-of-sample backtests (backtest.py). Numbers
# below are from the corrected harness; the original split was decided on
# look-ahead-inflated figures, and re-checking on corrected data across three
# windows did not change either list.
#   continuation — positive on all five in the trending 2021-23 window; ETH,
#     SOL and XRP go negative in the choppy 2023-25 one. Regime, not symbol:
#     the scope stays at five.
#   reversal — negative on SOL and XRP in all THREE windows tested
#     (2021-23, 2023-25, 2025-02->2026-09), so it does not run there.
# A gate with no edge on a symbol is not diversification, it is noise.
REVERSAL_SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT"]
CONTINUATION_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]
SYMBOLS = list(dict.fromkeys(REVERSAL_SYMBOLS + CONTINUATION_SYMBOLS))

# Crypto moves together. The day BTC breaks, the alt continuation gates fire
# the same direction — five positions at 2% each is 10% of the account at
# risk at once, double the plan's daily loss limit. Cap concurrent open
# setups; anything beyond it is logged, not alerted, so the paper record
# still shows what the cap cost or saved.
MAX_OPEN_POSITIONS = 2
SL_OFFSET = timedelta(hours=5, minutes=30)

# The plan's trading windows, UTC. A setup outside these is prep, not a trade.
KILL_ZONES = (("London", 7, 9), ("New York", 13, 15))

# How recent a 1H structure event must be to count as "the trigger" rather
# than old history. Backtesting showed 3h was far too tight — structure
# events are rarer than that, so the gate passed 0 times in 4,320 hours.
# Half a day keeps the signal current while letting price take its time
# reaching the zone after the turn.
TRIGGER_MAX_AGE = timedelta(hours=12)

# Capital plan — must match risk.py's defaults and the MCP server.
CAPITAL, RISK_PCT, LEVERAGE = 200, 2, 5

# Minimum zone width, as a percent of price. The raw structure engine emits
# one-tick gaps — a $10 band on a $79k asset — and "nearest zone below price"
# picks those far more often than real ones. Price can never be meaningfully
# INSIDE a band that thin at the moment an hourly check runs, so without this
# filter the setup gate is unreachable in practice.
MIN_ZONE_PCT = 0.35

# How close price must get to a zone to count as having reached it.
#
# This cannot be a containment test. Zones in the "open" list are open
# precisely BECAUSE price has not traded back into them — the moment it does,
# the engine marks them mitigated and they leave the list. So "price inside an
# open zone" is self-contradictory and was true 0 times in 4,320 backtested
# hours. Proximity is what the trade actually needs anyway: price arriving at
# the zone edge, not sitting in its middle.
ZONE_TOUCH_PCT = 0.25

# Round-trip fees as a percent of the $4 risk. A very tight stop can make the
# trade cost a fifth of what it risks, which quietly demands a much higher
# win rate than the stated R:R suggests.
MAX_FEE_PCT_OF_RISK = 10.0

# Optional dead-man's switch. Point this at a healthchecks.io (or similar)
# ping URL and the service alerts YOU when the monitor stops checking in —
# the one failure a monitor can never report about itself. Inert if unset.
HEALTHCHECK_URL = os.environ.get("HEALTHCHECK_URL", "").strip()

# Consecutive failed runs before the monitor complains about itself. Covers
# the cases it CAN report: API outages, code faults, bad data.
MAX_CONSECUTIVE_FAILURES = 3

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


def wide_enough(top: float, bottom: float, price: float) -> bool:
    """Reject one-tick gaps. See MIN_ZONE_PCT."""
    if not price:
        return False
    return ((top - bottom) / price) * 100 >= MIN_ZONE_PCT


def pick_demand_zone(struct_4h: dict, price: float):
    # Zones AT or below price: bottom <= price. The old test was top < price,
    # which selected only zones entirely beneath the current price — so the
    # "is price inside the zone" gate could never be true, by construction.
    zones = []
    for ob in struct_4h["order_blocks"]["open"]:
        if ob["type"] == "bullish" and ob["bottom"] <= price:
            zones.append((ob["top"], ob["bottom"], "ob"))
    for fvg in struct_4h["fair_value_gaps"]["open"]:
        if fvg["type"] == "bullish" and fvg["bottom"] <= price:
            zones.append((fvg["top"], fvg["bottom"], "fvg"))
    zones = [z for z in zones if wide_enough(z[0], z[1], price)]
    if not zones:
        return None
    top, bottom, kind = max(zones, key=lambda z: z[0])
    return {"top": top, "bottom": bottom, "kind": kind}


def pick_supply_zone(struct_4h: dict, price: float):
    """Mirror of pick_demand_zone — nearest bearish OB/FVG above price, for
    when the higher timeframes are aligned short."""
    # Mirror of pick_demand_zone: zones AT or above price, so price can be
    # inside one rather than always strictly beneath it.
    zones = []
    for ob in struct_4h["order_blocks"]["open"]:
        if ob["type"] == "bearish" and ob["top"] >= price:
            zones.append((ob["top"], ob["bottom"], "ob"))
    for fvg in struct_4h["fair_value_gaps"]["open"]:
        if fvg["type"] == "bearish" and fvg["top"] >= price:
            zones.append((fvg["top"], fvg["bottom"], "fvg"))
    zones = [z for z in zones if wide_enough(z[0], z[1], price)]
    if not zones:
        return None
    top, bottom, kind = min(zones, key=lambda z: z[1])
    return {"top": top, "bottom": bottom, "kind": kind}


def reached_zone(zone, price: float, low: float = None, high: float = None) -> bool:
    """Has price arrived at this zone within the last hour?

    Tests the hour's RANGE against the zone, not just the close. Price often
    tags a zone and leaves inside the same hour; a close-only test misses
    those entirely, which understates how often the setup is really available.
    See ZONE_TOUCH_PCT for why this is proximity rather than containment.
    """
    if not zone or not price:
        return False
    tol = price * ZONE_TOUCH_PCT / 100
    lo = low if low is not None else price
    hi = high if high is not None else price
    # Range overlap between the hour's travel and the zone (plus tolerance).
    return lo <= (zone["top"] + tol) and hi >= (zone["bottom"] - tol)


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


def load_paper() -> list:
    if PAPER_PATH.exists():
        try:
            return json.loads(PAPER_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def save_paper(trades: list) -> None:
    PAPER_PATH.write_text(json.dumps(trades, indent=2), encoding="utf-8")


def record_paper_trade(symbol: str, setup: dict, now_utc: datetime,
                        mode: str = "reversal") -> dict:
    """Log every SETUP READY as a paper trade, whether or not it gets taken.

    Backtests are arguments; this is evidence. It accumulates an untuned,
    forward-looking record at zero risk, which is the only honest way to find
    out whether the gates are worth acting on.

    `mode` tags which gate produced this trade ("reversal" or "continuation",
    added 2026-08-29) so the two can be told apart later in paper_trades.json
    without guessing from entry/stop shape.
    """
    entry, stop = setup["entry"], setup["stop"]
    risk_dist = abs(entry - stop)
    long = setup["direction"] == "long"
    return {
        "symbol": symbol,
        "mode": mode,
        "direction": setup["direction"],
        "entry": entry,
        "stop": stop,
        "target": round(entry + 2 * risk_dist, 2) if long else round(entry - 2 * risk_dist, 2),
        "fee_pct": setup.get("fee_pct"),
        "risk_pct_used": setup.get("risk_pct_used", RISK_PCT),
        "opened": now_utc.isoformat(),
        "status": "open",
        # Trailing state, used only when mode == "continuation". Out-of-sample
        # the two gates want opposite exits: trailing reversal trades gave
        # back -3.7R, trailing continuation trades added +38.9R (+52%). The
        # stop starts where the setup put it and only moves in the trade's
        # favour.
        "current_stop": stop,
        "partial_r": 0.0,
        "breakeven_done": False,
        "partial_done": False,
    }


def update_paper_trade(t: dict, r: dict, now_utc: datetime) -> bool:
    """Resolve an open paper trade against the hour's range. Returns True if
    it closed on this pass.

    The exit depends on which gate opened the trade — the split the
    out-of-sample backtest produced (2023-01 -> 2025-02):
      reversal:      fixed 1:2. Trailing these gave back -3.7R; the bounce
                     IS the trade.
      continuation:  breakeven at 1R, half off at 1:2, trail the rest behind
                     the latest confirmed 4H swing. Trailing added +38.9R.
    The stop is checked first, so a bar covering both counts as a loss — the
    pessimistic reading, identical to the backtest's convention.
    """
    if t["status"] != "open":
        return False
    lo = r.get("bar_low") or r["price"]
    hi = r.get("bar_high") or r["price"]
    long = t["direction"] == "long"
    fee = (t.get("fee_pct") or 0) / 100

    if t.get("mode", "reversal") != "continuation":
        hit_stop = lo <= t["stop"] if long else hi >= t["stop"]
        hit_target = hi >= t["target"] if long else lo <= t["target"]
        if hit_stop:
            t.update(status="loss", r=-1.0 - fee, closed=now_utc.isoformat())
            return True
        if hit_target:
            t.update(status="win", r=2.0 - fee, closed=now_utc.isoformat())
            return True
        return False

    # --- continuation: trail it ---
    entry = t["entry"]
    risk_dist = abs(entry - t["stop"])
    if risk_dist <= 0:
        return False
    cur = t.get("current_stop", t["stop"])
    partial_r = t.get("partial_r", 0.0)
    partial_done = t.get("partial_done", False)

    def r_at(price):
        return (price - entry) / risk_dist if long else (entry - price) / risk_dist

    hit_stop = lo <= cur if long else hi >= cur
    if hit_stop:
        total = partial_r + (0.5 if partial_done else 1.0) * r_at(cur) - fee
        t.update(status="win" if total > 0 else "loss", r=total,
                 closed=now_utc.isoformat(), current_stop=cur)
        return True

    hit_2r = hi >= t["target"] if long else lo <= t["target"]
    if hit_2r and not partial_done:
        t.update(partial_done=True, partial_r=1.0, breakeven_done=True)
        cur = entry
    elif not t.get("breakeven_done"):
        one_r = entry + risk_dist if long else entry - risk_dist
        if (hi >= one_r) if long else (lo <= one_r):
            t["breakeven_done"] = True
            cur = entry

    # Trail behind the latest confirmed 4H swing. Never backwards.
    swing = r.get("recent_low_4h") if long else r.get("recent_high_4h")
    if swing is not None and ((long and swing > cur) or (not long and swing < cur)):
        cur = swing
    t["current_stop"] = cur
    return False


def read_for_trade(r: dict, trade: dict, since_utc):
    """Copy of the symbol read whose bar range starts no earlier than the
    trade itself was opened. Bars that traded before the entry cannot stop
    a trade that did not exist yet."""
    opened = trade.get("opened")
    start = since_utc
    if opened:
        try:
            o = datetime.fromisoformat(opened)
            if o.tzinfo is None:
                o = o.replace(tzinfo=timezone.utc)
            start = o if start is None else max(start, o)
        except Exception:
            pass
    if start is None or start == since_utc or not r.get("candles_1h"):
        return r
    lo, hi = range_since(r["candles_1h"], start)
    return dict(r, bar_low=lo, bar_high=hi)


def paper_summary(trades: list) -> str:
    """Plain-language scoreboard of the forward record so far."""
    done = [t for t in trades if t["status"] in ("win", "loss")]
    if not done:
        open_n = sum(1 for t in trades if t["status"] == "open")
        return f"Paper record: no trades resolved yet ({open_n} open)."
    wins = [t for t in done if t["status"] == "win"]
    total_r = sum(t.get("r", 0) for t in done)
    return (
        f"Paper record: {len(done)} resolved, {len(wins)} wins "
        f"({100 * len(wins) / len(done):.0f}%), {total_r:+.1f}R "
        f"(${total_r * CAPITAL * RISK_PCT / 100:+.2f} at your sizing). "
        f"{sum(1 for t in trades if t['status'] == 'open')} still open."
    )


def open_slots(paper: list, positions: list) -> int:
    """Distinct symbols currently occupying a slot: an open paper trade (every
    fired setup) or a tracked real position. Distinct so a real trade that
    is also on the paper record is not counted twice."""
    syms = {t["symbol"] for t in paper if t.get("status") == "open"}
    syms |= {p["symbol"] for p in positions if not p.get("closed")}
    return len(syms)


def load_positions() -> list:
    if POSITIONS_PATH.exists():
        try:
            return json.loads(POSITIONS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def save_positions(positions: list) -> None:
    POSITIONS_PATH.write_text(json.dumps(positions, indent=2), encoding="utf-8")


def manage_position(pos: dict, r: dict, now_utc: datetime):
    """Watch an open trade against the plan's management rules.

    The plan says: move to breakeven at 1R, take half at 1:2, and exit on an
    opposing signal rather than waiting for the stop. Those are easy to state
    and easy to forget at 3am, which is the whole reason to automate them.

    Mutates pos to record what has already been announced, and returns a list
    of messages worth sending.
    """
    msgs = []
    price = r["price"]
    # The bar range since the last run. Wicks hit stops; closes do not
    # un-hit them. Falls back to the close when no range is supplied.
    lo = r.get("bar_low") or price
    hi = r.get("bar_high") or price
    entry, stop = pos["entry"], pos["stop"]
    long = pos["direction"] == "long"
    risk_dist = abs(entry - stop)
    if risk_dist <= 0:
        return msgs

    profit = (price - entry) if long else (entry - price)
    r_mult = profit / risk_dist                      # where it is now
    best = (hi - entry) if long else (entry - lo)
    best_r = best / risk_dist                        # the best it got to
    adverse = lo if long else hi                     # the worst it got to

    # Stop breached. Reported, not second-guessed — the exchange has almost
    # certainly closed this already.
    # Effective stop: the original until 1R, breakeven after it, and for a
    # continuation trade the trailed 4H swing once one has been set.
    # Breakeven always wins over a trailed level that sits below it.
    base = entry if pos.get("breakeven_done") else stop
    ts = pos.get("trail_stop")
    eff_stop = base if ts is None else (max(ts, base) if long else min(ts, base))
    # Checked FIRST against the adverse extreme: a bar that covers both the
    # stop and a milestone counts as stopped, the backtest's convention.
    hit_stop = adverse <= eff_stop if long else adverse >= eff_stop
    if hit_stop and not pos.get("closed"):
        pos["closed"] = True
        r_at_stop = ((eff_stop - entry) if long else (entry - eff_stop)) / risk_dist
        if r_at_stop <= -0.999:
            outcome = (f"Planned loss was ${round(CAPITAL * RISK_PCT / 100, 2)} "
                       f"({RISK_PCT}% of capital). That is the system working, "
                       f"not a failure.")
        else:
            outcome = (f"That stop sat at {r_at_stop:+.2f}R, so this closes flat "
                       f"or in profit — the management rules did their job.")
        msgs.append(
            f"STOP HIT — {pos['symbol']} {pos['direction'].upper()}\n"
            f"The bar traded to {adverse}, through your stop {eff_stop} "
            f"(now ${price}).\n"
            f"{outcome}\nRemoved from tracking.\n{footer(now_utc)}"
        )
        return msgs

    # 1R — the stop should come to breakeven so the trade cannot turn into a
    # loser from here.
    if best_r >= 1 and not pos.get("breakeven_done"):
        pos["breakeven_done"] = True
        msgs.append(
            f"MOVE STOP TO BREAKEVEN — {pos['symbol']} {pos['direction'].upper()}\n"
            f"Price reached 1R from entry {entry} (now ${price}, {round(r_mult, 2)}R).\n"
            f"Move your stop to {entry}. From here the trade cannot lose.\n"
            f"{footer(now_utc)}"
        )

    # 1:2 — the plan's first target. What happens here depends on the gate:
    # a reversal trade is DONE (trailing them lost money out-of-sample); a
    # continuation trade banks half and lets the rest run (trailing them
    # added +52%).
    mode = pos.get("mode", "reversal")
    if best_r >= 2 and not pos.get("partial_done"):
        pos["partial_done"] = True
        target = round(entry + 2 * risk_dist, 2) if long else round(entry - 2 * risk_dist, 2)
        if mode == "reversal":
            pos["closed"] = True
            msgs.append(
                f"1:2 TARGET REACHED — {pos['symbol']} {pos['direction'].upper()}\n"
                f"Price hit {target} this bar (now ${price}, {round(r_mult, 2)}R).\n"
                f"Reversal trade: take the FULL position off here. The bounce "
                f"is the trade — trailing reversal setups gave back profit in "
                f"testing.\nRemoved from tracking.\n{footer(now_utc)}"
            )
            return msgs
        msgs.append(
            f"1:2 TARGET REACHED — {pos['symbol']} {pos['direction'].upper()}\n"
            f"Price hit {target} this bar (now ${price}, {round(r_mult, 2)}R).\n"
            f"Continuation trade: take half off, stop to breakeven, and trail "
            f"the rest behind each new 4H swing. This is the trade that pays "
            f"for the others — let it run.\n{footer(now_utc)}"
        )

    # Continuation only: past breakeven, trail behind the latest confirmed 4H
    # swing and say so each time it moves. A swing that would loosen the stop
    # is ignored — the stop only ever tightens.
    # Trails from ENTRY, not only after breakeven - that is what the
    # backtest (+38.9R) and the paper journal both do. Waiting for 1R
    # left a live position on its original stop while a confirmed 4H
    # swing had already formed above it.
    if mode == "continuation":
        candidate = r.get("recent_low_4h") if long else r.get("recent_high_4h")
        base = entry if pos.get("breakeven_done") else stop
        current = pos.get("trail_stop") or base
        current = max(current, base) if long else min(current, base)
        if candidate is not None and ((long and candidate > current) or
                                      (not long and candidate < current)):
            pos["trail_stop"] = candidate
            msgs.append(
                f"MOVE STOP — {pos['symbol']} {pos['direction'].upper()}\n"
                f"New 4H swing {'low' if long else 'high'} confirmed at {candidate}. "
                f"Move your stop there (was {current}). Currently {round(r_mult, 2)}R.\n"
                f"{footer(now_utc)}"
            )

    # Opposing 1H signal — the plan's exit-early rule. Announced once, and
    # re-armed if structure turns back in favour, so a chopping 1H does not
    # produce a stream of identical warnings.
    ev = r.get("event_1h")
    if ev:
        against = "bearish" if long else "bullish"
        opposing = ev["direction"] == against
        try:
            fresh = (now_utc - datetime.fromisoformat(ev["time"])) <= TRIGGER_MAX_AGE
        except Exception:
            fresh = False
        if opposing and fresh and not pos.get("opposing_flagged"):
            pos["opposing_flagged"] = True
            msgs.append(
                f"OPPOSING SIGNAL — {pos['symbol']} {pos['direction'].upper()}\n"
                f"1H printed a {ev['direction']} {ev['type']} at {ev['level']}.\n"
                f"Currently {round(r_mult, 2)}R. Your plan says exit on an "
                f"opposing 1H signal rather than waiting for the stop.\n"
                f"{footer(now_utc)}"
            )
        elif not opposing:
            pos["opposing_flagged"] = False

    return msgs


async def check_news_blackout(now_utc: datetime):
    """Is a high-impact USD release inside the blackout window right now?

    Returns (blocked, description). A calendar failure must never block a
    setup — an unreachable third-party feed is not evidence of news, so this
    fails OPEN and says so.
    """
    try:
        raw = await server._get(
            server.FF_BASE_URL, "/ff_calendar_thisweek.json", cache_ttl=900,
        )
        res = news.analyze(raw, now_utc, currencies=("USD",),
                            min_impact="High", within_minutes=30)
        if res.get("blackout_active"):
            ev = res.get("next_event") or {}
            label = ev.get("title") or ev.get("event") or "high-impact release"
            return True, f"news blackout: {label}"
        return False, "news clear"
    except Exception as e:
        return False, f"news check unavailable ({type(e).__name__})"


def ping_healthcheck(ok: bool = True) -> None:
    """Check in with an external dead-man's switch, if one is configured.

    This is the only way to catch the failure the monitor cannot report
    itself: the whole machine going away. Best-effort — a failed ping must
    never affect the run."""
    if not HEALTHCHECK_URL:
        return
    url = HEALTHCHECK_URL if ok else HEALTHCHECK_URL.rstrip("/") + "/fail"
    try:
        httpx.get(url, timeout=10)
    except Exception as e:
        log(f"(healthcheck ping failed: {type(e).__name__})")


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


def evaluate_setup(r: dict, now_utc: datetime, news_blocked: bool = False):
    """Check the full entry checklist, not just whether a level moved.

    A crossed level is an event; a setup is an event that also happens to be
    in the right direction, at the right price, at the right time, clear of
    news, and cheap enough to be worth taking. Returns a dict describing
    every gate so a near-miss is still legible in the log.
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
    gates["in_zone"] = reached_zone(zone, r["price"],
                                     r.get("bar_low"), r.get("bar_high"))

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

    # Gate: no high-impact release inside the blackout window. The plan
    # forbids trading through FOMC/CPI/NFP, where a stop is decoration.
    gates["news_clear"] = not news_blocked

    if not all(gates.values()):
        return {"ready": False, "gates": gates, "direction": direction,
                "zone": zone, "kill_zone": kz}

    # Structural gates passed — build the trade. Stop goes just beyond the
    # zone that is supposed to hold; if it does not hold, the reason for the
    # trade is gone.
    if direction == "long":
        entry, stop = r["price"], round(zone["bottom"] * 0.999, 2)
    else:
        entry, stop = r["price"], round(zone["top"] * 1.001, 2)

    # Gate: fees must not eat the edge. A stop tight enough to make the
    # round trip cost a large slice of the $4 risk is a trade that needs to
    # be right far more often than the R:R implies.
    fee_pct = None
    try:
        m = risk.position_size(entry=entry, stop_loss=stop, capital=CAPITAL,
                                risk_pct=RISK_PCT, leverage=LEVERAGE)
        fee_pct = m["fees"]["pct_of_risk"]
        gates["fee_ok"] = fee_pct <= MAX_FEE_PCT_OF_RISK and m["passes_plan"]
    except Exception:
        # If the math will not compute, do not claim the setup is ready.
        gates["fee_ok"] = False

    return {"ready": all(gates.values()), "gates": gates,
            "direction": direction, "zone": zone, "kill_zone": kz,
            "entry": entry, "stop": stop, "fee_pct": fee_pct}


def _structural_stop(candles_4h: list, event_index: int, long: bool,
                     swing_strength: int, candle_stop: float) -> float:
    """Nearest CONFIRMED 4H swing low (long) / high (short) before the breakout
    candle, never tighter than the breakout candle's own extreme. Only candles
    up to the breakout are consulted and a swing needs `swing_strength`
    candles on both sides, so anything returned was fully formed before the
    break — no look-ahead. Falls back to the candle stop if no swing exists."""
    swings = structure.find_swings(candles_4h[:event_index + 1], swing_strength, swing_strength)
    kind = "low" if long else "high"
    pts = [s["price"] for s in swings if s["type"] == kind and s["index"] < event_index]
    if not pts:
        return candle_stop
    return min(candle_stop, pts[-1]) if long else max(candle_stop, pts[-1])


def _continuation_fee(entry: float, stop: float):
    """(fee % of risk, risk % used) for a continuation stop, sized to Binance's
    minimum notional when 2% would fall short. fee is None if the math fails."""
    sl_distance_pct = abs(entry - stop) / entry * 100
    min_risk_pct = round(risk.MIN_NOTIONAL_USD * sl_distance_pct / CAPITAL, 3)
    risk_pct_used = max(RISK_PCT, min_risk_pct)
    try:
        m = risk.position_size(entry=entry, stop_loss=stop, capital=CAPITAL,
                                risk_pct=risk_pct_used, leverage=LEVERAGE)
        return m["fees"]["pct_of_risk"], risk_pct_used
    except Exception:
        return None, risk_pct_used


def evaluate_trend_continuation_setup(trend_1d: str, event_4h: dict, candles_4h: list,
                                       price: float, now_utc: datetime,
                                       news_blocked: bool = False,
                                       max_fee_pct: float = None,
                                       stop_mode: str = "candle",
                                       swing_strength: int = 3):
    """Trend-continuation entry: trade a fresh 4H BOS immediately, without
    waiting for the pullback evaluate_setup() requires.

    `max_fee_pct` (default MAX_FEE_PCT_OF_RISK) and `stop_mode` exist for
    backtest.py to test alternatives without touching live behaviour:
      candle    — stop at the breakout candle's own low/high (live default)
      structure — stop at the nearest confirmed 4H swing before the break,
                  never tighter than the candle stop
      hybrid    — candle stop, widened to the structural one only when the
                  candle stop fails the fee gate
    Added 2026-09-15 after the VM log showed the week's only real setup
    (ETH 2026-09-14 20:00 BOS) pass every gate except fee_ok because the
    breakout candle was small. `swing_strength` must match whatever produced
    `candles_4h`'s structure (check_symbol uses 3 on 4H).

    Added 2026-08-29. The 2026-08-19->08-28 rally (BTC ~$65k->$81.5k, ETH
    ~$1,900->$2,566) went almost entirely uncaptured by evaluate_setup —
    every hourly check saw a confirmed bullish BOS but the demand-zone
    pullback it waits for never came before price ran further. This is the
    other half of the plan: evaluate_setup handles reversals (wait for the
    pullback, tight stop near structure); this handles continuation (enter
    on the break itself, wider stop at the breakout candle's own low/high).

    Deliberately does NOT require price in a demand/supply zone, a separate
    1H trigger, or a kill-zone window — the 4H BOS closing in the daily
    trend's direction IS the trigger, and it happens on the 4H clock
    (00/04/08/12/16/20 UTC), not London/NY hours. Requiring a kill zone here
    would reintroduce the same "confirmed but stale by the time conditions
    line up" gap this mode exists to fix. News blackout still applies —
    that's about not entering into a stop-hunt candle, unrelated to entry
    style.

    Chamara's call (2026-08-29): the 2%-risk math must not gate the entry.
    A structurally wide stop is fine — size to Binance's $100 minimum
    notional instead of shrinking the stop or skipping the trade, and report
    the resulting risk % plainly rather than silently forcing 2%.

    `candles_4h` must be the SAME candle list/slice that produced `event_4h`
    via structure.analyze(), since event_4h["index"] indexes into it.
    """
    gates = {}

    is_bos = bool(event_4h and event_4h["type"] == "BOS")
    direction = None
    if is_bos and trend_1d == "up" and event_4h["direction"] == "bullish":
        direction = "long"
    elif is_bos and trend_1d == "down" and event_4h["direction"] == "bearish":
        direction = "short"
    gates["trend_aligned_bos"] = direction is not None

    # Fresh means "this is the candle that just closed" — without this the
    # same break re-fires every hour for days until the next event.
    fresh = False
    if direction:
        try:
            fresh = (now_utc - datetime.fromisoformat(event_4h["time"])) <= timedelta(hours=4)
        except Exception:
            fresh = False
    gates["fresh"] = fresh
    gates["news_clear"] = not news_blocked

    if not all(gates.values()):
        return {"ready": False, "gates": gates, "direction": direction}

    breakout = candles_4h[event_4h["index"]]
    entry = price
    long = direction == "long"
    candle_stop = breakout["low"] if long else breakout["high"]
    stop = candle_stop
    if stop_mode == "structure":
        stop = _structural_stop(candles_4h, event_4h["index"], long, swing_strength, candle_stop)

    # Sanity: the stop must still sit on the correct side of entry. Can fail
    # if price has already round-tripped past the breakout candle's opposite
    # extreme by the time this runs.
    valid_side = (stop < entry) if long else (stop > entry)
    gates["stop_valid"] = valid_side
    if not valid_side:
        return {"ready": False, "gates": gates, "direction": direction}

    cap = MAX_FEE_PCT_OF_RISK if max_fee_pct is None else max_fee_pct
    fee_pct, risk_pct_used = _continuation_fee(entry, stop)
    gates["fee_ok"] = fee_pct is not None and fee_pct <= cap

    if not gates["fee_ok"] and stop_mode == "hybrid":
        wider = _structural_stop(candles_4h, event_4h["index"], long, swing_strength, candle_stop)
        if wider != stop:
            stop = wider
            fee_pct, risk_pct_used = _continuation_fee(entry, stop)
            gates["fee_ok"] = fee_pct is not None and fee_pct <= cap

    return {
        "ready": all(gates.values()), "gates": gates, "direction": direction,
        "entry": entry, "stop": stop, "fee_pct": fee_pct,
        "risk_pct_used": risk_pct_used,
        "risk_unusually_high": risk_pct_used > 6.0,
        "stop_source": "structure" if stop != candle_stop else "candle",
        "stop_dist_pct": abs(entry - stop) / entry * 100,
        "event": event_4h,
    }


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
        f"Manage: breakeven stop at {breakeven} (1R), then take the FULL "
        f"position off at 1:2. Trailing was re-tested on corrected data over "
        f"three windows and won only one of them (it is not a money-loser — "
        f"that older claim was wrong — just not better here), so the fixed "
        f"1:2 stands. Exit on an opposing 1H CHoCH rather "
        f"than waiting for the stop.\n"
        f"1D {r['trend_1d']} / 4H {r['trend_4h']} — aligned.\n"
        f"{warn}\n"
        f"Math only, NOT a recommendation. You decide.\n"
        f"{footer(now_utc)}"
    )


def format_continuation_alert(symbol: str, setup: dict, now_utc: datetime) -> str:
    """The continuation-mode counterpart to format_setup_alert. No zone to
    quote — the fresh trend-aligned 4H BOS itself is the trigger — and the
    risk % actually used can be above the plan's default 2% when the
    structurally-correct stop is wide enough to fall under Binance's $100
    minimum at 2% (Chamara's call, 2026-08-29: size to the minimum instead
    of shrinking the stop or skipping the trade)."""
    entry, stop = setup["entry"], setup["stop"]
    d = setup["direction"].upper()
    risk_pct_used = setup.get("risk_pct_used", RISK_PCT)
    try:
        m = risk.position_size(entry=entry, stop_loss=stop, capital=CAPITAL,
                                risk_pct=risk_pct_used, leverage=LEVERAGE)
    except Exception as e:
        return f"CONTINUATION SETUP {symbol} {d} @ {entry} / stop {stop} (math failed: {e})"

    verdict = "PASS" if m["passes_plan"] else "FAIL"
    warn = ""
    if m.get("warnings"):
        warn = "\nWarnings: " + "; ".join(str(w) for w in m["warnings"])
    risk_note = (
        f" (above the default {RISK_PCT}% — sized up to clear Binance's "
        f"${risk.MIN_NOTIONAL_USD:.0f} minimum at this stop width)"
        if risk_pct_used > RISK_PCT else ""
    )

    r_dist = abs(entry - stop)
    breakeven = round(entry + r_dist, 2) if d == "LONG" else round(entry - r_dist, 2)
    ev = setup.get("event") or {}

    return (
        f"CONTINUATION SETUP — {symbol} {d}\n"
        f"Fresh {ev.get('type', 'BOS')} at {ev.get('level')} ({ev.get('time')}), "
        f"same direction as the 1D trend — entering on the break, no pullback wait.\n\n"
        f"Entry {entry}  |  Stop {stop} (breakout candle's own low/high)  |  "
        f"Plan check: {verdict}\n"
        f"Size {m['position']['size_btc']} (${m['position']['notional_usd']}), "
        f"margin@{LEVERAGE}x ${m['position']['margin_by_leverage_usd'][f'{LEVERAGE}x']}\n"
        f"Risk ${m['risk_amount_usd']} ({risk_pct_used}%){risk_note}, "
        f"approx liq {m['approx_liquidation_price']}\n"
        f"Targets 1:2={m['plan_target_prices']['1:2']}  "
        f"1:3={m['plan_target_prices']['1:3']}\n"
        f"Fees ~{m['fees']['pct_of_risk']}% of risk\n\n"
        f"Manage: breakeven stop at {breakeven} (1R), then trail behind each "
        f"new 4H swing — the default, but NOT unconditional. On corrected "
        f"out-of-sample data trailing beats a fixed 1:2 overall (+128R vs "
        f"+49R across two windows) and that edge is almost entirely from "
        f"TRENDING markets; in chop the fixed 1:2 actually won (+21R vs "
        f"+13R). So if price has been chopping rather than trending, taking "
        f"the full position at 1:2 is a reasonable call, not a mistake. "
        f"Half off at 1:2 if you want something locked in early, rest "
        f"trailed; or trail the whole position with no fixed TP at all. "
        f"Decide now, don't wait until it's already in profit.\n"
        f"{warn}\n"
        f"Math only, NOT a recommendation. You decide.\n"
        f"{footer(now_utc)}"
    )


def range_since(candles: list, since_utc, bar_hours: int = 1):
    """(low, high) across every candle still forming at or after `since_utc`,
    plus the current forming bar. With no reference point only the forming
    bar counts. Runs are hourly at :07, so between two runs one bar
    completes; its wicks are what the close-only check kept missing."""
    if not candles:
        return None, None
    chosen = candles[-1:]
    if since_utc is not None:
        picked = []
        for c in candles:
            try:
                open_t = datetime.fromisoformat(c["open_time"])
            except Exception:
                continue
            if open_t.tzinfo is None:
                open_t = open_t.replace(tzinfo=timezone.utc)
            if open_t + timedelta(hours=bar_hours) > since_utc:
                picked.append(c)
        if picked:
            chosen = picked
    return min(c["low"] for c in chosen), max(c["high"] for c in chosen)


async def check_symbol(symbol: str, since_utc=None) -> dict:
    """Fetch + analyze one symbol via the same tested helpers the MCP server
    uses. Returns a plain result dict; does not touch persisted state.
    `since_utc` is the previous run's time: bar_low/bar_high then span every
    bar that traded since it, so an intra-hour stop or target hit between
    runs is seen."""
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
        "bar_low": range_since(candles_1h, since_utc)[0],
        "bar_high": range_since(candles_1h, since_utc)[1],
        "candles_1h": candles_1h,
        "event_1h": latest_event(struct_1h),
        "event_4h": latest_event(struct_4h),
        "candles_4h": candles_4h,
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
    failures = 0
    positions = load_positions()
    paper = load_paper()

    # Checked once per run, not per symbol — the calendar is market-wide.
    news_blocked, news_note = await check_news_blackout(now)
    lines.append(f"  {news_note}")

    last_run = None
    if state.get("last_run"):
        try:
            last_run = datetime.fromisoformat(state["last_run"])
        except Exception:
            last_run = None

    for symbol in SYMBOLS:
        sym_state = state.get(symbol, {})
        try:
            r = await check_symbol(symbol, since_utc=last_run)
        except Exception as e:
            lines.append(f"{symbol}: ERROR fetching/analyzing - {e}")
            state[symbol] = sym_state
            failures += 1
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
        # Resolve any open paper trades for this symbol against the hour.
        for t in paper:
            if t["symbol"] == symbol and update_paper_trade(
                    t, read_for_trade(r, t, last_run), now):
                alert_lines.append(
                    f"[PAPER] {symbol} {t['direction']} -> {t['status']} "
                    f"({t.get('r', 0):+.2f}R)")

        # An open trade outranks hunting for a new one — managing what you
        # already hold is where the plan's rules actually get broken.
        for pos in positions:
            if pos["symbol"] == symbol and not pos.get("closed"):
                for msg in manage_position(pos, read_for_trade(r, pos, last_run), now):
                    telegram_msgs.append(msg)
                    alert_lines.append(f"[POSITION] {msg.splitlines()[0]}")

        if symbol in REVERSAL_SYMBOLS:
            setup = evaluate_setup(r, now, news_blocked=news_blocked)
            gate_str = ",".join(k for k, v in setup["gates"].items() if v) or "none"
        else:
            # No reversal edge on this symbol out-of-sample — do not run it.
            setup = {"ready": False, "gates": {}, "direction": None, "zone": None}
            gate_str = "n/a"

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
                used = open_slots(paper, positions)
                if used >= MAX_OPEN_POSITIONS:
                    # Valid setup, full book. Logged, not sent.
                    alert_lines.append(f"[SETUP CAPPED] {symbol} {setup['direction']} "
                                       f"({used} open, cap {MAX_OPEN_POSITIONS})")
                else:
                    telegram_msgs.append(format_setup_alert(symbol, r, setup, now))
                    paper.append(record_paper_trade(symbol, setup, now))
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

        # Trend-continuation: the other half of the plan (added 2026-08-29).
        # Independent of the reversal setup above — both can fire, neither
        # blocks the other, since they trade different situations (a
        # pullback vs. a break already running).
        if symbol in CONTINUATION_SYMBOLS:
            cont_setup = evaluate_trend_continuation_setup(
                r["trend_1d"], r["event_4h"], r["candles_4h"], price, now,
                news_blocked=news_blocked)
            cont_gate_str = ",".join(k for k, v in cont_setup["gates"].items() if v) or "none"
        else:
            cont_setup = {"ready": False, "gates": {}, "direction": None}
            cont_gate_str = "n/a"
        if cont_setup["ready"]:
            ev = cont_setup["event"]
            cont_key = f"cont:{cont_setup['direction']}:{ev['time']}:{ev['level']}"
            if cont_key != sym_state.get("last_cont_setup_key"):
                used = open_slots(paper, positions)
                if used >= MAX_OPEN_POSITIONS:
                    alert_lines.append(f"[CONTINUATION CAPPED] {symbol} {cont_setup['direction']} "
                                       f"({used} open, cap {MAX_OPEN_POSITIONS})")
                else:
                    telegram_msgs.append(format_continuation_alert(symbol, cont_setup, now))
                    paper.append(record_paper_trade(symbol, cont_setup, now, mode="continuation"))
                sym_state["last_cont_setup_key"] = cont_key
            alert_lines.append(f"[CONTINUATION READY] {symbol} {cont_setup['direction']}")

        demand_str = (f"{r['demand']['bottom']}-{r['demand']['top']}"
                      if r["demand"] else "none nearby")
        lines.append(
            f"{symbol}: ${price} | 1D {r['trend_1d']} / 4H {r['trend_4h']}"
            f" / 1H {r['trend_1h']} | "
            f"funding {r['funding_pct']}% | support {demand_str} | "
            f"bull>{r['bull_confirm']} | bear<{r['bear_confirm']}"
        )
        lines.append(f"  setup gates passed: {gate_str}")
        lines.append(f"  continuation gates passed: {cont_gate_str}")
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

    # Self-reported failures: API outages, code faults, bad data. Alert once
    # on crossing the threshold rather than every hour, so a long outage does
    # not become its own spam problem.
    all_failed = failures == len(SYMBOLS)
    streak = state.get("consecutive_failures", 0) + 1 if all_failed else 0
    state["consecutive_failures"] = streak
    if all_failed and streak == MAX_CONSECUTIVE_FAILURES:
        telegram_msgs.append(
            f"MONITOR PROBLEM — {streak} consecutive failed runs.\n"
            f"Both symbols failed to fetch or analyse. Levels are NOT being "
            f"watched right now.\n"
            f"Check: ssh to the VM, then tail -20 ~/trading-analysis/monitor_log.txt\n"
            + footer(now)
        )

    # One heartbeat a day. Without it, a dead monitor and a quiet market look
    # identical from the phone — which is exactly how 19 green-but-empty CI
    # runs went unnoticed.
    if now.hour == HEARTBEAT_HOUR_UTC and state.get("last_heartbeat") != now.date().isoformat():
        health = "all good" if streak == 0 else f"WARNING: {streak} failed runs"
        telegram_msgs.append(
            f"Daily check-in — monitor is alive ({health}).\n\n"
            + "\n".join(summary)
            + f"\n\n{news_note}."
            + "\nNo setup alert today means no setup, not a failure.\n"
            + footer(now)
        )
        state["last_heartbeat"] = now.date().isoformat()

    # Stopped-out trades drop off the list; everything else keeps its flags
    # so a milestone is announced once, not every hour.
    save_positions([p for p in positions if not p.get("closed")])
    save_paper(paper)
    state["last_run"] = now.isoformat()

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

    # Last thing: check in with the external watchdog. If this stops
    # arriving, the service tells Chamara — covering the one failure mode
    # this process can never report on its own.
    ping_healthcheck(ok=not all_failed)


def cli(argv) -> int:
    """Manage tracked positions. Kept in this file so the VM has one script
    to deploy, not two that can drift apart."""
    usage = (
        "usage:\n"
        "  standalone_monitor.py                        run the hourly check\n"
        "  standalone_monitor.py list                   show tracked trades\n"
        "  standalone_monitor.py add SYM long|short ENTRY STOP [reversal|continuation]\n"
        "  standalone_monitor.py close SYM\n"
    )
    if not argv:
        return -1

    cmd, rest = argv[0], argv[1:]
    positions = load_positions()
    paper = load_paper()

    if cmd == "list":
        if not positions:
            print("No tracked positions.")
        for p in positions:
            flags = [k for k in ("breakeven_done", "partial_done") if p.get(k)]
            print(f"{p['symbol']} {p['direction']} entry {p['entry']} "
                  f"stop {p['stop']} {'[' + ','.join(flags) + ']' if flags else ''}")
        return 0

    if cmd == "add" and len(rest) in (4, 5):
        sym, direction, entry, stop = rest[0].upper(), rest[1].lower(), float(rest[2]), float(rest[3])
        # Which gate produced it decides how it is managed: reversal closes in
        # full at 1:2, continuation banks half and trails behind 4H swings.
        mode = rest[4].lower() if len(rest) == 5 else "reversal"
        if mode not in ("reversal", "continuation"):
            print("mode must be reversal or continuation")
            return 2
        if direction not in ("long", "short"):
            print("direction must be long or short")
            return 2
        # A long's stop belongs below entry and a short's above. Catching this
        # here beats discovering it from a nonsensical alert later.
        if (direction == "long" and stop >= entry) or (direction == "short" and stop <= entry):
            print(f"stop {stop} is on the wrong side of entry {entry} for a {direction}")
            return 2
        positions = [p for p in positions if p["symbol"] != sym]
        positions.append({"symbol": sym, "direction": direction, "entry": entry,
                           "stop": stop, "mode": mode,
                           "opened": datetime.now(timezone.utc).isoformat()})
        save_positions(positions)
        risk_dist = abs(entry - stop)
        print(f"Tracking {sym} {direction} entry {entry} stop {stop} as a {mode} trade.")
        print("Exit rule: " + ("full close at 1:2." if mode == "reversal"
                               else "half at 1:2, trail the rest behind 4H swings."))
        print(f"Breakeven alert at {round(entry + risk_dist, 2) if direction == 'long' else round(entry - risk_dist, 2)} (1R), "
              f"1:2 at {round(entry + 2 * risk_dist, 2) if direction == 'long' else round(entry - 2 * risk_dist, 2)}.")
        return 0

    if cmd == "close" and len(rest) == 1:
        sym = rest[0].upper()
        remaining = [p for p in positions if p["symbol"] != sym]
        if len(remaining) == len(positions):
            print(f"No tracked position for {sym}.")
            return 1
        save_positions(remaining)
        print(f"Stopped tracking {sym}.")
        return 0

    print(usage)
    return 2


if __name__ == "__main__":
    if len(sys.argv) > 1:
        raise SystemExit(cli(sys.argv[1:]))
    try:
        asyncio.run(main())
    except Exception as e:
        log(f"FATAL: {e}")
        raise
