"""Tests for standalone_monitor's setup-gate logic.

These cover the parts that decide whether Chamara's phone buzzes, which is
where a silent bug is most expensive: a monitor that never fires looks
identical to a quiet market.
"""
from datetime import datetime, timedelta, timezone

import standalone_monitor as m


# --- zone width filter -----------------------------------------------------

def test_wide_enough_rejects_one_tick_gaps():
    # The real case that broke this: a $10.30 band on a $79k asset. Price can
    # never be meaningfully inside that when the hourly check happens.
    assert not m.wide_enough(77800.0, 77789.7, 79215.3)


def test_wide_enough_accepts_a_real_zone():
    # A genuine 4H gap, ~1.5% wide.
    assert m.wide_enough(74482.8, 73399.7, 79215.3)


def test_pick_demand_zone_skips_micro_gap_for_real_one():
    struct = {
        "order_blocks": {"open": []},
        "fair_value_gaps": {"open": [
            # Nearest to price, but far too thin to trade.
            {"type": "bullish", "top": 77800.0, "bottom": 77789.7},
            # Further away, but an actual zone.
            {"type": "bullish", "top": 74482.8, "bottom": 73399.7},
        ]},
    }
    zone = m.pick_demand_zone(struct, 79215.3)
    assert zone == {"top": 74482.8, "bottom": 73399.7}


def test_pick_demand_zone_returns_none_when_all_too_thin():
    struct = {
        "order_blocks": {"open": []},
        "fair_value_gaps": {"open": [
            {"type": "bullish", "top": 77800.0, "bottom": 77789.7},
        ]},
    }
    assert m.pick_demand_zone(struct, 79215.3) is None


# --- structure-event regression guard --------------------------------------

def test_is_newer_accepts_a_later_event():
    assert m.is_newer("2026-08-24T12:00:00+00:00", "2026-08-23T04:00:00+00:00")


def test_is_newer_rejects_an_earlier_event():
    # The observed bug: the engine revised "latest" backwards a day and the
    # old != comparison re-alerted on an event already seen.
    assert not m.is_newer("2026-08-23T04:00:00+00:00", "2026-08-24T12:00:00+00:00")


def test_is_newer_rejects_the_same_event():
    assert not m.is_newer("2026-08-24T12:00:00+00:00", "2026-08-24T12:00:00+00:00")


def test_is_newer_accepts_first_ever_event():
    assert m.is_newer("2026-08-24T12:00:00+00:00", None)


# --- kill zones ------------------------------------------------------------

def test_kill_zone_detects_london():
    assert m.active_kill_zone(datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)) == "London"


def test_kill_zone_detects_new_york():
    assert m.active_kill_zone(datetime(2026, 8, 24, 14, 0, tzinfo=timezone.utc)) == "New York"


def test_kill_zone_excludes_weekends():
    # 2026-08-23 is a Sunday, 08:00 would otherwise be inside London.
    assert m.active_kill_zone(datetime(2026, 8, 23, 8, 0, tzinfo=timezone.utc)) is None


def test_kill_zone_end_is_exclusive():
    assert m.active_kill_zone(datetime(2026, 8, 24, 15, 0, tzinfo=timezone.utc)) is None


# --- the full gate ---------------------------------------------------------

def _result(price, trend_1d="up", trend_4h="up", zone=None, ev_dir="bullish",
            ev_age_hours=1):
    now = datetime(2026, 8, 24, 14, 0, tzinfo=timezone.utc)
    ev_time = (now - timedelta(hours=ev_age_hours)).isoformat()
    return {
        "price": price,
        "trend_1d": trend_1d,
        "trend_4h": trend_4h,
        "demand": zone,
        "supply": None,
        "event_1h": {"direction": ev_dir, "time": ev_time},
    }, now


ZONE = {"top": 74482.8, "bottom": 73399.7}


def test_setup_ready_when_every_gate_passes():
    # Entry near the top of the zone, so the stop is wide enough that fees
    # stay a small fraction of the risk.
    r, now = _result(74400.0, zone=ZONE)
    setup = m.evaluate_setup(r, now)
    assert setup["ready"], setup["gates"]
    assert setup["direction"] == "long"
    assert setup["entry"] == 74400.0
    # Stop sits just under the zone that is supposed to hold.
    assert setup["stop"] < ZONE["bottom"]


def test_no_setup_when_fees_eat_the_risk():
    # Entering at the zone floor makes the stop very tight; on a $200 account
    # the round trip then costs a large slice of the $4 risked, which demands
    # a far higher win rate than the stated R:R implies.
    r, now = _result(74000.0, zone=ZONE)
    setup = m.evaluate_setup(r, now)
    assert not setup["ready"]
    assert setup["gates"]["fee_ok"] is False
    assert setup["fee_pct"] > m.MAX_FEE_PCT_OF_RISK


def test_no_setup_during_a_news_blackout():
    r, now = _result(74400.0, zone=ZONE)
    setup = m.evaluate_setup(r, now, news_blocked=True)
    assert not setup["ready"]
    assert setup["gates"]["news_clear"] is False


