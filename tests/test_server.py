"""
Self-verifying tests for the Binance BTC Futures MCP server.

- Tool-output tests drive the *registered* tools through an in-process FastMCP
  Client (so decorator wiring, schemas and serialization are all exercised),
  with every outbound HTTP call mocked via respx — no live network.
- Reliability tests hit the shared _get() helper directly to prove retry,
  caching and error behavior.

Run:  pytest -q      (from the project root, inside the venv)
"""
import asyncio
from datetime import datetime, timezone

import httpx
import pytest
import respx
from fastmcp import Client

import server

# --------------------------------------------------------------------------- #
# Canned API payloads (shapes mirror the real endpoints)
# --------------------------------------------------------------------------- #
TICKER_24HR = {
    "symbol": "BTCUSDT", "lastPrice": "65000.0", "priceChange": "-400.0",
    "priceChangePercent": "-0.6", "highPrice": "66000.0", "lowPrice": "64000.0",
    "volume": "1000.0", "quoteVolume": "65000000.0",
}
PREMIUM_INDEX = {
    "symbol": "BTCUSDT", "lastFundingRate": "0.0001", "markPrice": "65010.0",
    "nextFundingTime": 1690000000000,
}
KLINES = [
    [1690000000000, "65000", "65100", "64900", "65050", "10.5",
     1690003599999, "0", 100, "0", "0", "0"],
    [1690003600000, "65050", "65200", "65000", "65180", "8.2",
     1690007199999, "0", 90, "0", "0", "0"],
]
DEPTH = {
    "lastUpdateId": 1,
    "bids": [["65000.0", "1.5"], ["64999.0", "2.0"]],
    "asks": [["65001.0", "1.0"], ["65002.0", "3.0"]],
}
FNG = {"data": [{"value": "33", "value_classification": "Fear",
                 "timestamp": "1690000000"}]}
CG_GLOBAL = {"data": {
    "total_market_cap": {"usd": 2.3e12},
    "total_volume": {"usd": 6.6e10},
    "market_cap_percentage": {"btc": 56.7},
    "market_cap_change_percentage_24h_usd": -0.5,
}}
CG_MARKETS = [
    {"symbol": "btc", "name": "Bitcoin", "current_price": 65000,
     "market_cap": 1.3e12, "price_change_percentage_24h": -0.6},
    {"symbol": "eth", "name": "Ethereum", "current_price": 1900,
     "market_cap": 2.3e11, "price_change_percentage_24h": 0.2},
]
OPEN_INTEREST = {"openInterest": "85000.5", "symbol": "BTCUSDT",
                 "time": 1690000000000}
OI_HIST = [
    {"symbol": "BTCUSDT", "sumOpenInterest": "80000.0",
     "sumOpenInterestValue": "5200000000", "timestamp": 1690000000000},
    {"symbol": "BTCUSDT", "sumOpenInterest": "84000.0",
     "sumOpenInterestValue": "5400000000", "timestamp": 1690014400000},
]
TOP_LS = [
    {"symbol": "BTCUSDT", "longShortRatio": "1.5", "longAccount": "0.60",
     "shortAccount": "0.40", "timestamp": 1690000000000},
    {"symbol": "BTCUSDT", "longShortRatio": "1.6", "longAccount": "0.61",
     "shortAccount": "0.39", "timestamp": 1690014400000},
]
GLOBAL_LS = [
    {"symbol": "BTCUSDT", "longShortRatio": "0.90", "longAccount": "0.47",
     "shortAccount": "0.53", "timestamp": 1690000000000},
    {"symbol": "BTCUSDT", "longShortRatio": "0.85", "longAccount": "0.46",
     "shortAccount": "0.54", "timestamp": 1690014400000},
]
TAKER = [
    {"buySellRatio": "1.2", "buyVol": "1000.0", "sellVol": "833.0",
     "timestamp": 1690000000000},
    {"buySellRatio": "1.25", "buyVol": "1100.0", "sellVol": "880.0",
     "timestamp": 1690014400000},
]

EXPECTED_TOOLS = {
    "get_btc_price", "get_funding_rate", "get_klines", "get_order_book",
    "get_fear_greed_index", "get_market_overview", "get_open_interest",
    "get_long_short_ratio", "get_taker_volume", "get_market_structure",
    "calculate_position_size", "get_session_clock", "get_economic_calendar",
    "get_top_down",
}


