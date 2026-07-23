"""
Binance BTC Futures MCP Server
--------------------------------
Provides live market data to Claude for Elliott Wave + ICT + SMC analysis:
- BTCUSDT Perpetual price, funding rate, order book, OHLCV (Binance)
- Open interest, long/short ratios, taker buy/sell delta (Binance futures data)
- ICT/SMC market structure computed from candles (swings, BOS/CHoCH, FVGs,
  order blocks, liquidity, premium/discount) — see structure.py
- Crypto Fear & Greed Index (alternative.me)
- Total market cap, BTC dominance, top altcoins (CoinGecko)
- High-impact economic calendar for news blackouts (ForexFactory feed) — see news.py

Plus two pure-local (no-network) helpers: a plan position-size/risk calculator
(risk.py) and a UTC kill-zone session clock.

All endpoints used are free public APIs — no API key needed for any of it.
Docs:
- Binance: https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data
- Fear & Greed: https://alternative.me/crypto/fear-and-greed-index/
- CoinGecko: https://docs.coingecko.com/reference/introduction
- Economic calendar: ForexFactory weekly JSON via faireconomy.media (unofficial mirror)

Reliability: every outbound call retries on 429/5xx and transient network
errors with exponential backoff (honoring Retry-After), and slow-changing
endpoints are cached for a few seconds to avoid rate limits (esp. CoinGecko).

Run locally via stdio (Claude Desktop). See README.md for setup.
"""

import asyncio
import random
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from fastmcp import FastMCP

import news
import risk
import structure

BINANCE_BASE_URL = "https://fapi.binance.com"
FNG_BASE_URL = "https://api.alternative.me"
COINGECKO_BASE_URL = "https://api.coingecko.com/api/v3"
# ForexFactory weekly economic calendar (free public mirror, no API key; unofficial).
FF_BASE_URL = "https://nfs.faireconomy.media"
SYMBOL_DEFAULT = "BTCUSDT"

# Sent on every request — CoinGecko in particular throttles UA-less traffic harder.
DEFAULT_HEADERS = {"User-Agent": "binance-btc-futures-mcp/1.2 (+local stdio)"}

# Transient statuses worth retrying (429 = rate limit, 5xx = server-side).
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_RETRIES = 3
REQUEST_TIMEOUT = 10.0

# Valid candle intervals for /fapi/v1/klines.
KLINE_INTERVALS = {
    "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h",
    "8h", "12h", "1d", "3d", "1w", "1M",
}
# Valid "period" values for the /futures/data/* statistics endpoints
# (note: NOT the same set as kline intervals — no 1m/3m/8h/3d/1w/1M).
DATA_PERIODS = {"5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"}

# Tiny in-process response cache: key -> (expiry_monotonic, data).
_CACHE: dict[tuple, tuple[float, Any]] = {}

mcp = FastMCP(
    name="binance-btc-futures",
    instructions=(
        "Tools for crypto market analysis: BTCUSDT Perpetual Futures data from "
        "Binance (price, funding rate, order book, OHLCV candles, open interest, "
        "long/short ratios, taker buy/sell delta), computed ICT/SMC market "
        "structure (swings, BOS/CHoCH, FVGs, order blocks, liquidity, "
        "premium/discount), the Crypto Fear & Greed Index, and a broader market "
        "overview (total market cap, BTC dominance, top altcoins) from CoinGecko. "
        "Also a position-size/risk calculator that enforces the $100 plan (2% "
        "risk, 5x leverage, 1:2 R:R, Binance minimums), a UTC session clock "
        "for London/New York kill-zone timing, and a high-impact economic "
        "calendar for news blackouts (no trading 30 min around FOMC/CPI/NFP etc.), "
        "and a get_top_down tool that fetches Daily+4H+1H structure in one call "
        "for the plan's top-down framework. "
        "Use these together for full Elliott Wave / ICT / SMC context, not just "
        "BTC futures in isolation."
    ),
)

