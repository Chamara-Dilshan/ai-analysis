"""
Weekly scorecard: is the live record behaving like the backtests said it
would, are real fills matching the alerts, and is choosing which alerts to
take helping or hurting?

The "expected range" comes from scorecard_reference.json - the net R of every
trade the live configuration would have taken across three backtest windows,
in trade order. For N live trades, every run of N CONSECUTIVE backtest trades
is summed; the 5th-95th percentile of those sums is the normal range. Sliding
windows rather than shuffling keeps the losing streaks and regimes in - this
system's bad stretches cluster, and a shuffled range would call ordinary
clustering a failure.

Pure functions over the journals: no I/O except reading the reference file.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

REF_PATH = Path(__file__).parent / "scorecard_reference.json"

# Below this many trades even a bad result is within what bad luck explains,
# so the scorecard says "watch", not "stop". Fixed here, before the data
# arrives, so it cannot be moved once the record is disappointing.
MIN_TRADES_TO_JUDGE = 50


def load_reference(path: Path = REF_PATH) -> list:
    """Per-window R sequences, in trade order."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return [w["r"] for w in data["windows"].values()]


def expected_band(n: int, sequences: list):
    """(p5, p50, p95) total R over n consecutive trades, or None when no
    backtest window is that long."""
    if n <= 0:
        return None
    sums = []
    for seq in sequences:
        if len(seq) < n:
            continue
        run = sum(seq[:n])
        sums.append(run)
        for i in range(n, len(seq)):
            run += seq[i] - seq[i - n]
            sums.append(run)
    if not sums:
        return None
    sums.sort()

    def pct(p):
        return sums[min(len(sums) - 1, int(p * len(sums)))]
    return pct(0.05), pct(0.50), pct(0.95)


def verdict(n: int, total_r: float, band) -> str:
    if band is None:
        return "more trades than any backtest window - compare by eye."
    low, _, high = band
    if total_r < low:
        if n >= MIN_TRADES_TO_JUDGE:
            return ("BELOW the backtests' worst 5%. Plan rule: stop real-money "
                    "trades and review what changed.")
        return (f"below the usual range, but {n} trades is too few to judge "
                f"(rule applies from {MIN_TRADES_TO_JUDGE}). Keep going, watch it.")
    if total_r > high:
        return ("above the usual range - better than the backtests. A lucky "
                "stretch is as likely as a better system.")
    return "on track - inside the range the backtests produced."


def _resolved(trades):
    return [t for t in trades if t.get("status") in ("win", "loss")]


def _closed_since(trades, since):
    out = []
    for t in _resolved(trades):
        try:
            if datetime.fromisoformat(t["closed"]) >= since:
                out.append(t)
        except (KeyError, ValueError):
            pass
    return out


def fill_slippage_pct(real: list) -> list:
    """Per real trade taken from an alert: how far the fill was from the
    alert's entry, in % of price. Positive = worse for the trade."""
    out = []
    for t in real:
        alert = t.get("alert_entry")
        if not alert:
            continue
        diff = (t["entry"] - alert) / alert * 100
        out.append(diff if t["direction"] == "long" else -diff)
    return out


def split_taken(paper: list, real: list):
    """(taken, skipped) resolved paper trades. A paper trade was taken when a
    real trade was logged from its alert - same symbol, same alert entry."""
    taken_keys = {(t["symbol"], t.get("alert_entry")) for t in real
                  if t.get("alert_entry") is not None}
    taken, skipped = [], []
    for t in _resolved(paper):
        (taken if (t["symbol"], t["entry"]) in taken_keys else skipped).append(t)
    return taken, skipped


def _r(trades):
    return sum(t.get("r", 0) for t in trades)


def weekly_scorecard(paper: list, real: list, drawdown_usd: float,
                     now: datetime, sequences: list = None) -> str:
    sequences = sequences if sequences is not None else load_reference()
    week_ago = now - timedelta(days=7)
    lines = [f"WEEKLY SCORECARD - {now.strftime('%d %b %Y')}"]

    # The system: every setup it fired, taken or not.
    done = _resolved(paper)
    wk = _closed_since(paper, week_ago)
    n, total = len(done), _r(done)
    lines.append(f"\nSystem (paper): {len(wk)} closed this week ({_r(wk):+.1f}R). "
                 f"All time {n} trades, {total:+.1f}R.")
    band = expected_band(n, sequences)
    if band:
        lines.append(f"Backtests, {n} trades in a row: {band[0]:+.1f}R to "
                     f"{band[2]:+.1f}R (typical {band[1]:+.1f}R).")
    if n:
        lines.append("Verdict: " + verdict(n, total, band))
    lines.append(f"Drawdown: ${-drawdown_usd:.2f} below the high "
                 f"(review $60 / stop $100).")

    # You: the trades actually taken.
    rdone = _resolved(real)
    rwk = _closed_since(real, week_ago)
    lines.append(f"\nYou (real): {len(rwk)} closed this week ({_r(rwk):+.2f}R). "
                 f"All time {len(rdone)} trades, {_r(rdone):+.2f}R before fees, "
                 f"{sum(1 for t in real if t.get('status') == 'open')} open.")
    slips = fill_slippage_pct(real)
    if slips:
        avg = sum(slips) / len(slips)
        lines.append(f"Fills vs alerts: {abs(avg):.2f}% {'worse' if avg > 0 else 'better'} "
                     f"on average over {len(slips)} trade(s).")

    # Choosing: did the alerts you took beat the ones you skipped?
    taken, skipped = split_taken(paper, real)
    if taken or skipped:
        def avg(ts):
            return f"{_r(ts) / len(ts):+.2f}R avg" if ts else "none"
        lines.append(f"\nAlerts you took: {len(taken)} ({avg(taken)}). "
                     f"Skipped: {len(skipped)} ({avg(skipped)}).")
        if len(taken) >= 10 and len(skipped) >= 10:
            better = _r(taken) / len(taken) > _r(skipped) / len(skipped)
            lines.append("Your picking is " + ("helping." if better else
                         "hurting - the skipped alerts did better. Consider taking every alert."))
    return "\n".join(lines)