def _kline(i, o, h, l, c):
    """Build one raw Binance kline array (only indices 0-5 are consumed)."""
    t = 1690000000000 + i * 14400000
    return [t, str(o), str(h), str(l), str(c), "1.0",
            t + 14399999, "0", 1, "0", "0", "0"]


# 6-candle sequence (mirrors the structure unit test) for the integration check.
KLINES_STRUCT = [
    _kline(0, 9, 10, 8, 9),
    _kline(1, 11, 12, 9, 11),
    _kline(2, 8, 11, 7, 8),
    _kline(3, 13, 13, 9, 13),
    _kline(4, 11.5, 12, 11, 11.5),
    _kline(5, 6.5, 10, 6, 6.5),
]


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _clear_cache():
    server._CACHE.clear()
    yield
    server._CACHE.clear()


async def _noop(*_args, **_kwargs):
    return None


def _data(result):
    """Extract the structured dict from a FastMCP CallToolResult."""
    for attr in ("data", "structured_content"):
        val = getattr(result, attr, None)
        if val is not None:
            return val
    return result.content


async def _call(tool: str, args: dict | None = None):
    async with Client(server.mcp) as client:
        return _data(await client.call_tool(tool, args or {}))


def _binance(respx_mock, path, payload):
    return respx_mock.get(host="fapi.binance.com", path=path).mock(
        return_value=httpx.Response(200, json=payload)
    )


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #
async def test_all_tools_registered():
    async with Client(server.mcp) as client:
        tools = await client.list_tools()
    names = {t.name for t in tools}
    assert names == EXPECTED_TOOLS, f"got {sorted(names)}"


# --------------------------------------------------------------------------- #
# Tool output parsing
# --------------------------------------------------------------------------- #
async def test_get_btc_price(respx_mock):
    _binance(respx_mock, "/fapi/v1/ticker/24hr", TICKER_24HR)
    d = await _call("get_btc_price", {"symbol": "BTCUSDT"})
    assert d["symbol"] == "BTCUSDT"
    assert d["last_price"] == 65000.0
    assert d["price_change_pct_24h"] == -0.6


async def test_get_funding_rate(respx_mock):
    _binance(respx_mock, "/fapi/v1/premiumIndex", PREMIUM_INDEX)
    d = await _call("get_funding_rate")
    assert d["last_funding_rate_pct"] == 0.01
    assert d["interpretation"].startswith("Mildly positive")
    assert d["next_funding_time_utc"].endswith("+00:00")


async def test_get_klines(respx_mock):
    _binance(respx_mock, "/fapi/v1/klines", KLINES)
    d = await _call("get_klines", {"interval": "4h", "limit": 2})
    assert len(d["candles"]) == 2
    assert d["candles"][0]["open"] == 65000.0
    assert d["candles"][1]["close"] == 65180.0


async def test_get_klines_bad_interval_errors(respx_mock):
    with pytest.raises(Exception) as ei:
        await _call("get_klines", {"interval": "7h"})
    assert "Invalid interval" in str(ei.value)


async def test_get_order_book(respx_mock):
    _binance(respx_mock, "/fapi/v1/depth", DEPTH)
    d = await _call("get_order_book", {"limit": 5})
    assert d["best_bid"] == 65000.0
    assert d["best_ask"] == 65001.0
    assert d["spread"] == 1.0


async def test_get_fear_greed_index(respx_mock):
    respx_mock.get(host="api.alternative.me", path="/fng/").mock(
        return_value=httpx.Response(200, json=FNG)
    )
    d = await _call("get_fear_greed_index")
    assert d["value"] == 33
    assert d["classification"] == "Fear"


async def test_get_market_overview(respx_mock):
    respx_mock.get(host="api.coingecko.com", path="/api/v3/global").mock(
        return_value=httpx.Response(200, json=CG_GLOBAL)
    )
    respx_mock.get(host="api.coingecko.com", path="/api/v3/coins/markets").mock(
        return_value=httpx.Response(200, json=CG_MARKETS)
    )
    d = await _call("get_market_overview", {"top_n": 5})
    assert d["btc_dominance_pct"] == 56.7
    assert d["top_coins"][0]["symbol"] == "BTC"
    assert d["top_coins"][1]["name"] == "Ethereum"