# Reused annotation set — the market-data lookups are read-only and hit the network.
_RO = {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True}
# Pure local computation — no network, deterministic for the same inputs.
_PURE = {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}


def _iso_ms(ms: Any) -> str:
    """Epoch milliseconds -> ISO 8601 UTC string."""
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()


def _iso_s(s: Any) -> str:
    """Epoch seconds -> ISO 8601 UTC string."""
    return datetime.fromtimestamp(int(s), tz=timezone.utc).isoformat()


def _backoff(attempt: int) -> float:
    """Exponential backoff with jitter, capped at 8s."""
    return min(0.5 * (2 ** attempt) + random.uniform(0, 0.25), 8.0)


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    """Honor a numeric Retry-After header (capped) if present, else backoff."""
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return min(float(retry_after), 10.0)
        except ValueError:
            pass
    return _backoff(attempt)


async def _get(
    base_url: str,
    path: str,
    params: Optional[dict] = None,
    *,
    cache_ttl: float = 0.0,
) -> Any:
    """
    Shared GET helper: retries transient failures with backoff, optionally
    serves/stores a short-lived cache entry, and raises clear RuntimeErrors.
    """
    params = params or {}
    url = f"{base_url}{path}"
    key = (base_url, path, tuple(sorted(params.items())))

    if cache_ttl > 0:
        cached = _CACHE.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]

    last_exc: Optional[Exception] = None
    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT, headers=DEFAULT_HEADERS
    ) as client:
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
                if cache_ttl > 0:
                    _CACHE[key] = (time.monotonic() + cache_ttl, data)
                return data
            except httpx.HTTPStatusError as e:
                last_exc = e
                status = e.response.status_code
                if status in RETRY_STATUS and attempt < MAX_RETRIES:
                    await asyncio.sleep(_retry_delay(e.response, attempt))
                    continue
                raise RuntimeError(
                    f"API error {status} calling {url}: {e.response.text[:300]}"
                ) from e
            except httpx.RequestError as e:
                last_exc = e
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(_backoff(attempt))
                    continue
                raise RuntimeError(
                    f"Network error calling {url}: {e}. "
                    "Check your internet connection."
                ) from e

    # Loop only exits via return/raise above; this guards against logic slips.
    raise RuntimeError(f"Failed calling {url} after retries: {last_exc}")


async def _fetch_klines(
    symbol: str, interval: str, limit: int, cache_ttl: float = 10,
) -> list:
    """Fetch klines and normalize to candle dicts — shared by get_klines and
    get_market_structure."""
    if interval not in KLINE_INTERVALS:
        raise ValueError(
            f"Invalid interval '{interval}'. Must be one of: {sorted(KLINE_INTERVALS)}"
        )
    limit = max(1, min(limit, 500))
    raw = await _get(
        BINANCE_BASE_URL, "/fapi/v1/klines",
        {"symbol": symbol.upper(), "interval": interval, "limit": limit},
        cache_ttl=cache_ttl,
    )
    return [
        {
            "open_time": _iso_ms(c[0]),
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "volume": float(c[5]),
        }
        for c in raw
    ]


@mcp.tool(annotations=_RO)
async def get_btc_price(symbol: str = SYMBOL_DEFAULT) -> dict:
    """
    Get the current price and 24-hour stats for a Binance Futures perpetual
    symbol (default BTCUSDT). Use this before every trade analysis to anchor
    the current market price.

    Args:
        symbol: Trading pair symbol, e.g. "BTCUSDT" or "ETHUSDT".

    Returns:
        dict with last_price, price_change_24h, price_change_pct_24h,
        high_24h, low_24h, volume_24h_base, volume_24h_quote.
    """
    data = await _get(
        BINANCE_BASE_URL, "/fapi/v1/ticker/24hr",
        {"symbol": symbol.upper()}, cache_ttl=3,
    )
    return {
        "symbol": data["symbol"],
        "last_price": float(data["lastPrice"]),
        "price_change_24h": float(data["priceChange"]),
        "price_change_pct_24h": float(data["priceChangePercent"]),
        "high_24h": float(data["highPrice"]),
        "low_24h": float(data["lowPrice"]),
        "volume_24h_base": float(data["volume"]),
        "volume_24h_quote": float(data["quoteVolume"]),
    }


