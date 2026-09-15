"""
Harness-correctness tests for backtest.py: the forming-candle rebuild and the
post-entry partial candle in simulate_trailing. Pure logic, no network.
"""
import backtest as B
import server

H = 3_600_000
T0 = 1_700_000_000_000 - (1_700_000_000_000 % (4 * H))   # a 4H boundary


def bar(t_ms, o, h, l, c, v=1.0):
    return {"open_time": server._iso_ms(t_ms), "open": float(o), "high": float(h),
            "low": float(l), "close": float(c), "volume": float(v)}


def flat(n, price=100.0):
    return [bar(T0 + k * 4 * H, price, price + 0.5, price - 0.5, price) for k in range(n)]


# --------------------------------------------------------------------------- #
# forming_candle
# --------------------------------------------------------------------------- #
def test_forming_candle_uses_only_bars_printed_so_far():
    c1h = [bar(T0 + k * H, 100 + k, 105 + k, 95 + k, 101 + k) for k in range(8)]
    t1h = [T0 + k * H for k in range(8)]
    two_in = B.forming_candle(c1h, t1h, T0, 1)
    assert (two_in["open"], two_in["high"], two_in["low"], two_in["close"]) == (100, 106, 95, 102)
    assert two_in["volume"] == 2.0 and two_in["open_time"] == server._iso_ms(T0)


def test_forming_candle_equals_the_real_candle_once_complete():
    c1h = [bar(T0 + k * H, 100 + k, 105 + k, 95 + k, 101 + k) for k in range(8)]
    t1h = [T0 + k * H for k in range(8)]
    full = B.forming_candle(c1h, t1h, T0, 3)
    assert (full["open"], full["high"], full["low"], full["close"]) == (100, 108, 95, 104)
    assert full["volume"] == 4.0


def test_forming_candle_is_none_before_it_opens():
    c1h = [bar(T0 + k * H, 100, 101, 99, 100) for k in range(8)]
    t1h = [T0 + k * H for k in range(8)]
    assert B.forming_candle(c1h, t1h, T0 + 4 * H, 2) is None


# --------------------------------------------------------------------------- #
# simulate_trailing with the post-entry partial candle
# --------------------------------------------------------------------------- #
def test_first_candle_stop_hit_ends_the_trade_before_the_walk():
    first = bar(T0, 100, 100.2, 97.0, 99.0)       # dips through the 98 stop after entry
    out, r, held = B.simulate_trailing(flat(30), 10, "long", 100.0, 98.0, first_candle=first)
    assert (out, r, held) == ("loss", -1.0, 0)


def test_first_candle_can_move_the_stop_to_breakeven_before_the_walk():
    c = flat(30)
    c[11] = bar(T0 + 11 * 4 * H, 100, 100.2, 99.0, 99.5)   # below entry, above the 98 stop
    first = bar(T0, 100, 102.5, 99.5, 102.0)                # +1R after entry, short of 2R
    with_first = B.simulate_trailing(c, 10, "long", 100.0, 98.0, first_candle=first)
    without = B.simulate_trailing(c, 10, "long", 100.0, 98.0)
    assert with_first == ("loss", 0.0, 1)        # breakeven stop taken out on the next candle
    assert without[0] == "open" and without[2] == 19   # unchanged legacy behaviour: walks to the end


def test_first_candle_can_lock_the_partial_before_the_walk():
    first = bar(T0, 100, 104.5, 99.8, 104.0)     # through 2R after entry
    out, r, held = B.simulate_trailing(flat(30), 10, "long", 100.0, 98.0, first_candle=first)
    # Half locked at 2R and the stop moved to entry; the next flat candle's
    # 0.5 wick tags that breakeven stop, so the trade closes for the locked 1R.
    assert (out, r, held) == ("win", 1.0, 1)


def test_short_side_mirrors():
    first = bar(T0, 100, 103.0, 99.8, 102.5)     # rallies through a 102 stop on a short
    out, r, held = B.simulate_trailing(flat(30), 10, "short", 100.0, 102.0, first_candle=first)
    assert (out, r, held) == ("loss", -1.0, 0)


# --------------------------------------------------------------------------- #
# _get_retry
# --------------------------------------------------------------------------- #
def test_get_retry_survives_transient_network_errors(monkeypatch):
    import asyncio
    calls = {"n": 0}

    async def flaky_get(base, path, params, cache_ttl=0):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("Network error calling klines: . Check your internet connection.")
        return [["ok", path, params]]

    async def no_sleep(_):
        return None

    monkeypatch.setattr(B.server, "_get", flaky_get)
    monkeypatch.setattr(B.asyncio, "sleep", no_sleep)
    out = asyncio.run(B._get_retry("/fapi/v1/klines", {"symbol": "BTCUSDT"}))
    assert out == [["ok", "/fapi/v1/klines", {"symbol": "BTCUSDT"}]]
    assert calls["n"] == 3


def test_get_retry_gives_up_after_the_last_attempt(monkeypatch):
    import asyncio
    import pytest

    async def always_fails(base, path, params, cache_ttl=0):
        raise RuntimeError("down")

    async def no_sleep(_):
        return None

    monkeypatch.setattr(B.server, "_get", always_fails)
    monkeypatch.setattr(B.asyncio, "sleep", no_sleep)
    with pytest.raises(RuntimeError):
        asyncio.run(B._get_retry("/fapi/v1/klines", {}, attempts=3))
