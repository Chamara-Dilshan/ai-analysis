"""
The forward record and position management: paper trades opened and
resolved, the drawdown rule, open slots, and the hourly breakeven /
trail / stop / opposing-signal checks on a real position. Pure
functions over the journal dicts; loading and saving them stays in
standalone_monitor, next to the paths the tests redirect.

Split out of standalone_monitor.py (2026-10-21), which re-exports every
name here: standalone_monitor.X keeps working for bot.py, backtest.py and
the tests.
"""
from datetime import datetime, timezone
from zones import range_since
from gates import CAPITAL, RISK_PCT, TRIGGER_MAX_AGE
from alerts import footer


# The plan's drawdown rule (Section 7, 2026-10-20), measured on the paper
# record from its highest point. It replaced daily/weekly/monthly breaks,
# which a portfolio replay showed cost money without reducing the drop: the
# system's real risk is a slow bleed of ~$80-90 over months (worst tested
# drawdowns -$78 to -$91 on $200), which no calendar limit ever sees.
DRAWDOWN_REVIEW_USD = 60.0


DRAWDOWN_STOP_USD = 100.0


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

    # Breakeven must never LOOSEN a stop the trail has already advanced past
    # entry — manage_position guards this with max(trail, base); this copy
    # did not, the same drift that left the price guard below unapplied here.
    def to_breakeven(current):
        return max(current, entry) if long else min(current, entry)

    hit_2r = hi >= t["target"] if long else lo <= t["target"]
    if hit_2r and not partial_done:
        t.update(partial_done=True, partial_r=1.0, breakeven_done=True)
        cur = to_breakeven(cur)
    elif not t.get("breakeven_done"):
        one_r = entry + risk_dist if long else entry - risk_dist
        if (hi >= one_r) if long else (lo <= one_r):
            t["breakeven_done"] = True
            cur = to_breakeven(cur)

    # Trail behind the latest confirmed 4H swing. Never backwards, and never
    # past price: a swing confirms several candles after it forms, so price
    # may already be the wrong side of it. Parking the stop there books an
    # exit that never traded — and always in profit, so it silently flatters
    # the one record this project treats as untainted. Same guard as
    # manage_position; this copy was missed when that one was fixed.
    swing = r.get("recent_low_4h") if long else r.get("recent_high_4h")
    price_now = r.get("price")
    if swing is not None and price_now is not None and (
            (long and swing >= price_now) or (not long and swing <= price_now)):
        swing = None
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
    # after_only: the bar the entry sits inside also contains pre-entry price,
    # so it cannot be used to declare this trade stopped. Costs up to an hour
    # of delay on a genuine stop — the exchange holds the real one and this
    # layer only reports — and removes a guaranteed false stop-out.
    lo, hi = range_since(r["candles_1h"], start, after_only=True)
    return dict(r, bar_low=lo, bar_high=hi)


def paper_summary(trades: list) -> str:
    """Plain-language scoreboard of the forward record so far."""
    done = [t for t in trades if t["status"] in ("win", "loss")]
    if not done:
        open_n = sum(1 for t in trades if t["status"] == "open")
        return f"Paper record: no trades resolved yet ({open_n} open)."
    wins = [t for t in done if t["status"] == "win"]
    total_r = sum(t.get("r", 0) for t in done)
    # Convert per trade at the risk % that trade ACTUALLY used. A flat 2% is
    # wrong for continuation entries, which size to Binance's minimum notional
    # and can run well above it — and R and dollars can diverge sharply when
    # the risk % varies (a structural-stop run once showed +5.1R worth +$5,
    # because the wins landed on small-risk trades and the losses on large).
    # This is the record used to judge whether the system works; it should not
    # flatter itself.
    total_usd = sum(t.get("r", 0) * CAPITAL * t.get("risk_pct_used", RISK_PCT) / 100
                    for t in done)
    money = f"{'+' if total_usd >= 0 else '-'}${abs(total_usd):.2f}"
    return (
        f"Paper record: {len(done)} resolved, {len(wins)} wins "
        f"({100 * len(wins) / len(done):.0f}%), {total_r:+.1f}R "
        f"({money} at each trade's actual sizing). "
        f"{sum(1 for t in trades if t['status'] == 'open')} still open."
    )


def paper_drawdown(trades: list) -> tuple:
    """(equity, drawdown) in USD of the resolved paper record, walked in the
    order trades closed. Drawdown is <= 0: how far equity sits below its own
    highest point so far, starting from the journal's $0."""
    done = sorted((t for t in trades if t.get("status") in ("win", "loss")),
                  key=lambda t: t.get("closed") or t.get("opened") or "")
    equity = peak = 0.0
    for t in done:
        equity += t.get("r", 0) * CAPITAL * t.get("risk_pct_used", RISK_PCT) / 100
        peak = max(peak, equity)
    return equity, equity - peak


def drawdown_check(trades: list, prev_level):
    """(level, message) for the plan's drawdown rule. level is None, "review"
    or "stop". message is set only when the level gets WORSE than prev_level,
    so each line is announced once per drawdown. The level clears only after
    recovering to half the review line — hovering around -$60 must not
    re-alert every hour."""
    _, dd = paper_drawdown(trades)
    rank = {None: 0, "review": 1, "stop": 2}
    if dd <= -DRAWDOWN_STOP_USD:
        level = "stop"
    elif dd <= -DRAWDOWN_REVIEW_USD:
        level = "review"
    elif dd > -DRAWDOWN_REVIEW_USD / 2:
        level = None
    else:
        level = prev_level            # between the lines: hold, no flapping
    if rank[level] <= rank.get(prev_level, 0):
        return level, None
    if level == "stop":
        msg = (f"DRAWDOWN STOP — paper record is ${-dd:.2f} below its high "
               f"(line: ${DRAWDOWN_STOP_USD:.0f}).\n"
               "Plan Section 7: stop real-money trades. Paper only until the "
               "record makes a new high. No backtested period ever fell this "
               "far, so something has changed — review before going back.\n")
    else:
        msg = (f"DRAWDOWN REVIEW — paper record is ${-dd:.2f} below its high "
               f"(line: ${DRAWDOWN_REVIEW_USD:.0f}).\n"
               "Plan Section 7: keep going, but check your recent real trades "
               "against their alerts — same entry, stop and exit? Drops of "
               "$78-$91 happened in every tested period; this alone is not a "
               "reason to quit.\n")
    return level, msg


def open_slots(paper: list, positions: list) -> int:
    """Distinct symbols currently occupying a slot: an open paper trade (every
    fired setup) or a tracked real position. Distinct so a real trade that
    is also on the paper record is not counted twice."""
    syms = {t["symbol"] for t in paper if t.get("status") == "open"}
    syms |= {p["symbol"] for p in positions if not p.get("closed")}
    return len(syms)


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
            f"{outcome}\nRemoved from tracking. Log your exit: "
            f"/closed {pos['symbol']} <price>\n{footer(now_utc)}"
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
                f"testing.\nRemoved from tracking. Log your exit: "
                f"/closed {pos['symbol']} <price>\n{footer(now_utc)}"
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
        # A swing only confirms `swing_strength` candles after it forms, so by
        # then price can already be the wrong side of it — a confirmed 4H low
        # ABOVE a long's current price is a stop that exits the moment it is
        # placed, and the position then reports as a win it never earned.
        # Ignore those; the next confirmed swing on the right side will trail.
        if candidate is not None and ((long and candidate >= price) or
                                      (not long and candidate <= price)):
            candidate = None
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