@mcp.tool(annotations=_RO)
async def get_funding_rate(symbol: str = SYMBOL_DEFAULT) -> dict:
    """
    Get the current and predicted funding rate for a perpetual futures
    symbol. Positive funding = longs pay shorts (bearish/overheated long
    pressure). Negative funding = shorts pay longs (bullish pressure /
    potential short squeeze setup). Essential for the FUNDING RATE section
    of every trade checklist.

    Args:
        symbol: Trading pair symbol, e.g. "BTCUSDT".

    Returns:
        dict with symbol, last_funding_rate_pct, mark_price,
        next_funding_time (ISO 8601 UTC), interpretation.
    """
    data = await _get(
        BINANCE_BASE_URL, "/fapi/v1/premiumIndex",
        {"symbol": symbol.upper()}, cache_ttl=10,
    )
    rate = float(data["lastFundingRate"]) * 100

    if rate > 0.02:
        interpretation = "Strongly positive — crowded longs, bearish pressure risk"
    elif rate > 0:
        interpretation = "Mildly positive — longs paying shorts"
    elif rate < -0.02:
        interpretation = "Strongly negative — crowded shorts, bullish/squeeze risk"
    elif rate < 0:
        interpretation = "Mildly negative — shorts paying longs"
    else:
        interpretation = "Neutral — funding flat"

    return {
        "symbol": data["symbol"],
        "last_funding_rate_pct": round(rate, 5),
        "mark_price": float(data["markPrice"]),
        "next_funding_time_utc": _iso_ms(data["nextFundingTime"]),
        "interpretation": interpretation,
    }


@mcp.tool(annotations=_RO)
async def get_klines(
    symbol: str = SYMBOL_DEFAULT,
    interval: str = "4h",
    limit: int = 100,
) -> dict:
    """
    Get OHLCV candlestick data for multi-timeframe analysis (Weekly / Daily /
    4H / 1H structure, Elliott Wave counting, Order Blocks, FVGs).

    Args:
        symbol: Trading pair symbol, e.g. "BTCUSDT".
        interval: One of "1m","3m","5m","15m","30m","1h","2h","4h","6h",
            "8h","12h","1d","3d","1w","1M".
        limit: Number of candles to return (1-500, default 100).

    Returns:
        dict with symbol, interval, and a list of candles, each containing
        open_time (ISO 8601 UTC), open, high, low, close, volume.
    """
    candles = await _fetch_klines(symbol, interval, limit)
    return {"symbol": symbol.upper(), "interval": interval, "candles": candles}


@mcp.tool(annotations=_RO)
async def get_order_book(symbol: str = SYMBOL_DEFAULT, limit: int = 20) -> dict:
    """
    Get live order book depth (bids/asks) to help spot liquidity pools,
    thin zones, and imbalances near key SMC/ICT levels.

    Args:
        symbol: Trading pair symbol, e.g. "BTCUSDT".
        limit: Depth per side (5, 10, 20, 50, 100, 500, 1000; default 20).

    Returns:
        dict with symbol, bids and asks as lists of [price, quantity],
        best_bid, best_ask, spread.
    """
    valid_limits = {5, 10, 20, 50, 100, 500, 1000}
    if limit not in valid_limits:
        limit = min(valid_limits, key=lambda x: abs(x - limit))

    # Not cached — order book depth is live liquidity, freshness matters.
    data = await _get(
        BINANCE_BASE_URL, "/fapi/v1/depth",
        {"symbol": symbol.upper(), "limit": limit},
    )
    bids = [[float(p), float(q)] for p, q in data["bids"]]
    asks = [[float(p), float(q)] for p, q in data["asks"]]
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None

    return {
        "symbol": symbol.upper(),
        "bids": bids,
        "asks": asks,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": round(best_ask - best_bid, 2) if best_bid and best_ask else None,
    }


