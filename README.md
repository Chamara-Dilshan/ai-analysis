# Binance BTC Futures MCP Server

Chamara ගේ trading assistant එකට live BTCUSDT Perpetual futures data + market
sentiment/overview දෙන local MCP server එක. Claude Desktop app එකේ **stdio**
transport හරහා run වෙනවා — public hosting/API key කිසිවක් අවශ්‍ය නෑ (Binance,
alternative.me, CoinGecko market data endpoints ඔක්කොම public).

## 🛠️ Tools මොනවද තියෙන්නේ

| Tool | මොකද කරන්නේ | Source |
|---|---|---|
| `get_btc_price` | Current price, 24h high/low/change/volume | Binance |
| `get_funding_rate` | Funding rate + bullish/bearish interpretation | Binance |
| `get_klines` | OHLCV candles — timeframe ඕනෑම එකක් (1m ... 1M) | Binance |
| `get_order_book` | Bid/ask depth — liquidity pools/thin zones බලන්න | Binance |
| `get_open_interest` | Open interest (current + history) — positioning build/unwind බලන්න | Binance |
| `get_long_short_ratio` | Top traders vs retail crowd long/short ratio — divergence signal | Binance |
| `get_taker_volume` | Taker buy/sell volume ratio — aggressive order flow | Binance |
| `get_market_structure` | ICT/SMC structure — swings, BOS/CHoCH, FVG, order blocks, liquidity, premium/discount | Binance (computed) |
| `get_fear_greed_index` | Crypto Fear & Greed Index (0-100) + classification | alternative.me |
| `get_market_overview` | Total market cap, BTC dominance, top N altcoins | CoinGecko |
| `calculate_position_size` | Plan-compliant sizing + risk checks (2% risk, 5x lev, 1:2 R:R, Binance min, liquidation, fees) — pass/fail verdict | Local (plan math) |
| `get_session_clock` | Current UTC time + London/NY kill-zone status for entry timing | Local (clock) |
| `get_economic_calendar` | High-impact news blackout check (FOMC/CPI/NFP…) — "no trade 30 min around news" | ForexFactory (faireconomy.media) |

alternative.me, CoinGecko, සහ ForexFactory calendar feed (faireconomy.media)
ඔක්කොම **free, no API key** — Binance වගේම public data endpoints. (Calendar
feed එක **unofficial mirror** එකක් — schedule එක verify කරගන්න.)

`get_market_structure` extra API call එකක් නෑ — klines වලින්ම (`structure.py`)
locally compute කරනවා. මේවා **heuristic** ICT/SMC rules; validated signals නෙවෙයි,
සහ Elliott Wave auto-count කරන්නේ නෑ (swings වලින් manual count කරන්න).

`calculate_position_size` (`risk.py`) සහ `get_session_clock` දෙකම **network එකක්
නෑ** — pure local. Sizing tool එක trading plan එකේ rules (2% risk, 5x leverage,
1:2 R:R, Binance min order 0.001 BTC / $100 notional) encode කරලා, trade එකකට
කලින්ම pass/fail verdict එකක් දෙනවා. Math විතරයි — signal/advice නෙවෙයි, decision
එක Chamara ගේ.

## 📦 Setup (One-Time)

### 1. Python environment එක හදන්න

```bash
cd binance-mcp
python3 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Server එක standalone test කරන්න (optional)

```bash
python server.py
```
Ctrl+C එකෙන් නවත්තන්න — මේක ok run වුනොත් setup එක හරි.

### 3. Claude Desktop config එකට එකතු කරන්න

Config file එකේ location:
- **Mac**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux**: `~/.config/Claude/claude_desktop_config.json`

File එක නැත්නම් අලුතෙන් හදන්න. තියෙනවා නම් `mcpServers` object එකට මේක
එකතු කරන්න (path එක ඔයාගේ actual absolute path එකට වෙනස් කරන්න):

```json
{
  "mcpServers": {
    "binance-btc-futures": {
      "command": "/absolute/path/to/binance-mcp/venv/bin/python",
      "args": ["/absolute/path/to/binance-mcp/server.py"]
    }
  }
}
```

⚠️ **වැදගත්**: `command` එකේ path එක venv **තුළ** තියෙන python executable
එකට යොමු කරන්න (`which python` venv activate කරලා run කරලා check කරන්න)
— system python එකට යොමු කළොත් `fastmcp`/`httpx` `ModuleNotFoundError` වේවි.

### 4. Claude Desktop restart කරන්න

සම්පූර්ණයෙන්ම quit කරලා (menu bar/tray icon එකෙනුත්) reopen කරන්න.
Settings → Connectors වල `binance-btc-futures` connected ලෙස පේනවා නම් OK.

## ✅ Test කරන්නේ මෙහෙම

Claude Desktop එකේ අලුත් chat එකක් අරන් අහන්න:

> "BTCUSDT price, funding rate, open interest සහ long/short ratio එක්ක Fear &
> Greed index එකයි market overview එකයි check කරලා දෙන්න"

Claude ට අදාළ tools call කරලා live data එකතු කරගන්න පුළුවන් වෙයි.

## 🧪 Automated tests (dev)

`server.py` එක වෙනස් කලාට පස්සේ, මේකෙන් හැම tool එකම + retry/cache logic එක
verify කරගන්න පුළුවන් (network එක ඕනෑ නෑ — APIs mock කරනවා):

```bash
python -m pip install -r requirements-dev.txt   # one-time
python -m pytest -q
```
Tests ඔක්කොම pass වුනොත් `server.py` එක healthy.

## 🔧 Troubleshooting

| ගැටළුව | හේතුව / විසඳුම |
|---|---|
| Connector එක connect වෙන්නෙ නෑ | Config JSON syntax වැරදිද බලන්න (comma, bracket) |
| `ModuleNotFoundError` | `command` path එක venv python එකට යොමු වෙනවද බලන්න |
| Tool call fails: "Network error" | Internet connection / firewall Binance domain block කරනවද |
| Tool call fails: "API error 451" | ඔයාගේ region එකේ Binance restricted විය හැක — VPN එකක් අවශ්‍ය විය හැක |
| Tool call fails: "API error 429" | Rate-limited — auto-retry ඇතත් heavy use නම් ටිකක් ඉඳලා try කරන්න (CoinGecko free tier) |

## 📌 Note

- Market data (price/funding/klines/order book/OI/long-short/taker/sentiment/
  market cap) විතරයි — trading execute කරන්නේ නෑ. Account access, order
  placement, කිසිත් නෑ. Read-only.
- Reliability: හැම API call එකකම 429/5xx errors + network blips වලට auto-retry
  (exponential backoff, `Retry-After` respect) තියෙනවා, සහ slow-changing
  endpoints කෙටි කාලෙකට cache වෙනවා — CoinGecko rate-limit අඩු කරන්න.
- Rate limits: Binance public API සහ CoinGecko free tier දෙකම IP-based
  (heavy use කලොත් 429 errors එන්න පුළුවන්, ඒත් retry logic එකෙන් handle
  වෙනවා) — trading routine එකට (දිනකට analysis 2-3ක්) කිසිම ප්‍රශ්නයක් නෑ.
