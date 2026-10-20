# Pre-registration: "2% strict" continuation sizing

Written and committed **before** the test below was run. The point of this file
is that the pass/fail rule cannot be bent after seeing the answer.

## The idea
Today a continuation setup whose stop is too wide for 2% risk to reach
Binance's $100 minimum is **sized up** to whatever risk % clears it (often
3–4%, flagged above ~6%). "2% strict" instead **skips** any continuation setup
that would need more than 2% risk (stop wider than 4% of price). Reversal is
unchanged — it already skips such setups.

## Why it is being tested
Found by accident on 2026-10-20 in the portfolio replay (both gates, cap 2,
$200) that was built to answer a different question:

| window | live (size up) | 2% strict |
|---|---|---|
| trending 2021-01 → 2023-01 | +$198, maxDD −$89 | +$376, maxDD −$79 |
| choppy 2023-01 → 2025-02 | +$160, maxDD −$78 | +$175, maxDD −$87 |
| recent 2025-02 → 2026-10 | +$37, maxDD −$91 | +$26, maxDD −$77 |

Better in 2 of 3 windows, but not pre-registered, and those windows have been
used for many earlier tests. So it gets one test on data this project has
never touched.

## The test
- **Window:** 2019-09-01 → 2021-01-01, Binance USDT-M futures. Never used by
  this project. Symbols listed later than the start simply contribute from
  their listing date.
- **Harness:** current `backtest.py` (look-ahead fixed), live config
  (continuation + trailing on 5 symbols, reversal + fixed on BTC/ETH/BNB),
  then the same chronological portfolio replay: max 2 open, $200,
  non-compounding, P&L booked at exit.
- **Compared:** live sizing vs 2% strict, nothing else changed.

## Pass rule (both required)
1. 2% strict earns **more USD** than live sizing on this window, AND
2. 2% strict's **max drawdown is no worse than live's by more than $10**.

If it passes: adopt 2% strict in the live monitor. If it fails on either:
reject, keep live sizing, and do not re-test this idea on any window.

## Result
Run 2026-10-21 on 2019-09-01 → 2021-01-01 (318 raw trades; SOL listed Sep 2020, BNB/XRP early 2020).

| | live (size up) | 2% strict |
|---|---|---|
| trades taken | 229 | 215 |
| total R | +46.8R | +44.7R |
| **USD** | **+$176** | **+$179** |
| **max drawdown** | **−$57** | **−$54** |
| worst day / week / month | −$13 / −$20 / −$38 | −$9 / −$20 / −$36 |
| days losing > $10 | 1 | 0 |

1. More USD: +$179 > +$176 — **pass** (by $3).
2. Drawdown no more than $10 worse: −$54 vs −$57, i.e. $3 *better* — **pass**.

**PASSED. Adopted** in the live continuation gate as `risk_ok`, in the same commit as this result.

Honest reading: on profit this is a tie — $3 over 15 months is noise. What it
really buys is risk control: no trade above the plan's 2%, a smaller worst
drop, and fewer bad days, without costing money in 3 of 4 periods tested.
That, plus the rule having been set in advance, is why it is adopted rather
than argued about. The 2019–2020 window is now used; it cannot be the
untouched test for any future idea.