@mcp.tool(annotations=_RO)
async def get_fear_greed_index() -> dict:
    """
    Get the current Crypto Fear & Greed Index (0-100). Extreme Fear (<20)
    historically lines up with market bottoms; Extreme Greed (>80) with
    tops. Use this alongside funding rate for sentiment confluence in the
    trade checklist.

    Returns:
        dict with value (0-100), classification (e.g. "Extreme Fear",
        "Fear", "Neutral", "Greed", "Extreme Greed"), and timestamp_utc.
    """
    data = await _get(FNG_BASE_URL, "/fng/", {"limit": 1}, cache_ttl=300)
    entry = data["data"][0]
    return {
        "value": int(entry["value"]),
        "classification": entry["value_classification"],
        "timestamp_utc": _iso_s(entry["timestamp"]),
    }


@mcp.tool(annotations=_RO)
async def get_market_overview(top_n: int = 10) -> dict:
    """
    Get a broader crypto market snapshot: total market cap, total volume,
    BTC dominance, and the top N coins by market cap (including altcoins).
    Use this so BTC analysis isn't done in isolation from the wider market.

    Args:
        top_n: How many top coins to include (1-50, default 10).

    Returns:
        dict with total_market_cap_usd, total_volume_24h_usd,
        btc_dominance_pct, market_cap_change_pct_24h, and a list of top
        coins each with symbol, name, price_usd, market_cap_usd,
        price_change_pct_24h.
    """
    top_n = max(1, min(top_n, 50))

    global_data = await _get(COINGECKO_BASE_URL, "/global", cache_ttl=60)
    g = global_data["data"]

    coins = await _get(
        COINGECKO_BASE_URL, "/coins/markets",
        {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": top_n,
            "page": 1,
            "sparkline": "false",
        },
        cache_ttl=60,
    )

    return {
        "total_market_cap_usd": g["total_market_cap"]["usd"],
        "total_volume_24h_usd": g["total_volume"]["usd"],
        "btc_dominance_pct": round(g["market_cap_percentage"].get("btc", 0), 2),
        "market_cap_change_pct_24h": round(
            g.get("market_cap_change_percentage_24h_usd", 0), 2
        ),
        "top_coins": [
            {
                "symbol": c["symbol"].upper(),
                "name": c["name"],
                "price_usd": c["current_price"],
                "market_cap_usd": c["market_cap"],
                "price_change_pct_24h": round(
                    c.get("price_change_percentage_24h") or 0, 2
                ),
            }
            for c in coins
        ],
    }


def _validate_period(period: str) -> None:
    if period not in DATA_PERIODS:
        raise ValueError(
            f"Invalid period '{period}'. Must be one of: {sorted(DATA_PERIODS)}"
        )


