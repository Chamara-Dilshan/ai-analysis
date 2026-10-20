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
import time
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

import server
import structure
import dashboard
import news
import scorecard

# Moved to their own modules 2026-10-21; re-exported so standalone_monitor.X
# keeps working everywhere. The hourly run below still calls them by these
# names, which is what lets the tests patch them here.
from zones import (  # noqa: F401
    MIN_ZONE_PCT,
    ZONE_TOUCH_PCT,
    is_newer,
    latest_event,
    latest_event_time,
    pick_bear_confirm,
    pick_bull_confirm,
    pick_demand_zone,
    pick_supply_zone,
    range_since,
    reached_zone,
    wide_enough,
)
from gates import (  # noqa: F401
    CAPITAL,
    KILL_ZONES,
    LEVERAGE,
    MAX_FEE_PCT_OF_RISK,
    RISK_PCT,
    TRIGGER_MAX_AGE,
    _continuation_fee,
    _structural_stop,
    active_kill_zone,
    evaluate_setup,
    evaluate_trend_continuation_setup,
)
from alerts import (  # noqa: F401
    LOT_STEPS,
    SL_OFFSET,
    _fmt_qty,
    footer,
    format_continuation_alert,
    format_math,
    format_setup_alert,
    order_ticket,
)
from journal import (  # noqa: F401
    DRAWDOWN_REVIEW_USD,
    DRAWDOWN_STOP_USD,
    drawdown_check,
    manage_position,
    open_slots,
    paper_drawdown,
    paper_summary,
    read_for_trade,
    record_paper_trade,
    update_paper_trade,
)

HERE = Path(__file__).parent
STATE_PATH = HERE / "monitor_state.json"
POSITIONS_PATH = HERE / "positions.json"
PAPER_PATH = HERE / "paper_trades.json"
# Trades actually taken, logged through bot.py (/took, /closed).
REAL_PATH = HERE / "real_trades.json"
# Shared by the hourly run, the CLI and bot.py. Each loads these JSON files,
# changes them and writes them back; without one lock a /took arriving
# mid-run would be overwritten by the run's own save a few seconds later.
LOCK_PATH = HERE / "monitor.lock"
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
# risk at once. Cap concurrent open
# setups; anything beyond it is logged, not alerted, so the paper record
# still shows what the cap cost or saved.
MAX_OPEN_POSITIONS = 2

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


@contextmanager
def state_lock(timeout: float = 180.0):
    """Exclusive lock over the state, journal and positions files. A no-op
    where fcntl does not exist (Windows dev machines), which only ever run one
    writer at a time anyway."""
    try:
        import fcntl
    except ImportError:
        yield
        return
    with open(LOCK_PATH, "w") as fh:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"{LOCK_PATH.name} held for over {timeout:.0f}s")
                time.sleep(0.5)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


# Message kinds that /pause holds back. Everything else - stop hits,
# breakeven and trail cues, drawdown lines, monitor problems, the heartbeat -
# still goes out: a paused alert stream must never hide a live position.
SETUP_ALERT_PREFIXES = ("SETUP READY", "CONTINUATION SETUP", "HEADS UP")


def paused_until(state: dict, now_utc: datetime):
    """The pause end time if alerts are paused right now, else None."""
    raw = state.get("paused_until")
    if not raw:
        return None
    try:
        until = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return until if until > now_utc else None


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


def send_telegram(message: str) -> bool:
    """Best-effort Telegram push, for runs where nobody is looking at this
    machine. Credentials come from the environment only — never hard-code a
    token here. Same contract as show_toast: a notification failure must
    never affect the monitor.

    Returns True only when the message actually reached Telegram. The caller
    uses that to decide whether an alert may be marked as already-sent: a
    setup's dedup key never recurs, so recording it after a failed send
    suppresses that alert forever.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message,
                  "disable_web_page_preview": True},
            timeout=15,
        )
        if resp.status_code != 200:
            log(f"(telegram failed: HTTP {resp.status_code})")
            return False
        return True
    except Exception as e:
        # The token is in the request URL, and some exception strings carry the
        # URL. Scrub it — this log can end up in a CI run log or an uploaded
        # artifact, which may be world-readable.
        detail = str(e).replace(token, "<token>")
        log(f"(telegram failed: {type(e).__name__}: {detail})")
    return False


def send_document(filename: str, content: bytes, caption: str = "") -> bool:
    """Send a file to the Telegram chat (the dashboard). Same contract as
    send_telegram: True only if it arrived, and it never raises."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/sendDocument",
            data={"chat_id": chat_id, "caption": caption},
            files={"document": (filename, content, "text/html")},
            timeout=30,
        )
        if resp.status_code != 200:
            log(f"(telegram document failed: HTTP {resp.status_code})")
            return False
        return True
    except Exception as e:
        log(f"(telegram document failed: {type(e).__name__})")
        return False


