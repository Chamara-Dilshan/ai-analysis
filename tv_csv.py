"""
Read a TradingView "Export chart data" CSV and run the same ICT/SMC structure
analysis the MCP server runs on Binance klines.

TradingView's export column names and time format vary by chart, plan and
locale, so parsing is deliberately defensive: delimiter is sniffed, headers are
matched case-insensitively, and the time column accepts epoch seconds, epoch
milliseconds, or ISO-8601 (with or without an offset). Any extra indicator
columns the chart happened to plot are ignored.

Usage:
    python tv_csv.py "C:/path/BINANCE_BTCUSDT.P, 60.csv"
    python tv_csv.py <path> --last 300 --swing 3
    python tv_csv.py <path> --json
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from typing import Any, Optional

import structure

# Header name -> candle key. Matched against lowercased, stripped headers, so
# "Volume" and "volume" both land on "volume" while "Volume MA" is ignored.
_FIELDS = {
    "time": "open_time",
    "date": "open_time",
    "datetime": "open_time",
    "timestamp": "open_time",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume",
}

_REQUIRED = ("open_time", "open", "high", "low", "close")


class ParseError(Exception):
    """The file isn't a usable TradingView OHLC export."""


def _sniff_delimiter(sample: str) -> str:
    """Comma normally; semicolon in locales where ',' is the decimal separator."""
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
    except csv.Error:
        return ","


def _parse_time(raw: str) -> tuple[str, bool]:
    """
    Return (ISO-8601 UTC string, was_naive).

    was_naive is True when the source had no timezone offset — TradingView
    exports in the chart's *display* timezone, so a naive stamp is assumed UTC
    and the caller warns that levels may be shifted.
    """
    text = raw.strip()
    if not text:
        raise ParseError("empty time value")

    # Epoch seconds or milliseconds.
    try:
        number = float(text)
    except ValueError:
        pass
    else:
        seconds = number / 1000 if abs(number) > 1e11 else number
        dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
        return dt.isoformat().replace("+00:00", "Z"), False

    iso = text.replace("Z", "+00:00").replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise ParseError(f"unrecognised time value {raw!r}") from exc

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z"), True
    dt = dt.astimezone(timezone.utc)
    return dt.isoformat().replace("+00:00", "Z"), False


def _parse_price(raw: str) -> Optional[float]:
    """Tolerate thousands separators and decimal commas; blank -> None."""
    text = raw.strip().replace(" ", "")
    if not text or text.lower() in {"nan", "null", "n/a", "-"}:
        return None
    if "," in text and "." in text:
        text = text.replace(",", "")          # 1,234.56
    elif text.count(",") == 1 and "." not in text:
        text = text.replace(",", ".")          # 1234,56
    try:
        return float(text)
    except ValueError:
        return None


def load_candles(path: str) -> tuple[list[dict], dict]:
    """
    Parse a TradingView CSV into structure.py's candle dicts.

    Returns (candles, meta) where candles is chronological (oldest first) and
    meta records what was skipped or assumed, so nothing is silently dropped.
    """
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(8192)
        handle.seek(0)
        reader = csv.reader(handle, delimiter=_sniff_delimiter(sample))
        try:
            header = next(reader)
        except StopIteration:
            raise ParseError("file is empty")

        columns: dict[str, int] = {}
        for index, name in enumerate(header):
            key = _FIELDS.get(name.strip().lower())
            if key and key not in columns:
                columns[key] = index

        missing = [f for f in _REQUIRED if f not in columns]
        if missing:
            raise ParseError(
                f"missing column(s) {', '.join(missing)}; header was {header}"
            )

        candles: list[dict] = []
        skipped = 0
        naive_times = 0
        for row in reader:
            if len(row) <= max(columns.values()):
                skipped += 1
                continue
            try:
                stamp, was_naive = _parse_time(row[columns["open_time"]])
            except ParseError:
                skipped += 1
                continue
            prices = {
                key: _parse_price(row[columns[key]])
                for key in ("open", "high", "low", "close")
            }
            if any(value is None for value in prices.values()):
                skipped += 1
                continue
            if prices["high"] < prices["low"]:
                skipped += 1
                continue
            volume = (
                _parse_price(row[columns["volume"]]) if "volume" in columns else None
            )
            naive_times += int(was_naive)
            candles.append({
                "open_time": stamp,
                "open": prices["open"],
                "high": prices["high"],
                "low": prices["low"],
                "close": prices["close"],
                "volume": volume if volume is not None else 0.0,
            })

    if not candles:
        raise ParseError("no usable OHLC rows found")

    was_descending = len(candles) > 1 and candles[0]["open_time"] > candles[-1]["open_time"]
    candles.sort(key=lambda c: c["open_time"])

    return candles, {
        "source_file": path,
        "rows_parsed": len(candles),
        "rows_skipped": skipped,
        "reordered_to_chronological": was_descending,
        "naive_timestamps": naive_times,
        "timezone_note": (
            "Timestamps had no UTC offset and were assumed UTC. TradingView "
            "exports in the chart's display timezone — if your chart is not on "
            "UTC, candle times here are shifted (price levels are unaffected)."
            if naive_times else None
        ),
        "first_candle": candles[0]["open_time"],
        "last_candle": candles[-1]["open_time"],
    }