@mcp.tool(annotations=_RO)
async def get_open_interest(
    symbol: str = SYMBOL_DEFAULT,
    period: str = "4h",
    limit: int = 30,
) -> dict:
    """
    Get current open interest plus its recent history. Rising OI means new
    positions/leverage are entering (a move is "backed" by fresh money);
    falling OI means positions are closing / deleveraging. Pair with price
    direction to judge whether a trend has conviction behind it.

    Args:
        symbol: Trading pair symbol, e.g. "BTCUSDT".
        period: History bucket — one of "5m","15m","30m","1h","2h","4h",
            "6h","12h","1d" (default "4h").
        limit: Number of history points (1-500, default 30).

    Returns:
        dict with symbol, current_open_interest (base units, e.g. BTC),
        current_time_utc, period, window_change_pct (first->last of the
        returned history), interpretation, and a history list of
        {timestamp_utc, open_interest, open_interest_value_usd}.
    """
    _validate_period(period)
    limit = max(1, min(limit, 500))
    sym = symbol.upper()

    current = await _get(
        BINANCE_BASE_URL, "/fapi/v1/openInterest", {"symbol": sym}, cache_ttl=15,
    )
    hist = await _get(
        BINANCE_BASE_URL, "/futures/data/openInterestHist",
        {"symbol": sym, "period": period, "limit": limit}, cache_ttl=30,
    )

    history = [
        {
            "timestamp_utc": _iso_ms(h["timestamp"]),
            "open_interest": float(h["sumOpenInterest"]),
            "open_interest_value_usd": float(h["sumOpenInterestValue"]),
        }
        for h in hist
    ]

    window_change_pct = None
    if len(history) >= 2 and history[0]["open_interest"]:
        window_change_pct = round(
            (history[-1]["open_interest"] - history[0]["open_interest"])
            / history[0]["open_interest"] * 100,
            2,
        )

    if window_change_pct is None:
        interpretation = "Not enough history to gauge the trend."
    elif window_change_pct > 0:
        interpretation = (
            f"Open interest up {window_change_pct}% over the window — new "
            "positions building (leverage/conviction entering)."
        )
    elif window_change_pct < 0:
        interpretation = (
            f"Open interest down {abs(window_change_pct)}% over the window — "
            "positions closing / deleveraging."
        )
    else:
        interpretation = "Open interest roughly flat over the window."

    return {
        "symbol": sym,
        "current_open_interest": float(current["openInterest"]),
        "current_time_utc": _iso_ms(current["time"]),
        "period": period,
        "window_change_pct": window_change_pct,
        "interpretation": interpretation,
        "history": history,
    }


def _ls_interpret(ratio: Optional[float]) -> str:
    if ratio is None:
        return "unavailable"
    if ratio > 1:
        return f"net long (ratio {ratio:.2f}: more longs than shorts)"
    if ratio < 1:
        return f"net short (ratio {ratio:.2f}: more shorts than longs)"
    return "balanced (ratio 1.00)"


@mcp.tool(annotations=_RO)
async def get_long_short_ratio(
    symbol: str = SYMBOL_DEFAULT,
    period: str = "4h",
    limit: int = 30,
) -> dict:
    """
    Get positioning from two angles: top-trader positions ("smart money")
    vs the global account crowd ("retail"). Divergence is the signal — e.g.
    top traders net long while the crowd is net short can mark a squeeze
    setup. Ratio > 1 = more longs than shorts.

    Args:
        symbol: Trading pair symbol, e.g. "BTCUSDT".
        period: One of "5m","15m","30m","1h","2h","4h","6h","12h","1d"
            (default "4h").
        limit: History points fetched to find the latest (1-500, default 30).

    Returns:
        dict with symbol, period, top_trader_position_long_short_ratio,
        global_account_long_short_ratio, their timestamps, and an
        interpretation contrasting smart money vs the crowd.
    """
    _validate_period(period)
    limit = max(1, min(limit, 500))
    sym = symbol.upper()

    top = await _get(
        BINANCE_BASE_URL, "/futures/data/topLongShortPositionRatio",
        {"symbol": sym, "period": period, "limit": limit}, cache_ttl=60,
    )
    glob = await _get(
        BINANCE_BASE_URL, "/futures/data/globalLongShortAccountRatio",
        {"symbol": sym, "period": period, "limit": limit}, cache_ttl=60,
    )

    def latest(rows: list) -> Optional[dict]:
        return max(rows, key=lambda x: int(x["timestamp"])) if rows else None

    t, g = latest(top), latest(glob)
    top_ratio = float(t["longShortRatio"]) if t else None
    global_ratio = float(g["longShortRatio"]) if g else None

    interpretation = (
        f"Top traders {_ls_interpret(top_ratio)}; "
        f"crowd {_ls_interpret(global_ratio)}."
    )

    return {
        "symbol": sym,
        "period": period,
        "top_trader_position_long_short_ratio": top_ratio,
        "global_account_long_short_ratio": global_ratio,
        "top_trader_timestamp_utc": _iso_ms(t["timestamp"]) if t else None,
        "global_timestamp_utc": _iso_ms(g["timestamp"]) if g else None,
        "interpretation": interpretation,
    }