def dashboard_file(now_utc: datetime) -> tuple:
    """(filename, bytes) of the dashboard built from the journals on disk."""
    html_text = dashboard.build_dashboard(load_paper(), load_real(), now_utc,
                                          running_version())
    return f"dashboard-{now_utc.strftime('%Y-%m-%d')}.html", html_text.encode("utf-8")


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


def load_paper() -> list:
    if PAPER_PATH.exists():
        try:
            return json.loads(PAPER_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def save_paper(trades: list) -> None:
    PAPER_PATH.write_text(json.dumps(trades, indent=2), encoding="utf-8")


def load_real() -> list:
    if REAL_PATH.exists():
        try:
            return json.loads(REAL_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def save_real(trades: list) -> None:
    REAL_PATH.write_text(json.dumps(trades, indent=2), encoding="utf-8")


def load_positions() -> list:
    if POSITIONS_PATH.exists():
        try:
            return json.loads(POSITIONS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def save_positions(positions: list) -> None:
    POSITIONS_PATH.write_text(json.dumps(positions, indent=2), encoding="utf-8")


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


def running_version() -> str:
    """Short commit id and subject of the deployed code, for the heartbeat."""
    try:
        out = subprocess.run(["git", "log", "-1", "--format=%h %s"], cwd=HERE,
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or "unknown version"
    except Exception:
        return "unknown version"


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


async def refresh_lot_steps() -> None:
    try:
        info = await server._get(server.BINANCE_BASE_URL, "/fapi/v1/exchangeInfo",
                                 cache_ttl=3600)
        for sym in info.get("symbols", []):
            if sym.get("symbol") in SYMBOLS:
                for f in sym.get("filters", []):
                    if f.get("filterType") == "LOT_SIZE":
                        LOT_STEPS[sym["symbol"]] = float(f["stepSize"])
    except Exception as e:
        log(f"(lot sizes not refreshed, using fallbacks: {e})")


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
    # max_items defaults to 5 — a payload cap for the MCP display, where the
    # caller only wants a readable summary. pick_demand_zone/pick_supply_zone
    # treat this list as the COMPLETE zone universe, and analyze keeps the 5
    # NEWEST, not the 5 nearest price. So the nearest-zone choice was being
    # made over an arbitrary handful (measured: 5 of 17 open FVGs, 5 of 11
    # open OBs on a real read). Ask for the lot here; the display path keeps
    # its own default.
    struct_4h = structure.analyze(candles_4h, swing_strength=3, max_items=100)
    struct_1d = structure.analyze(candles_1d, swing_strength=2, max_items=100)
    struct_1h = structure.analyze(candles_1h, swing_strength=2, max_items=100)

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


async def main() -> None:
    now = datetime.now(timezone.utc)
    state = load_state()
    lines = [f"=== {now.isoformat()} ==="]
    alert_lines = []      # everything notable, for the log
    telegram_msgs = []    # only what is worth interrupting him for
    summary = []          # one line per symbol, for the daily heartbeat
    failures = 0
    # (dict, key, previous_value) for every flag written on the strength of a
    # Telegram message that has not been sent yet. Rolled back if the send
    # fails — a setup key never recurs, so recording one after a failed push
    # suppresses that alert permanently. Covers dedup keys, the daily
    # heartbeat stamp, and a position's breakeven/trail announcement flags.
    # It must NEVER carry pos["closed"]: a stop that was hit was hit whether
    # or not the message arrived, and undoing it would re-close the trade.
    pending_alert_keys = []
    # Paper trades appended this run. The journal is the one forward record no
    # backtest bug can contaminate, so a trade recorded on the strength of a
    # push that never landed has to come back out: the rolled-back dedup key
    # re-alerts next run, and without this the setup would be journalled twice.
    pending_paper_trades = []
    positions = load_positions()
    paper = load_paper()
    await refresh_lot_steps()

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

        # "Price inside an open zone" was self-contradictory and never true:
        # a zone is OPEN precisely because price has not returned to it, so
        # bottom <= price <= top could not hold and entered_demand was dead
        # code (was_in_demand sat at false permanently in monitor_state.json).
        # Use the same proximity test the real gate uses, against the bar's
        # RANGE rather than the close, so a wick into the zone counts.
        was_in = sym_state.get("was_in_demand", False)
        in_now = bool(r["demand"]) and reached_zone(
            r["demand"], price, r.get("bar_low"), r.get("bar_high"))
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
                # Snapshot the flags that exist only to say a milestone was
                # ANNOUNCED. If the push fails they have to go back, or the
                # "move your stop" message is announced once and lost. pos
                # ["closed"] is deliberately not among them — see above.
                was_be = pos.get("breakeven_done")
                was_trail = pos.get("trail_stop")
                msgs = manage_position(pos, read_for_trade(r, pos, last_run), now)
                if msgs:
                    if pos.get("breakeven_done") != was_be:
                        pending_alert_keys.append((pos, "breakeven_done", was_be))
                    if pos.get("trail_stop") != was_trail:
                        pending_alert_keys.append((pos, "trail_stop", was_trail))
                for msg in msgs:
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
                    # Valid setup, full book. Logged, not sent — and NOT
                    # recorded as alerted. Writing the key here cost a real
                    # trade on 2026-10-08: ETH was capped at 13:07, a slot
                    # freed at 14:07 with every gate still passing, and the
                    # key this branch had written suppressed the send. The
                    # dedup records what was SENT, never what was merely seen.
                    alert_lines.append(f"[SETUP CAPPED] {symbol} {setup['direction']} "
                                       f"({used} open, cap {MAX_OPEN_POSITIONS})")
                else:
                    trade = record_paper_trade(symbol, setup, now)
                    telegram_msgs.append(format_setup_alert(symbol, r, setup, now)
                                         + f"\nTook it? Reply /took {symbol}")
                    paper.append(trade)
                    pending_paper_trades.append(trade)
                    pending_alert_keys.append(
                        (sym_state, "last_setup_key", sym_state.get("last_setup_key")))
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
                # Registered for rollback like the setup keys. Without this a
                # failed push marked the zone notified forever, so the only
                # heads-up for it was the one that never arrived.
                pending_alert_keys.append(
                    (sym_state, "last_headsup_key", sym_state.get("last_headsup_key")))
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
                    # Same rule as the reversal branch above, and the stakes
                    # are higher here: cont_key is pinned to one 4H event and
                    # never goes stale, so a key written while capped silenced
                    # that break permanently, even once a slot freed.
                    alert_lines.append(f"[CONTINUATION CAPPED] {symbol} {cont_setup['direction']} "
                                       f"({used} open, cap {MAX_OPEN_POSITIONS})")
                else:
                    trade = record_paper_trade(symbol, cont_setup, now,
                                               mode="continuation")
                    telegram_msgs.append(format_continuation_alert(symbol, cont_setup, now)
                                         + f"\nTook it? Reply /took {symbol}")
                    paper.append(trade)
                    pending_paper_trades.append(trade)
                    pending_alert_keys.append(
                        (sym_state, "last_cont_setup_key",
                         sym_state.get("last_cont_setup_key")))
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
    # ANY symbol failing is a degraded run, not just all of them. The old
    # all-or-nothing test meant 4 of 5 symbols erroring every hour reported
    # "all good", pinged the healthcheck OK, and listed one symbol in the
    # heartbeat — the monitor's whole failure mode, applied to itself.
    all_failed = failures == len(SYMBOLS)
    degraded = failures > 0
    streak = state.get("consecutive_failures", 0) + 1 if degraded else 0
    state["consecutive_failures"] = streak
    if degraded and streak == MAX_CONSECUTIVE_FAILURES:
        scope = ("Every symbol failed" if all_failed
                 else f"{failures} of {len(SYMBOLS)} symbols failed")
        telegram_msgs.append(
            f"MONITOR PROBLEM — {streak} consecutive degraded runs.\n"
            f"{scope} to fetch or analyse. Those levels are NOT being "
            f"watched right now.\n"
            f"Check: ssh to the VM, then tail -20 ~/trading-analysis/monitor_log.txt\n"
            + footer(now)
        )

    # The plan's drawdown rule, on the forward paper record. Recorded only
    # once the push lands, like every other "he was told" flag.
    prev_dd = state.get("drawdown_alert")
    dd_level, dd_msg = drawdown_check(paper, prev_dd)
    if dd_msg:
        telegram_msgs.append(dd_msg + footer(now))
        pending_alert_keys.append((state, "drawdown_alert", prev_dd))
    if dd_level is None:
        state.pop("drawdown_alert", None)
    else:
        state["drawdown_alert"] = dd_level

    # The weekly scorecard: the live record against what the backtests say
    # is normal for that many trades. Sundays, with the heartbeat.
    week = now.date().isoformat()
    send_dashboard = False
    if (now.weekday() == 6 and now.hour == HEARTBEAT_HOUR_UTC
            and state.get("last_scorecard") != week):
        try:
            _, dd_now = paper_drawdown(paper)
            telegram_msgs.append(scorecard.weekly_scorecard(paper, load_real(), dd_now, now)
                                 + "\n" + footer(now))
            pending_alert_keys.append((state, "last_scorecard", state.get("last_scorecard")))
            state["last_scorecard"] = week
            send_dashboard = True
        except Exception as e:
            log(f"(scorecard failed: {e!r})")

    # One heartbeat a day. Without it, a dead monitor and a quiet market look
    # identical from the phone — which is exactly how 19 green-but-empty CI
    # runs went unnoticed.
    if now.hour == HEARTBEAT_HOUR_UTC and state.get("last_heartbeat") != now.date().isoformat():
        if streak:
            health = f"WARNING: {streak} degraded runs, {failures}/{len(SYMBOLS)} symbols failing"
        elif len(summary) < len(SYMBOLS):
            # Never say "all good" while silently reporting fewer symbols.
            health = f"WARNING: only {len(summary)}/{len(SYMBOLS)} symbols reported"
        else:
            health = "all good"
        telegram_msgs.append(
            f"Daily check-in — monitor is alive ({health}).\n\n"
            + "\n".join(summary)
            + f"\n\n{news_note}."
            + "\nNo setup alert today means no setup, not a failure.\n"
            + f"Running {running_version()}.\n"
            + footer(now)
        )
        # Stamped only if the push lands. Marking the day done on a failed
        # send costs the one message that distinguishes a dead monitor from
        # a quiet market — the exact thing this heartbeat exists to catch.
        pending_alert_keys.append((state, "last_heartbeat", state.get("last_heartbeat")))
        state["last_heartbeat"] = now.date().isoformat()

    # Only advance the cursor if every symbol was actually examined. A fetch
    # error skips that symbol's stop/target check for the hour; moving
    # last_run anyway would shrink the next run's bar_low/bar_high window past
    # the gap, so the missed hour is never re-examined and a stop touched
    # inside it goes unseen. Holding the cursor makes the next run cover both.
    if failures == 0:
        state["last_run"] = now.isoformat()
    else:
        lines.append(f"last_run held at {state.get('last_run')} — "
                     f"{failures} symbol(s) failed, next run re-covers this hour")

    lines.append(footer(now))
    log("\n".join(lines))

    if alert_lines:
        alert_text = "\n".join(alert_lines) + "\n" + footer(now)
        ALERT_PATH.write_text(alert_text, encoding="utf-8")
        show_toast("BTC/ETH monitor", alert_lines[0])
        log("--- ALERT ---\n" + alert_text)

    # Structure events and level crossings stay in the log. Only setups,
    # heads-ups and the heartbeat are worth a phone notification — an alert
    # that fires on every wiggle is an alert that gets muted.
    until = paused_until(state, now)
    if until:
        held = [m for m in telegram_msgs if m.startswith(SETUP_ALERT_PREFIXES)]
        if held:
            telegram_msgs = [m for m in telegram_msgs if m not in held]
            log(f"(alerts paused until {until.strftime('%Y-%m-%d %H:%M')} UTC - "
                f"held back {len(held)} setup alert(s); still journalled)")

    sends_ok = True
    for msg in telegram_msgs:
        if not send_telegram(msg):
            sends_ok = False
        log("--- TELEGRAM ---\n" + msg)

    # Everything that records "he was told" is written AFTER the sends, so a
    # failed push can undo it. send_telegram never raises (it swallows its own
    # errors), so there is no path where this deferral loses the run's work.
    if not sends_ok:
        if pending_alert_keys:
            for state_ref, key_name, previous in pending_alert_keys:
                if previous is None:
                    state_ref.pop(key_name, None)
                else:
                    state_ref[key_name] = previous
            log(f"(telegram send failed — rolled back {len(pending_alert_keys)} alert "
                f"flag(s); the setup will alert again next run)")
        if pending_paper_trades:
            # Identity, not equality: two genuine setups on the same symbol
            # and hour would compare equal and both be dropped.
            unsent = {id(t) for t in pending_paper_trades}
            paper = [t for t in paper if id(t) not in unsent]
            log(f"(telegram send failed — dropped {len(unsent)} unsent paper "
                f"trade(s); the re-alert next run will journal them once)")

    # The dashboard rides along with the weekly scorecard. Best effort and
    # outside the rollback: it is a picture of the journals, not an alert,
    # and /dashboard can always produce another.
    if send_dashboard and sends_ok:
        try:
            name, content = dashboard_file(now)
            send_document(name, content, "Weekly dashboard - open it in your browser.")
        except Exception as e:
            log(f"(dashboard failed: {e!r})")

    # Stopped-out trades drop off the list; everything else keeps its flags
    # so a milestone is announced once, not every hour.
    save_positions([p for p in positions if not p.get("closed")])
    save_paper(paper)
    save_state(state)

    # Last thing: check in with the external watchdog. If this stops
    # arriving, the service tells Chamara — covering the one failure mode
    # this process can never report on its own.
    # Any failed symbol means this run did not do its job — ping not-OK so the
    # external watchdog sees a partial outage, not just a total one.
    ping_healthcheck(ok=not degraded)


CLI_USAGE = (
    "usage:\n"
    "  standalone_monitor.py                        run the hourly check\n"
    "  standalone_monitor.py list                   show tracked trades\n"
    "  standalone_monitor.py add SYM long|short ENTRY STOP [reversal|continuation]\n"
    "  standalone_monitor.py close SYM\n"
)


def cli(argv) -> int:
    """Manage tracked positions. Kept in this file so the VM has one script
    to deploy, not two that can drift apart."""
    if not argv:
        return -1

    with state_lock():
        return _cli(argv)


def _cli(argv) -> int:
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
        # Refuse to silently replace a live position: doing so discards its
        # breakeven_done / trail_stop / opened state and the trade carries on
        # being managed as if it had just opened. Make the destructive step
        # explicit instead of printing success over it.
        existing = next((p for p in positions if p["symbol"] == sym), None)
        if existing:
            flags = [k for k in ("breakeven_done", "partial_done") if existing.get(k)]
            print(f"{sym} is already tracked: {existing['direction']} entry "
                  f"{existing['entry']} stop {existing.get('trail_stop', existing['stop'])}"
                  f"{' [' + ','.join(flags) + ']' if flags else ''}, opened "
                  f"{existing.get('opened', 'unknown')}.")
            print(f"Close it first if you mean to replace it:  "
                  f"standalone_monitor.py close {sym}")
            return 2
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
        # The paper trade is a separate record and stayed "open", so it kept
        # occupying an open_slots slot against MAX_OPEN_POSITIONS forever —
        # quietly shrinking how many new setups could ever fire.
        freed = 0
        for t in paper:
            if t.get("symbol") == sym and t.get("status") == "open":
                t.update(status="closed_manually",
                         closed=datetime.now(timezone.utc).isoformat())
                freed += 1
        if freed:
            save_paper(paper)
        print(f"Stopped tracking {sym}."
              + (f" Released {freed} paper slot(s); "
                 f"{open_slots(paper, remaining)}/{MAX_OPEN_POSITIONS} now in use."
                 if freed else ""))
        return 0

    print(CLI_USAGE)
    return 2


if __name__ == "__main__":
    if len(sys.argv) > 1:
        raise SystemExit(cli(sys.argv[1:]))
    try:
        with state_lock():
            asyncio.run(main())
    except Exception as e:
        log(f"FATAL: {e}")
        raise