def _summarize(meta: dict, result: dict) -> str:
    """Human-readable digest of the structure read."""
    lines = [
        "=" * 62,
        f"  {meta['source_file']}",
        "=" * 62,
        f"Candles      : {meta['rows_parsed']}  (skipped {meta['rows_skipped']})",
        f"Range        : {meta['first_candle']}  ->  {meta['last_candle']}",
    ]
    if meta["reordered_to_chronological"]:
        lines.append("Note         : file was newest-first; reordered to oldest-first")
    if meta["timezone_note"]:
        lines.append(f"Note         : {meta['timezone_note']}")

    if "error" in result:
        lines.append(f"\nERROR: {result['error']}")
        return "\n".join(lines)

    pd = result["premium_discount"]
    atr_info = result["atr"]
    lines += [
        "",
        f"Trend        : {result['trend'].upper()}",
        f"Dealing range: {pd['range_low']} - {pd['range_high']}  "
        f"(EQ {pd['equilibrium']}, source: {pd['range_source']})",
        f"Last close   : {pd['current_close']}  -> {pd['position_pct']}% "
        f"= {pd['zone'].upper()}",
    ]
    if pd.get("note"):
        lines.append(f"               {pd['note']}")
    lines.append(
        f"ATR({atr_info['period']})      : {atr_info['value']} "
        f"({atr_info['pct_of_price']}% of price)"
    )

    lines.append("\nRecent structure events (newest first):")
    for event in result["recent_structure_events"] or []:
        lines.append(
            f"  {event['time']}  {event['type']:5} {event['direction']:7} "
            f"@ {event['level']}"
        )
    if not result["recent_structure_events"]:
        lines.append("  (none)")

    fvgs = result["fair_value_gaps"]
    lines.append(
        f"\nOpen FVGs ({fvgs['open_count']} of {fvgs['total_count']} total):"
    )
    for gap in fvgs["open"] or []:
        lines.append(
            f"  {gap['time']}  {gap['type']:7} {gap['bottom']} - {gap['top']} "
            f"({gap['size_pct']}%)"
        )
    if not fvgs["open"]:
        lines.append("  (none)")

    obs = result["order_blocks"]
    lines.append(
        f"\nOpen order blocks ({obs['open_count']} of {obs['total_count']} total):"
    )
    for block in obs["open"] or []:
        lines.append(
            f"  {block['time']}  {block['type']:7} {block['bottom']} - {block['top']}"
        )
    if not obs["open"]:
        lines.append("  (none)")

    liq = result["liquidity"]
    lines.append("\nResting liquidity:")
    for level in liq["equal_highs"]:
        lines.append(f"  buy-side  @ {level['price']}  (x{level['count']})")
    for level in liq["equal_lows"]:
        lines.append(f"  sell-side @ {level['price']}  (x{level['count']})")
    if not liq["equal_highs"] and not liq["equal_lows"]:
        lines.append("  (none)")

    return "\n".join(lines)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Analyze a TradingView chart-data CSV export."
    )
    parser.add_argument("path", help="path to the exported .csv")
    parser.add_argument("--last", type=int, default=0,
                        help="analyze only the last N candles (0 = all)")
    parser.add_argument("--swing", type=int, default=2,
                        help="swing strength / fractal width (default 2)")
    parser.add_argument("--json", action="store_true",
                        help="print raw JSON instead of a summary")
    args = parser.parse_args(argv)

    try:
        candles, meta = load_candles(args.path)
    except (OSError, ParseError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.last > 0 and len(candles) > args.last:
        candles = candles[-args.last:]
        meta["rows_parsed"] = len(candles)
        meta["first_candle"] = candles[0]["open_time"]
        meta["last_candle"] = candles[-1]["open_time"]

    result: dict[str, Any] = structure.analyze(candles, swing_strength=args.swing)

    if args.json:
        print(json.dumps({"meta": meta, "structure": result}, indent=2))
    else:
        print(_summarize(meta, result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