@mcp.tool(annotations=_RO)
async def get_taker_volume(
    symbol: str = SYMBOL_DEFAULT,
    period: str = "4h",
    limit: int = 30,
) -> dict:
    """
    Get the latest taker buy/sell volume ratio — aggressive market-order
    flow. Ratio > 1 means takers are lifting offers (net aggressive buying);
    < 1 means hitting bids (net aggressive selling). Useful for confirming
    whether a move has real order-flow behind it.

    Args:
        symbol: Trading pair symbol, e.g. "BTCUSDT".
        period: One of "5m","15m","30m","1h","2h","4h","6h","12h","1d"
            (default "4h").
        limit: History points fetched to find the latest (1-500, default 30).

    Returns:
        dict with symbol, period, taker_buy_sell_ratio, taker_buy_volume,
        taker_sell_volume, timestamp_utc, interpretation.
    """
    _validate_period(period)
    limit = max(1, min(limit, 500))
    sym = symbol.upper()

    rows = await _get(
        BINANCE_BASE_URL, "/futures/data/takerlongshortRatio",
        {"symbol": sym, "period": period, "limit": limit}, cache_ttl=60,
    )
    latest = max(rows, key=lambda x: int(x["timestamp"])) if rows else None
    ratio = float(latest["buySellRatio"]) if latest else None

    if ratio is None:
        interpretation = "unavailable"
    elif ratio > 1:
        interpretation = f"Net aggressive buying (buy/sell {ratio:.2f})."
    elif ratio < 1:
        interpretation = f"Net aggressive selling (buy/sell {ratio:.2f})."
    else:
        interpretation = "Balanced taker flow."

    return {
        "symbol": sym,
        "period": period,
        "taker_buy_sell_ratio": ratio,
        "taker_buy_volume": float(latest["buyVol"]) if latest else None,
        "taker_sell_volume": float(latest["sellVol"]) if latest else None,
        "timestamp_utc": _iso_ms(latest["timestamp"]) if latest else None,
        "interpretation": interpretation,
    }


@mcp.tool(annotations=_RO)
async def get_market_structure(
    symbol: str = SYMBOL_DEFAULT,
    interval: str = "4h",
    limit: int = 150,
    swing_strength: int = 2,
) -> dict:
    """
    Compute ICT / SMC market structure from candles: swing points, market
    structure (BOS / CHoCH) with current trend, Fair Value Gaps, order blocks,
    equal-high/low liquidity, premium/discount of the CURRENT dealing range
    (last swing low to last swing high — zone can be "above_range" or
    "below_range" when price has already broken past the last confirmed swing),
    and ATR (volatility, for sanity-checking a stop distance). This is the
    structural read that raw candles alone don't give you.

    These are documented heuristics, not validated signals — and Elliott Wave is
    NOT auto-counted (reliable auto-counting isn't feasible); use the returned
    swing points to count manually.

    Args:
        symbol: Trading pair symbol, e.g. "BTCUSDT".
        interval: Candle timeframe (see get_klines). Default "4h".
        limit: Candles to analyze (1-500, default 150 — more = more context).
        swing_strength: Fractal window on each side for swing detection
            (1-10, default 2; higher = only larger/major swings).

    Returns:
        dict with symbol, interval, trend, recent_structure_events, swings,
        premium_discount, fair_value_gaps, order_blocks, liquidity, atr, notes.
    """
    swing_strength = max(1, min(swing_strength, 10))
    candles = await _fetch_klines(symbol, interval, limit)
    result = structure.analyze(candles, swing_strength=swing_strength)
    return {"symbol": symbol.upper(), "interval": interval, **result}