def test_news_gate_passes_when_calendar_is_clear():
    r, now = _result(74400.0, zone=ZONE)
    setup = m.evaluate_setup(r, now, news_blocked=False)
    assert setup["gates"]["news_clear"] is True


def test_no_setup_when_price_outside_zone():
    r, now = _result(79000.0, zone=ZONE)
    setup = m.evaluate_setup(r, now)
    assert not setup["ready"]
    assert setup["gates"]["in_zone"] is False


def test_no_setup_when_timeframes_disagree():
    r, now = _result(74000.0, trend_4h="down", zone=ZONE)
    setup = m.evaluate_setup(r, now)
    assert not setup["ready"]
    assert setup["gates"]["aligned"] is False


def test_no_setup_when_trigger_points_the_wrong_way():
    r, now = _result(74000.0, zone=ZONE, ev_dir="bearish")
    setup = m.evaluate_setup(r, now)
    assert not setup["ready"]
    assert setup["gates"]["trigger"] is False


def test_no_setup_when_trigger_is_stale():
    r, now = _result(74000.0, zone=ZONE, ev_age_hours=9)
    setup = m.evaluate_setup(r, now)
    assert not setup["ready"]
    assert setup["gates"]["trigger"] is False


def test_no_setup_outside_kill_zone():
    r, _ = _result(74000.0, zone=ZONE)
    off_hours = datetime(2026, 8, 24, 3, 0, tzinfo=timezone.utc)
    setup = m.evaluate_setup(r, off_hours)
    assert not setup["ready"]
    assert setup["gates"]["kill_zone"] is False


def test_alert_message_carries_the_numbers_that_matter():
    r, now = _result(74400.0, zone=ZONE)
    setup = m.evaluate_setup(r, now)
    msg = m.format_setup_alert("BTCUSDT", r, setup, now)
    for expected in ("SETUP READY", "BTCUSDT", "LONG", "Entry", "Stop",
                     "Size", "Targets", "breakeven", "NOT a recommendation"):
        assert expected in msg


# --- open position management ---------------------------------------------

def _pos(direction="long", entry=77200.0, stop=76780.0, **flags):
    p = {"symbol": "BTCUSDT", "direction": direction, "entry": entry,
         "stop": stop}
    p.update(flags)
    return p


def _tick(price, ev_dir=None, ev_age_hours=1):
    now = datetime(2026, 8, 24, 14, 0, tzinfo=timezone.utc)
    r = {"price": price, "event_1h": None}
    if ev_dir:
        r["event_1h"] = {
            "direction": ev_dir, "type": "CHoCH", "level": price,
            "time": (now - timedelta(hours=ev_age_hours)).isoformat(),
        }
    return r, now


def test_position_quiet_before_any_milestone():
    pos = _pos()
    r, now = _tick(77300.0)          # only ~0.24R
    assert m.manage_position(pos, r, now) == []


def test_position_flags_breakeven_at_1r():
    pos = _pos()
    r, now = _tick(77620.0)          # exactly 1R
    msgs = m.manage_position(pos, r, now)
    assert any("BREAKEVEN" in x for x in msgs)
    assert pos["breakeven_done"]


def test_position_breakeven_announced_only_once():
    pos = _pos(breakeven_done=True)
    r, now = _tick(77620.0)
    assert not any("BREAKEVEN" in x for x in m.manage_position(pos, r, now))


def test_position_flags_first_target_at_2r():
    pos = _pos(breakeven_done=True)
    r, now = _tick(78040.0)          # 2R
    msgs = m.manage_position(pos, r, now)
    assert any("1:2 TARGET" in x for x in msgs)
    assert pos["partial_done"]


def test_position_reports_stop_hit_and_closes():
    pos = _pos()
    r, now = _tick(76770.0)          # through the stop
    msgs = m.manage_position(pos, r, now)
    assert any("STOP HIT" in x for x in msgs)
    assert pos["closed"]


def test_position_warns_on_opposing_1h_signal():
    pos = _pos()
    r, now = _tick(77300.0, ev_dir="bearish")
    msgs = m.manage_position(pos, r, now)
    assert any("OPPOSING SIGNAL" in x for x in msgs)


def test_position_ignores_a_signal_in_its_favour():
    pos = _pos()
    r, now = _tick(77300.0, ev_dir="bullish")
    assert not any("OPPOSING" in x for x in m.manage_position(pos, r, now))


def test_position_opposing_warning_rearms_after_structure_turns_back():
    pos = _pos(opposing_flagged=True)
    favourable, now = _tick(77300.0, ev_dir="bullish")
    m.manage_position(pos, favourable, now)
    assert pos["opposing_flagged"] is False


def test_short_position_milestones_run_the_other_way():
    pos = _pos(direction="short", entry=77200.0, stop=77620.0)
    r, now = _tick(76780.0)          # 1R in profit for a short
    msgs = m.manage_position(pos, r, now)
    assert any("BREAKEVEN" in x for x in msgs)
