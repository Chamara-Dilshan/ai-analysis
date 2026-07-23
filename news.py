"""
Economic-calendar parsing for the "no high-impact news within 30 min" rule
(Docs/02_trading_plan.md, checklist item 7).

Input shape mirrors the ForexFactory weekly calendar JSON (faireconomy.media):
each event is {title, country (currency code), date (ISO 8601 w/ tz offset),
impact, forecast, previous}. Pure logic lives here; the HTTP fetch is in
server.py. Unit-tested in tests/test_news.py.

These are scheduled event times, not signals. The point is only to keep Chamara
out of the market around violent, unpredictable releases (FOMC, CPI, NFP, PCE).
"""
from datetime import datetime, timezone
from typing import Any, Optional

# High-impact is what the plan cares about; keep the full ladder for filtering.
IMPACT_RANK = {"high": 3, "medium": 2, "low": 1, "holiday": 0, "": 0}


def _rank(impact: str) -> int:
    return IMPACT_RANK.get((impact or "").strip().lower(), 0)


def _parse_events(raw: Any) -> list:
    """Normalize raw feed rows to {title, currency, impact, time_utc, forecast,
    previous}; silently skip rows without a parseable date."""
    events: list = []
    for e in raw if isinstance(raw, list) else []:
        if not isinstance(e, dict):
            continue
        try:
            dt = datetime.fromisoformat(e.get("date"))
        except (TypeError, ValueError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        events.append({
            "title": (e.get("title") or "").strip(),
            "currency": (e.get("country") or "").strip().upper(),
            "impact": (e.get("impact") or "").strip(),
            "time_utc": dt.astimezone(timezone.utc),
            "forecast": e.get("forecast") or "",
            "previous": e.get("previous") or "",
        })
    return events


def analyze(
    raw: Any,
    now: datetime,
    currencies=("USD",),
    min_impact: str = "High",
    within_minutes: int = 30,
    max_events: int = 8,
) -> dict:
    """
    Filter the calendar to the currencies and impact the plan cares about, then
    answer: is a qualifying release inside the +/- `within_minutes` blackout of
    `now`? Returns blackout status, the next event, and an upcoming list.
    """
    within_minutes = max(1, min(int(within_minutes), 1440))
    wanted = {c.strip().upper() for c in currencies if c and c.strip()} or {"USD"}
    # Unknown/garbage min_impact defaults to High (stricter, safer).
    min_rank = IMPACT_RANK.get((min_impact or "").strip().lower(), 3)

    filtered = [
        ev for ev in _parse_events(raw)
        if _rank(ev["impact"]) >= min_rank and ev["currency"] in wanted
    ]
    filtered.sort(key=lambda ev: ev["time_utc"])

    def mins(ev) -> int:
        return round((ev["time_utc"] - now).total_seconds() / 60.0)

    def fmt(ev) -> dict:
        return {
            "title": ev["title"],
            "currency": ev["currency"],
            "impact": ev["impact"],
            "time_utc": ev["time_utc"].strftime("%Y-%m-%d %H:%M UTC"),
            "minutes_until": mins(ev),
            "forecast": ev["forecast"],
            "previous": ev["previous"],
        }

    blackout_events = [ev for ev in filtered if abs(mins(ev)) <= within_minutes]
    blackout = bool(blackout_events)

    # Upcoming = still-future plus just-released (inside the trailing window), capped.
    upcoming = [ev for ev in filtered if mins(ev) >= -within_minutes][:max_events]
    future = [ev for ev in filtered if mins(ev) >= 0]
    next_ev: Optional[dict] = future[0] if future else None

    if blackout:
        nearest = min(blackout_events, key=lambda ev: abs(mins(ev)))
        m = mins(nearest)
        when = f"in {m} min" if m >= 0 else f"{abs(m)} min ago"
        note = (
            f"⛔ High-impact {nearest['currency']} event {when}: {nearest['title']}. "
            "Checklist item 7 = NO — do not enter now."
        )
    elif next_ev is not None and mins(next_ev) <= 60:
        note = (
            f"⚠️ {next_ev['title']} ({next_ev['currency']}) in {mins(next_ev)} min. "
            "No new entries 30 min before/after — plan around it."
        )
    elif next_ev is not None:
        h, mm = divmod(mins(next_ev), 60)
        note = (
            f"Clear on the news front. Next high-impact: {next_ev['title']} "
            f"({next_ev['currency']}) in {h}h {mm:02d}m."
        )
    else:
        note = (
            f"No high-impact {'/'.join(sorted(wanted))} events left in this week's "
            "calendar. News-clear — but always double-check."
        )

    return {
        "now_utc": now.strftime("%Y-%m-%d %H:%M UTC"),
        "filter": {
            "currencies": sorted(wanted),
            "min_impact": min_impact,
            "within_minutes": within_minutes,
        },
        "blackout_active": blackout,
        "news_clear": not blackout,
        "next_event": fmt(next_ev) if next_ev else None,
        "upcoming": [fmt(ev) for ev in upcoming],
        "source": (
            "ForexFactory weekly calendar via faireconomy.media "
            "(free public mirror, no API key; unofficial)."
        ),
        "note": note,
        "disclaimer": (
            "Scheduled news times only — not a signal or advice. High-impact "
            "releases move price violently; the plan bans trading 30 min "
            "before/after. Verify against an official economic calendar."
        ),
    }