@mcp.tool(annotations=_RO)
async def get_top_down(
    symbol: str = SYMBOL_DEFAULT,
    swing_strength: int = 2,
    daily_limit: int = 100,
    h4_limit: int = 150,
    h1_limit: int = 150,
) -> dict:
    """
    Fetch and compute ICT/SMC structure for Daily, 4H, and 1H in one call —
    matching the plan's top-down framework (Section 5): Daily sets the macro
    trend/bias, 4H holds the Order Block/FVG/liquidity zone, 1H is where you
    wait for the BOS/CHoCH entry trigger inside that zone. Saves three separate
    get_market_structure round-trips at the start of a session.

    Args:
        symbol: Trading pair symbol, e.g. "BTCUSDT".
        swing_strength: Fractal window for swing detection, applied to all
            three timeframes (1-10, default 2).
        daily_limit: Daily candles to analyze (default 100).
        h4_limit: 4H candles to analyze (default 150).
        h1_limit: 1H candles to analyze (default 150).

    Returns:
        dict with symbol, daily/h4/h1 (each shaped like get_market_structure:
        interval, trend, recent_structure_events, swings, premium_discount,
        fair_value_gaps, order_blocks, liquidity, atr), and a coaching note.
    """
    swing_strength = max(1, min(swing_strength, 10))

    async def one(interval: str, limit: int) -> dict:
        candles = await _fetch_klines(symbol, interval, limit)
        return {"interval": interval,
                **structure.analyze(candles, swing_strength=swing_strength)}

    daily, h4, h1 = await asyncio.gather(
        one("1d", daily_limit), one("4h", h4_limit), one("1h", h1_limit),
    )

    return {
        "symbol": symbol.upper(),
        "daily": daily,
        "h4": h4,
        "h1": h1,
        "note": (
            "Top-down read (plan Section 5): Daily trend sets bias, 4H holds "
            "the OB/FVG/liquidity zone, 1H looks for the BOS/CHoCH entry "
            "trigger inside it. Elliott Wave is not auto-counted on any "
            "timeframe — use the swing points to count manually."
        ),
    }


@mcp.tool(annotations=_PURE)
async def calculate_position_size(
    entry: float,
    stop_loss: float,
    take_profit: Optional[float] = None,
    capital: float = 100.0,
    risk_pct: float = 1.0,
    leverage: float = 3.0,
) -> dict:
    """
    Size a trade the plan's way and check it against every hard risk rule BEFORE
    entry. Give it your entry, your stop (this defines the risk), and optionally
    a target. Direction is inferred (stop below entry = long, above = short).

    It returns the position size in BTC and USD, the margin needed at your
    leverage (and at 2x/3x/5x), an approximate liquidation price, reward:risk,
    the plan's tiered target prices (1:2 / 1:3 / 1:4), an estimated round-trip
    fee, and a list of rule warnings with a pass/fail verdict. Use it for the
    "SL set before entry", "R:R ≥ 1:2", and position-sizing parts of the
    checklist — and to catch a trade that is too small to place on $100 without
    breaking the 2% rule (in which case the answer is: skip it).

    Remember: leverage changes only margin and the liquidation price, never your
    risk. This is math only — Chamara still decides whether to take the trade.

    Args:
        entry: Planned entry price.
        stop_loss: Stop-loss price (invalidation). Must differ from entry.
        take_profit: Optional target price, for reward:risk.
        capital: Account size in USD (default 100).
        risk_pct: Percent of capital to risk (default 1%; hard cap is 2%).
        leverage: Leverage multiple (default 3x; hard cap is 5x).

    Returns:
        dict with direction, risk_amount_usd, stop_distance_pct, position
        (notional/size_btc/margin), reward_risk_ratio, plan_target_prices, fees,
        approx_liquidation_price, passes_plan, warnings, and a verdict.
    """
    return risk.position_size(
        entry=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        capital=capital,
        risk_pct=risk_pct,
        leverage=leverage,
    )


# Plan kill zones in UTC (Docs/02_trading_plan.md, Section 6): (name, start_h, end_h).
KILLZONES = (("London", 7, 9), ("New York", 13, 15))


