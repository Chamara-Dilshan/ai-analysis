"""
Deterministic tests for structure.rsi / structure.rsi_divergence — synthetic
close sequences whose RSI direction and swing pivots are known by hand.
"""
import structure as S


def mk(i, c):
    return {"open_time": f"t{i}", "open": float(c), "high": float(c) + 0.2,
            "low": float(c) - 0.2, "close": float(c), "volume": 1.0}


def series(*segments):
    """segments = (start, step, count) tuples; each continues from its own start."""
    closes = []
    for start, step, count in segments:
        closes.extend(start + step * k for k in range(count))
    return [mk(i, c) for i, c in enumerate(closes)]


# --------------------------------------------------------------------------- #
# RSI
# --------------------------------------------------------------------------- #
def test_rsi_length_and_warmup():
    candles = series((10, 1.0, 30))
    out = S.rsi(candles, 14)
    assert len(out) == 30
    assert out[:14] == [None] * 14
    assert all(v is not None for v in out[14:])


def test_rsi_extremes_and_flat():
    assert S.rsi(series((10, 1.0, 30)), 14)[-1] == 100.0     # only gains
    assert S.rsi(series((40, -1.0, 30)), 14)[-1] == 0.0      # only losses
    assert S.rsi(series((20, 0.0, 30)), 14)[-1] == 50.0      # no movement
    assert S.rsi(series((20, 1.0, 10)), 14) == [None] * 10   # too short


def test_rsi_mixed_is_between():
    # Six +1 bars then six -0.5 bars: gains outweigh losses, so RSI > 50 but < 100.
    candles = series((20, 0.0, 16), (21, 1.0, 6), (26.5, -0.5, 6))
    v = S.rsi(candles, 14)[-1]
    assert 50.0 < v < 100.0


# --------------------------------------------------------------------------- #
# Divergence
# --------------------------------------------------------------------------- #
def bullish_case():
    # flat -> steep drop to 10 (RSI pinned at 0) -> bounce to 13 -> shallow
    # drop to a LOWER low at 9 (RSI now above 0) -> two bars up to confirm.
    return series((20, 0.0, 16), (19, -1.0, 10), (10.5, 0.5, 6),
                  (12.6, -0.4, 10), (9.5, 0.5, 2))


def test_rsi_divergence_bullish_lower_low_higher_rsi():
    div = S.rsi_divergence(bullish_case())
    assert div is not None and div["type"] == "bullish"
    assert div["index"] == 41
    assert div["price_b"] < div["price_a"]
    assert div["rsi_b"] > div["rsi_a"]


def test_rsi_divergence_bearish_higher_high_lower_rsi():
    candles = series((20, 0.0, 16), (21, 1.0, 10), (29.5, -0.5, 6),
                     (27.4, 0.4, 10), (30.5, -0.5, 2))
    div = S.rsi_divergence(candles)
    assert div is not None and div["type"] == "bearish"
    assert div["index"] == 41
    assert div["price_b"] > div["price_a"]
    assert div["rsi_b"] < div["rsi_a"]


def test_rsi_divergence_none_when_momentum_confirms_the_low():
    # Second trough is both a lower low AND a much steeper drop, so RSI is
    # lower too — momentum agrees with price, no divergence to report.
    candles = series((20, 0.0, 16), (21, 1.0, 6), (25.5, -0.5, 6),
                     (23.5, 0.5, 3), (23, -1.5, 14), (4, 0.5, 2))
    assert S.rsi_divergence(candles) is None


def test_rsi_divergence_respects_lookback():
    stale = bullish_case() + [mk(100 + k, 10.0) for k in range(35)]
    assert S.rsi_divergence(stale, lookback=30) is None
    assert S.rsi_divergence(stale, lookback=60) is not None


def test_rsi_divergence_cannot_see_an_unconfirmed_pivot():
    # Drop the two confirming bars: the second trough is the last real candle
    # and cannot be a swing yet, so there is only one usable low -> None.
    assert S.rsi_divergence(bullish_case()[:-2]) is None
