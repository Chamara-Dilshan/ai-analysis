"""
Deterministic unit tests for structure.py — synthetic candle sequences whose
swings / FVGs / order blocks / BOS-CHoCH are known by hand. Pure logic, no I/O.
"""
import structure as S


def mk(i, h, l, c, o=None):
    return {"open_time": f"t{i}", "open": c if o is None else o,
            "high": float(h), "low": float(l), "close": float(c), "volume": 1.0}


# --------------------------------------------------------------------------- #
# Swings
# --------------------------------------------------------------------------- #
def test_find_swings_basic():
    candles = [
        mk(0, 10, 8, 9),
        mk(1, 12, 9, 11),   # swing high (12 > 10 and > 11)
        mk(2, 11, 7, 8),    # swing low  (7 < 9 and < 9)
        mk(3, 14, 9, 13),   # swing high (14 > 11 and > 13)
        mk(4, 13, 10, 10),
    ]
    sw = S.find_swings(candles, left=1, right=1)
    assert [(s["index"], s["type"], s["price"]) for s in sw] == [
        (1, "high", 12.0), (2, "low", 7.0), (3, "high", 14.0),
    ]


# --------------------------------------------------------------------------- #
# BOS / CHoCH
# --------------------------------------------------------------------------- #
def test_detect_structure_bos_then_choch():
    candles = [
        mk(0, 10, 8, 9),
        mk(1, 12, 9, 11),    # swing high @12 (activates t2)
        mk(2, 11, 7, 8),     # swing low  @7  (activates t3)
        mk(3, 13, 9, 13),    # close 13 breaks 12 -> BOS bullish (trend none->up)
        mk(4, 12, 11, 11.5),  # swing high @13 (activates t4)
        mk(5, 10, 6, 6.5),   # close 6.5 breaks low 7 -> CHoCH bearish (trend up)
    ]
    sw = S.find_swings(candles, left=1, right=1)
    trend, events = S.detect_structure(candles, sw, right=1)
    assert trend == "down"
    assert [(e["type"], e["direction"], e["level"]) for e in events] == [
        ("BOS", "bullish", 12.0), ("CHoCH", "bearish", 7.0),
    ]


# --------------------------------------------------------------------------- #
# Fair Value Gaps
# --------------------------------------------------------------------------- #
def test_bullish_fvg_unmitigated():
    candles = [mk(0, 10, 8, 9), mk(1, 15, 11, 15), mk(2, 16, 13, 15.5),
               mk(3, 15, 14, 14.5)]  # never trades back to <=13
    fvgs = S.find_fvgs(candles)
    assert len(fvgs) == 1
    f = fvgs[0]
    assert f["type"] == "bullish" and f["top"] == 13.0 and f["bottom"] == 10.0
    assert f["mitigated"] is False


def test_bullish_fvg_mitigated():
    candles = [mk(0, 10, 8, 9), mk(1, 15, 11, 15), mk(2, 16, 13, 15.5),
               mk(3, 15, 9, 10)]  # low 9 re-enters the gap
    fvgs = S.find_fvgs(candles)
    assert len(fvgs) == 1 and fvgs[0]["mitigated"] is True


def test_bearish_fvg():
    candles = [mk(0, 20, 18, 19), mk(1, 17, 13, 13.5), mk(2, 16, 12, 15)]
    fvgs = S.find_fvgs(candles)
    assert len(fvgs) == 1
    f = fvgs[0]
    assert f["type"] == "bearish" and f["top"] == 18.0 and f["bottom"] == 16.0


# --------------------------------------------------------------------------- #
# Order blocks
# --------------------------------------------------------------------------- #
def test_bullish_order_block():
    candles = [mk(0, 10, 8, 9, o=10),    # bearish candle (close<open) -> the OB
               mk(1, 15, 11, 15, o=9),   # displacement up, creates bullish FVG
               mk(2, 16, 13, 15.5)]
    obs = S.find_order_blocks(candles)
    assert len(obs) == 1
    ob = obs[0]
    assert ob["type"] == "bullish" and ob["index"] == 0
    assert ob["top"] == 10.0 and ob["bottom"] == 8.0


# --------------------------------------------------------------------------- #
# Liquidity (equal highs/lows)
# --------------------------------------------------------------------------- #
def test_equal_highs_cluster():
    swings = [
        {"index": 1, "time": "a", "price": 100.0, "type": "high"},
        {"index": 5, "time": "b", "price": 100.05, "type": "high"},  # within 0.1%
        {"index": 3, "time": "c", "price": 90.0, "type": "low"},
    ]
    liq = S.find_liquidity(swings, tol_pct=0.1)
    assert len(liq["equal_highs"]) == 1
    assert liq["equal_highs"][0]["count"] == 2
    assert liq["equal_lows"] == []


# --------------------------------------------------------------------------- #
# Premium / discount
# --------------------------------------------------------------------------- #
def test_premium_zone():
    candles = [mk(0, 20, 10, 12), mk(1, 18, 14, 18)]  # range 10-20, close 18
    pd = S.premium_discount(candles)
    assert pd["range_high"] == 20.0 and pd["range_low"] == 10.0
    assert pd["position_pct"] == 80.0 and pd["zone"] == "premium"


# --------------------------------------------------------------------------- #
# Full analyze()
# --------------------------------------------------------------------------- #
def test_analyze_end_to_end():
    candles = [
        mk(0, 10, 8, 9), mk(1, 12, 9, 11), mk(2, 11, 7, 8),
        mk(3, 13, 9, 13), mk(4, 12, 11, 11.5), mk(5, 10, 6, 6.5),
    ]
    out = S.analyze(candles, swing_strength=1)
    assert "error" not in out
    assert out["trend"] == "down"
    assert out["candles_analyzed"] == 6
    assert out["recent_structure_events"]  # non-empty
    assert "premium_discount" in out and "liquidity" in out


def test_analyze_too_few_candles():
    out = S.analyze([mk(0, 10, 8, 9)], swing_strength=2)
    assert "error" in out
