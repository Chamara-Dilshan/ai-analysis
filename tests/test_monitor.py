"""Tests for the standalone monitor's setup-gate engine.

These cover the logic that decides whether Chamara's phone buzzes. Getting it
wrong in either direction is expensive: a false alert costs trust, a missed
one costs the setup he was waiting for.
"""
from datetime import datetime, timezone

import pytest

import standalone_monitor as m


MON_NY = datetime(2026, 8, 24, 14, 0, tzinfo=timezone.utc)   # Monday, NY zone
MON_LDN = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)   # Monday, London
MON_QUIET = datetime(2026, 8, 24, 17, 0, tzinfo=timezone.utc)  # Monday, no zone
SUN_LDN = datetime(2026, 8, 23, 8, 0, tzinfo=timezone.utc)   # Sunday, London hrs


def _result(**overrides):
    """A symbol result with every gate passing, so each test can break
    exactly one thing."""
    base = {
        "price": 74000.0,
        "funding_pct": 0.01,
        "trend_1d": "up",
        "trend_4h": "up",
        "trend_1h": "up",
        "demand": {"top": 74482.8, "bottom": 73399.7},
        "supply": {"top": 79000.0, "bottom": 78500.0},
        "event_1h": {
            "type": "CHoCH",
            "direction": "bullish",
            "time": "2026-08-24T13:00:00+00:00",
            "level": 73800.0,
        },
    }
    base.update(overrides)
    return base


# --- is_newer: the regression that caused a duplicate alert -----------------

def test_is_newer_blocks_backwards_revision():
    """The swing engine can revise its latest event backwards when a
    provisional swing fails to confirm. That is not news."""
    assert m.is_newer("2026-08-23T04:00:00+00:00",
                      "2026-08-24T12:00:00+00:00") is False


def test_is_newer_allows_genuinely_newer():
    assert m.is_newer("2026-08-24T12:00:00+00:00",
                      "2026-08-23T04:00:00+00:00") is True


def test_is_newer_first_event_counts():
    assert m.is_newer("2026-08-24T12:00:00+00:00", None) is True


def test_is_newer_identical_is_not_newer():
    t = "2026-08-24T12:00:00+00:00"
    assert m.is_newer(t, t) is False


def test_is_newer_handles_unparseable_without_raising():
    """If the timestamps cannot be parsed there is no ordering to reason
    about, so it falls back to plain inequality — the old behaviour, applied
    only where nothing better is available."""
    assert m.is_newer("not-a-date", "not-a-date") is False
    assert m.is_newer("not-a-date", "different-junk") is True


# --- kill zones -------------------------------------------------------------

@pytest.mark.parametrize("when,expected", [
    (MON_LDN, "London"),
    (MON_NY, "New York"),
    (MON_QUIET, None),
    (SUN_LDN, None),                                             # weekend
    (datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc), None),    # end exclusive
    (datetime(2026, 8, 24, 7, 0, tzinfo=timezone.utc), "London"),  # start incl.
])
def test_active_kill_zone(when, expected):
    assert m.active_kill_zone(when) == expected


# --- evaluate_setup ---------------------------------------------------------

def test_all_gates_pass_produces_entry_and_stop():
    s = m.evaluate_setup(_result(), MON_NY)
    assert s["ready"] is True
    assert s["direction"] == "long"
    assert s["entry"] == 74000.0
    # Stop sits just beyond the zone that is meant to hold.
    assert s["stop"] < 73399.7


def test_misaligned_timeframes_block_the_setup():
    s = m.evaluate_setup(_result(trend_4h="down"), MON_NY)
    assert s["ready"] is False
    assert s["gates"]["aligned"] is False


def test_price_outside_zone_blocks_the_setup():
    s = m.evaluate_setup(_result(price=79000.0), MON_NY)
    assert s["ready"] is False
    assert s["gates"]["in_zone"] is False


def test_wrong_direction_trigger_blocks_the_setup():
    """A bearish 1H turn does not open a long, however good the zone."""
    ev = dict(_result()["event_1h"], direction="bearish")
    s = m.evaluate_setup(_result(event_1h=ev), MON_NY)
    assert s["ready"] is False
    assert s["gates"]["trigger"] is False


def test_stale_trigger_blocks_the_setup():
    """An old 1H event is history, not a trigger."""
    ev = dict(_result()["event_1h"], time="2026-08-24T02:00:00+00:00")
    s = m.evaluate_setup(_result(event_1h=ev), MON_NY)
    assert s["gates"]["trigger"] is False


def test_outside_kill_zone_blocks_the_setup():
    s = m.evaluate_setup(_result(), MON_QUIET)
    assert s["ready"] is False
    assert s["gates"]["kill_zone"] is False


def test_weekend_blocks_even_a_perfect_setup():
    s = m.evaluate_setup(_result(), SUN_LDN)
    assert s["ready"] is False
    assert s["gates"]["kill_zone"] is False


def test_short_setup_uses_supply_zone():
    s = m.evaluate_setup(
        _result(
            trend_1d="down", trend_4h="down", price=78700.0,
            event_1h={"type": "BOS", "direction": "bearish",
                      "time": "2026-08-24T13:00:00+00:00", "level": 78900.0},
        ),
        MON_NY,
    )
    assert s["ready"] is True
    assert s["direction"] == "short"
    assert s["stop"] > 79000.0    # stop above the supply zone


# --- alert formatting -------------------------------------------------------

def test_setup_alert_contains_the_numbers_that_matter():
    r = _result()
    s = m.evaluate_setup(r, MON_NY)
    msg = m.format_setup_alert("BTCUSDT", r, s, MON_NY)
    for fragment in ("SETUP READY", "BTCUSDT", "LONG", "Entry", "Stop",
                     "Size", "margin", "liq", "1:2", "breakeven",
                     "NOT a recommendation"):
        assert fragment in msg


def test_setup_alert_survives_broken_math():
    """A sizing failure must still produce a usable message rather than
    killing the run — the alert matters more than the arithmetic."""
    r = _result()
    s = m.evaluate_setup(r, MON_NY)
    s["stop"] = s["entry"]        # zero stop distance: risk.position_size raises
    msg = m.format_setup_alert("BTCUSDT", r, s, MON_NY)
    assert "SETUP READY" in msg and "math failed" in msg
