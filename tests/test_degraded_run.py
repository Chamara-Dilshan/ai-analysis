"""
Regression tests for the two audit-2 findings deferred from 05f40ee:
  - a partial outage reported as healthy (all_failed required EVERY symbol to
    fail, so 4 of 5 down pinged the watchdog OK and said "all good")
  - analyze's max_items=5 display cap silently truncating the zone universe
    that pick_demand_zone treats as complete
"""
import structure
import standalone_monitor as m


def bar(i, low, high):
    mid = (low + high) / 2
    return {"open_time": f"t{i}", "open": mid, "high": float(high),
            "low": float(low), "close": mid, "volume": 1.0}


# --------------------------------------------------------------------------- #
# Zone universe must not be truncated for the selector
# --------------------------------------------------------------------------- #
def _zigzag(n=120):
    """Alternating swings, enough to produce well over 5 FVGs and OBs."""
    candles, price = [], 100.0
    for i in range(n):
        step = 6.0 if (i // 3) % 2 == 0 else -5.0
        price += step
        candles.append(bar(i, price - 2.0, price + 2.0))
    return candles


def test_analyze_default_truncates_but_reports_the_true_count():
    d = structure.analyze(_zigzag(), swing_strength=2)
    fvg = d["fair_value_gaps"]
    # The cap is a display cap: open_count is honest, the list is not.
    assert len(fvg["open"]) <= 5
    assert fvg["open_count"] >= len(fvg["open"])


def test_raising_max_items_exposes_the_whole_universe():
    c = _zigzag()
    small = structure.analyze(c, swing_strength=2)
    full = structure.analyze(c, swing_strength=2, max_items=100)
    assert len(full["fair_value_gaps"]["open"]) >= len(small["fair_value_gaps"]["open"])
    assert len(full["fair_value_gaps"]["open"]) == full["fair_value_gaps"]["open_count"]
    assert len(full["order_blocks"]["open"]) == full["order_blocks"]["open_count"]


def test_nearest_zone_needs_the_untruncated_list_to_be_correct():
    # Truncation keeps the NEWEST zones, not the nearest. Given a list where
    # the nearest zone is not among the newest five, the selector can only
    # find it if it was handed the whole universe.
    price = 100.0
    newest_far = [("bullish", 40.0 + i, 55.0 + i) for i in range(5)]
    nearest = [("bullish", 97.0, 99.0)]
    def struct(zones):
        return {"order_blocks": {"open": []},
                "fair_value_gaps": {"open": [
                    {"type": t, "bottom": lo, "top": hi} for t, lo, hi in zones]}}
    truncated = m.pick_demand_zone(struct(newest_far), price)
    complete = m.pick_demand_zone(struct(nearest + newest_far), price)
    assert truncated["top"] < 60.0          # best available from a truncated list
    assert (complete["bottom"], complete["top"]) == (97.0, 99.0)


# --------------------------------------------------------------------------- #
# A partial outage must not read as healthy
# --------------------------------------------------------------------------- #
def test_degraded_logic_counts_any_failure_not_only_total():
    total = len(m.SYMBOLS)
    for failures in range(total + 1):
        degraded = failures > 0
        all_failed = failures == total
        assert degraded == (failures >= 1)
        # The old test would have been `all_failed`, silent for 1..total-1.
        if 0 < failures < total:
            assert degraded and not all_failed


def test_healthcheck_and_alert_use_degraded_not_all_failed():
    import inspect
    src = inspect.getsource(m.main)
    assert "ping_healthcheck(ok=not degraded)" in src
    assert "if degraded and streak == MAX_CONSECUTIVE_FAILURES" in src
    # Heartbeat must not be able to say "all good" while short on symbols.
    assert "only {len(summary)}/{len(SYMBOLS)} symbols reported" in src