async def test_get_open_interest(respx_mock):
    _binance(respx_mock, "/fapi/v1/openInterest", OPEN_INTEREST)
    _binance(respx_mock, "/futures/data/openInterestHist", OI_HIST)
    d = await _call("get_open_interest", {"period": "4h", "limit": 30})
    assert d["current_open_interest"] == 85000.5
    assert d["window_change_pct"] == 5.0  # (84000-80000)/80000*100
    assert len(d["history"]) == 2
    assert "up 5.0%" in d["interpretation"]


async def test_get_long_short_ratio(respx_mock):
    _binance(respx_mock, "/futures/data/topLongShortPositionRatio", TOP_LS)
    _binance(respx_mock, "/futures/data/globalLongShortAccountRatio", GLOBAL_LS)
    d = await _call("get_long_short_ratio", {"period": "4h"})
    # latest = max timestamp
    assert d["top_trader_position_long_short_ratio"] == 1.6
    assert d["global_account_long_short_ratio"] == 0.85
    assert "Top traders net long" in d["interpretation"]
    assert "crowd net short" in d["interpretation"]


async def test_get_taker_volume(respx_mock):
    _binance(respx_mock, "/futures/data/takerlongshortRatio", TAKER)
    d = await _call("get_taker_volume", {"period": "4h"})
    assert d["taker_buy_sell_ratio"] == 1.25
    assert d["taker_buy_volume"] == 1100.0
    assert d["interpretation"].startswith("Net aggressive buying")


async def test_bad_period_errors(respx_mock):
    with pytest.raises(Exception) as ei:
        await _call("get_open_interest", {"period": "8h"})  # 8h invalid for data
    assert "Invalid period" in str(ei.value)


# --------------------------------------------------------------------------- #
# Reliability: retry / cache / error (exercised on _get directly)
# --------------------------------------------------------------------------- #
async def test_get_retries_on_429_then_succeeds(respx_mock, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _noop)  # don't actually wait
    route = respx_mock.get(host="fapi.binance.com", path="/fapi/v1/ticker/24hr").mock(
        side_effect=[httpx.Response(429), httpx.Response(200, json={"ok": True})]
    )
    data = await server._get(
        server.BINANCE_BASE_URL, "/fapi/v1/ticker/24hr", {"symbol": "BTCUSDT"}
    )
    assert data == {"ok": True}
    assert route.call_count == 2


async def test_get_caches_within_ttl(respx_mock):
    route = respx_mock.get(host="api.alternative.me", path="/fng/").mock(
        return_value=httpx.Response(200, json=FNG)
    )
    a = await server._get(server.FNG_BASE_URL, "/fng/", {"limit": 1}, cache_ttl=300)
    b = await server._get(server.FNG_BASE_URL, "/fng/", {"limit": 1}, cache_ttl=300)
    assert a == b
    assert route.call_count == 1  # second served from cache


async def test_get_raises_runtimeerror_on_400(respx_mock):
    route = respx_mock.get(host="fapi.binance.com", path="/fapi/v1/ticker/24hr").mock(
        return_value=httpx.Response(400, text="bad symbol")
    )
    with pytest.raises(RuntimeError) as ei:
        await server._get(
            server.BINANCE_BASE_URL, "/fapi/v1/ticker/24hr", {"symbol": "NOPE"}
        )
    assert "400" in str(ei.value)
    assert route.call_count == 1  # 400 is not retried


# --------------------------------------------------------------------------- #
# Market structure tool (integration — structure.py logic is unit-tested
# separately in test_structure.py)
# --------------------------------------------------------------------------- #
async def test_get_market_structure(respx_mock):
    _binance(respx_mock, "/fapi/v1/klines", KLINES_STRUCT)
    d = await _call("get_market_structure",
                    {"interval": "4h", "limit": 100, "swing_strength": 1})
    assert d["symbol"] == "BTCUSDT"
    assert d["interval"] == "4h"
    assert "error" not in d
    assert d["trend"] in {"up", "down", "none"}
    for section in ("recent_structure_events", "swings", "premium_discount",
                    "fair_value_gaps", "order_blocks", "liquidity", "atr"):
        assert section in d


