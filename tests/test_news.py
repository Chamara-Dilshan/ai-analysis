"""
Deterministic unit tests for news.py — economic-calendar filtering and the
news-blackout logic. Fixed `now`, synthetic feed rows, no I/O.
"""
from datetime import datetime, timedelta, timezone

import news as N

NOW = datetime(2026, 7, 23, 13, 0, tzinfo=timezone.utc)


def ev(title, currency, impact, offset_min):
    """Build one feed row whose time is `offset_min` from NOW."""
    dt = NOW + timedelta(minutes=offset_min)
    return {"title": title, "country": currency, "impact": impact,
            "date": dt.isoformat(), "forecast": "", "previous": ""}


def test_blackout_high_impact_soon():
    out = N.analyze([ev("FOMC", "USD", "High", 15)], NOW)
    assert out["blackout_active"] is True
    assert out["news_clear"] is False
    assert out["note"].startswith("⛔")
    assert out["next_event"]["title"] == "FOMC"
    assert out["next_event"]["minutes_until"] == 15


def test_blackout_recent_release():
    out = N.analyze([ev("CPI", "USD", "High", -20)], NOW)
    assert out["blackout_active"] is True
    assert out["upcoming"][0]["minutes_until"] == -20  # still shown (trailing window)
    assert out["next_event"] is None                   # nothing strictly ahead


def test_warns_when_event_within_hour():
    out = N.analyze([ev("NFP", "USD", "High", 45)], NOW)
    assert out["blackout_active"] is False
    assert out["note"].startswith("⚠️")
    assert out["next_event"]["minutes_until"] == 45


def test_clear_when_next_event_far():
    out = N.analyze([ev("PPI", "USD", "High", 180)], NOW)
    assert out["blackout_active"] is False
    assert out["note"].startswith("Clear")
    assert out["next_event"]["minutes_until"] == 180


def test_currency_filter_excludes_other():
    out = N.analyze([ev("ECB Press Conf", "EUR", "High", 15)], NOW,
                    currencies=("USD",))
    assert out["blackout_active"] is False
    assert out["next_event"] is None
    assert out["upcoming"] == []
    assert "No high-impact USD" in out["note"]


def test_impact_filter():
    rows = [ev("Retail Sales", "USD", "Medium", 15)]
    assert N.analyze(rows, NOW, min_impact="High")["blackout_active"] is False
    assert N.analyze(rows, NOW, min_impact="Medium")["blackout_active"] is True


def test_multiple_currencies_included_and_sorted():
    rows = [ev("CPI", "USD", "High", 200), ev("ECB", "EUR", "High", 100)]
    out = N.analyze(rows, NOW, currencies=("USD", "EUR"))
    assert [e["title"] for e in out["upcoming"]] == ["ECB", "CPI"]  # by time
    assert out["next_event"]["title"] == "ECB"


def test_custom_within_minutes():
    rows = [ev("CPI", "USD", "High", 45)]
    assert N.analyze(rows, NOW, within_minutes=30)["blackout_active"] is False
    assert N.analyze(rows, NOW, within_minutes=60)["blackout_active"] is True


def test_skips_unparseable_dates():
    rows = [
        {"title": "Bad", "country": "USD", "impact": "High", "date": "not-a-date"},
        ev("Good", "USD", "High", 120),
    ]
    out = N.analyze(rows, NOW)
    assert [e["title"] for e in out["upcoming"]] == ["Good"]


def test_non_list_raw_is_safe():
    out = N.analyze({"error": "rate limited"}, NOW)
    assert out["blackout_active"] is False
    assert out["upcoming"] == []
    assert out["next_event"] is None