def _session_state(now: datetime) -> dict:
    """
    Classify a UTC datetime against the plan's kill zones. Pure so it can be
    unit-tested with fixed times; the tool passes the real wall clock.
    """
    now_min = now.hour * 60 + now.minute

    active = None
    minutes_left_in_zone = None
    for name, start, end in KILLZONES:
        if start <= now.hour < end:
            active = name
            minutes_left_in_zone = end * 60 - now_min
            break

    next_name = None
    minutes_until_next = None
    if active is None:
        upcoming = [
            (name, start * 60 - now_min)
            for name, start, _ in KILLZONES
            if start * 60 > now_min
        ]
        if upcoming:
            next_name, minutes_until_next = min(upcoming, key=lambda x: x[1])
        else:
            # All of today's windows have passed — next is tomorrow's first.
            first_name, first_start, _ = KILLZONES[0]
            next_name = first_name
            minutes_until_next = (24 * 60 - now_min) + first_start * 60

    is_weekend = now.weekday() >= 5

    if is_weekend:
        note = (
            "Weekend — thinner liquidity and little scheduled news. Your plan "
            "caps weekend leverage at 2x and many clean setups simply aren't "
            "here. No trade is a fine weekend."
        )
    elif active:
        note = (
            f"In the {active} kill zone ({minutes_left_in_zone} min left). A valid "
            "trading window ONLY if all 7 checklist points are YES."
        )
    else:
        h, m = divmod(minutes_until_next, 60)
        note = (
            "Outside your kill zones. You trade London 07:00–09:00 and New York "
            f"13:00–15:00 UTC only. Next: {next_name} in {h}h {m:02d}m. Use this "
            "time to prep levels, not to force a trade."
        )

    return {
        "utc_time": now.strftime("%Y-%m-%d %H:%M UTC"),
        "utc_weekday": now.strftime("%A"),
        "in_kill_zone": active is not None,
        "active_kill_zone": active,
        "minutes_left_in_zone": minutes_left_in_zone,
        "next_kill_zone": next_name,
        "minutes_until_next": minutes_until_next,
        "is_weekend": is_weekend,
        "kill_zones_utc": {"London": "07:00-09:00", "New York": "13:00-15:00"},
        "note": note,
    }


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": False,
                       "openWorldHint": False})
async def get_session_clock() -> dict:
    """
    Get the current UTC time and whether the market is inside one of the plan's
    kill zones — London 07:00–09:00 UTC or New York 13:00–15:00 UTC. Use this
    for the "Kill Zone timing correct" checklist item and the daily routine, so
    timing is read from the real clock instead of guessed.

    Returns:
        dict with utc_time, utc_weekday, in_kill_zone, active_kill_zone,
        minutes_left_in_zone, next_kill_zone, minutes_until_next, is_weekend,
        the kill-zone windows, and a coaching note.
    """
    return _session_state(datetime.now(timezone.utc))


@mcp.tool(annotations=_RO)
async def get_economic_calendar(
    currencies: str = "USD",
    min_impact: str = "High",
    within_minutes: int = 30,
) -> dict:
    """
    Check this week's high-impact economic calendar for the "no high-impact news
    within 30 min" rule (checklist item 7). Answers whether a qualifying release
    (FOMC, CPI, NFP, PCE, PPI, etc.) falls inside a blackout window around now,
    and lists what's coming up. BTC reacts mostly to USD macro, so the default
    filter is USD / High impact.

    Args:
        currencies: Comma-separated currency codes to watch (default "USD";
            e.g. "USD,EUR"). These are the event's currency, not a trading pair.
        min_impact: Minimum impact to include — "High", "Medium", or "Low"
            (default "High").
        within_minutes: Blackout half-window in minutes around now (default 30).

    Returns:
        dict with now_utc, filter, blackout_active, news_clear, next_event,
        upcoming (list), source, and a coaching note. Data is a free public
        ForexFactory mirror (no API key) — verify against an official calendar.
    """
    raw = await _get(FF_BASE_URL, "/ff_calendar_thisweek.json", cache_ttl=900)
    currs = tuple(c.strip() for c in currencies.split(",") if c.strip())
    return news.analyze(
        raw,
        datetime.now(timezone.utc),
        currencies=currs or ("USD",),
        min_impact=min_impact,
        within_minutes=within_minutes,
    )


if __name__ == "__main__":
    mcp.run()