async def test_get_top_down(respx_mock):
    # Same klines payload backs all three timeframe fetches (respx matches by
    # host+path only) — enough to prove the fan-out/merge wiring is correct.
    _binance(respx_mock, "/fapi/v1/klines", KLINES_STRUCT)
    d = await _call("get_top_down", {"swing_strength": 1})
    assert d["symbol"] == "BTCUSDT"
    assert "note" in d
    for tf, interval in (("daily", "1d"), ("h4", "4h"), ("h1", "1h")):
        assert d[tf]["interval"] == interval
        assert "error" not in d[tf]
        assert d[tf]["trend"] in {"up", "down", "none"}
        assert "atr" in d[tf] and "premium_discount" in d[tf]


# --------------------------------------------------------------------------- #
# Position-size calculator (risk.py logic is unit-tested in test_risk.py; these
# exercise the tool wiring) and session clock
# --------------------------------------------------------------------------- #
async def test_calculate_position_size_long():
    d = await _call("calculate_position_size", {
        "entry": 65000, "stop_loss": 64675, "capital": 200,
        "risk_pct": 1, "leverage": 3,
    })
    assert d["direction"] == "long"
    assert d["risk_amount_usd"] == 2.0
    assert d["stop_distance_pct"] == 0.5
    assert d["position"]["notional_usd"] == 400.0
    assert d["plan_target_prices"]["1:2"] == 65650.0
    assert d["passes_plan"] is True


async def test_calculate_position_size_flags_undersized():
    # $200 capital, 3% stop on 1% risk ($2) -> $66.67 notional, below the
    # $100 Binance minimum (an exchange rule, not tied to account size).
    d = await _call("calculate_position_size", {
        "entry": 65000, "stop_loss": 63050, "capital": 200, "risk_pct": 1,
    })
    assert d["passes_plan"] is False
    assert any("minimum" in w.lower() for w in d["warnings"])


def test_session_state_in_london():
    st = server._session_state(datetime(2026, 7, 23, 8, 0, tzinfo=timezone.utc))
    assert st["in_kill_zone"] is True
    assert st["active_kill_zone"] == "London"
    assert st["minutes_left_in_zone"] == 60


def test_session_state_between_zones():
    st = server._session_state(datetime(2026, 7, 23, 10, 30, tzinfo=timezone.utc))
    assert st["in_kill_zone"] is False
    assert st["next_kill_zone"] == "New York"
    assert st["minutes_until_next"] == 150  # 13:00 - 10:30


def test_session_state_after_ny_rolls_to_tomorrow():
    st = server._session_state(datetime(2026, 7, 23, 16, 0, tzinfo=timezone.utc))
    assert st["in_kill_zone"] is False
    assert st["next_kill_zone"] == "London"
    assert st["minutes_until_next"] == (24 * 60 - 16 * 60) + 7 * 60  # 900


async def test_get_session_clock_shape():
    d = await _call("get_session_clock")
    assert isinstance(d["in_kill_zone"], bool)
    for key in ("utc_time", "utc_weekday", "kill_zones_utc", "note"):
        assert key in d


# Far-future dates so the events are always "upcoming" regardless of run time.
FF_CALENDAR = [
    {"title": "Core CPI m/m", "country": "USD",
     "date": "2099-01-15T13:30:00+00:00", "impact": "High",
     "forecast": "0.3%", "previous": "0.2%"},
    {"title": "ECB Press Conference", "country": "EUR",
     "date": "2099-01-15T12:45:00+00:00", "impact": "High",
     "forecast": "", "previous": ""},
    {"title": "Wholesale Inventories m/m", "country": "USD",
     "date": "2099-01-15T15:00:00+00:00", "impact": "Low",
     "forecast": "", "previous": ""},
]


async def test_get_economic_calendar(respx_mock):
    respx_mock.get(host="nfs.faireconomy.media",
                   path="/ff_calendar_thisweek.json").mock(
        return_value=httpx.Response(200, json=FF_CALENDAR)
    )
    d = await _call("get_economic_calendar", {"currencies": "USD",
                                              "min_impact": "High"})
    # Only the High-impact USD event passes the default filter.
    titles = [e["title"] for e in d["upcoming"]]
    assert titles == ["Core CPI m/m"]
    assert d["next_event"]["title"] == "Core CPI m/m"
    assert d["blackout_active"] is False
    assert d["news_clear"] is True
    assert d["filter"]["currencies"] == ["USD"]
