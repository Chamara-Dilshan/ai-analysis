# Case study: building a trading monitor you can trust

This project started as a data server for chatting about Bitcoin and became an always-on system that alerts,
manages trades and grades itself. Most of the work was not adding features. It was finding out which results were
real. Four episodes show how.

---

## 1. The backtest that was 96% wrong

**Situation.** By mid-September 2026 the continuation gate (enter on a fresh 4H break of structure in the
direction of the daily trend) had a backtest of **+307R** over 25 months, positive on all five coins. It had been
used to justify expanding from two coins to five and to pick the exit rule.

**What was wrong.** The replay recomputed 4H and daily structure once per *completed* candle. At 13:00 it was
evaluating the 12:00–16:00 candle using its final close, high and low, which are values that did not exist until
16:00. The live monitor sees the candle as Binance serves it mid-formation. So the backtest:

- only ever entered breaks that held until the close,
- placed the stop at a low that might not have printed yet, and
- credited the trade with price action from before the entry.

**How it was found.** A real paper trade disagreed with the replay. The live monitor's stop on a 2026-09-03 BTC
trade was $77,854.0; the backtest's stop for the same setup was $76,151.9, a level the live system never used.

**Fix.** `forming_candle()` rebuilds the candle under evaluation from the 1H bars printed so far, and the exit
simulation starts from the part of the entry candle *after* the entry (commit `de703e4`). The corrected replay
reproduces the real trade's stop **to the tick**, which is how we know this is a fix and not just a more
pessimistic model.

| Out of sample, 2023-01 → 2025-02 | Before | After |
|---|---|---|
| Continuation, 5 coins | +307.4R | **+12.8R** |
| Reversal, 3 coins | +61.6R | **+27.6R** |

Every earlier conclusion was re-tested on the corrected numbers. Some survived for different reasons than before;
the claim that continuation was positive on all five coins did not.

**Lesson.** A backtest is a model of the live system, and the only way to validate it is to compare it with a
real trade, number for number.

---

## 2. Ten ideas, ten rejections

With honest numbers, the obvious next step was to improve the strategy. Each idea was tested out of sample, and
from the fifth one onward the pass rule was written down before running it:

| Idea | Result |
|---|---|
| Filter by trade direction, session, zone type, zone depth (`b01d8d9`) | nothing held across windows |
| Funding-rate confluence (`e719620`) | −6.4R, sign flipped by coin |
| RSI-divergence confluence (`6289fc6`) | kept ~5% of trades and lost most of the edge |
| Structural stops instead of candle stops | **passed** on one window, **failed on all five coins** in the next |
| Trailing exits on reversal trades | won 1 of 3 windows |
| Dropping the London/NY session filter (`38e61e5`) | the added trades lost −16.8R |
| Entering on a CHoCH after a fresh daily flip (`44c7fdd`) | +0.02R per trade, a loss after fees |
| 3 open positions instead of 2 | won the trending window only |
| Daily/weekly loss breaks | earned less and made the worst drawdown *bigger* |
| A higher fee cap | raised the total while flipping the sign on two coins |

The structural-stop result is the one worth remembering. It passed every criterion on one window, and on the
next, never-used window it was **worse on every coin**. Shipping it would have been a regression with a good
spreadsheet behind it.

**Lesson.** Passing on one window is not evidence. The edge here lives in the structure gates and the exits;
filters mostly fit noise.

---

## 3. Deciding before looking

One idea, skipping continuation setups that would need more than 2% risk instead of sizing them up, came out
ahead in two of three windows by accident while answering a different question. Those windows had already been
used many times, so that proved nothing.

So the test was **pre-registered**: the pass rule, the harness and the window (2019-09 → 2021-01, never used by
the project) were written into [`05_preregistration_2pct_strict.md`](05_preregistration_2pct_strict.md) and
committed (`256c07d`) **before** any 2019 data was fetched. The git history proves the order.

Result: +$179 vs +$176, worst drop −$54 vs −$57. Both conditions passed, so it was adopted (`6ee4872`), with
the honest reading recorded next to it: a tie on profit, a gain in risk control. The untouched window is now
spent, and the document says so.

---

## 4. Silent bugs in production

The monitor never crashed. That was the problem: its failures looked like a quiet market. Fifteen defects were
found, most of them by reading the VM's state files after a blunt question from the user, not from the logs.

**The one with a measured cost** (`2b810fc`). When a valid setup arrived while two positions were already open,
it was correctly *not* alerted, but its de-duplication key was still written. An hour later a slot freed up,
every condition still held, and that key suppressed the alert. Price went straight to the 2R target.

**The class behind it** (`d438777`). The bug was "state written on a path that didn't complete". Searching for
that pattern found four more: paper trades saved before the Telegram send (so a retry journalled a trade twice),
the daily heartbeat marked as sent on a failed push, and position flags set before the message announcing them.
Now everything that records "the user was told" is written only after the send succeeds, and rolled back if it
fails.

**The habit that made the fixes count.** For every fix, the fix was reverted and the new tests re-run to confirm
they **fail** on the broken code. A regression test that passes either way proves nothing.

---

## 5. Operations

- **Hosting.** Binance blocks US IP addresses with HTTP 451, and GitHub Actions runners are in the US. An early
  scheduled workflow went green 19 times in a row while every run fetched nothing, because errors were caught per
  coin. The monitor moved to an Oracle Cloud VM in Mumbai. Lesson: never trust a green tick, read the log.
- **Deploys.** `update.sh` tests the candidate commit in a throwaway git worktree and moves the live checkout only
  if all tests pass. It was verified by deploying a deliberately broken commit, which was refused.
- **Concurrency.** The hourly run, the CLI and the Telegram bot all read-modify-write the same JSON files, so they
  share an `fcntl` lock. The bot saves Telegram's update offset *before* running a command, so a crash can't
  replay a `/took` and log a trade twice.
- **Self-grading.** The weekly scorecard compares the live record with every run of N consecutive backtest trades
  (sliding windows, not shuffling, so the real losing streaks are kept), with a stop rule fixed in code before
  the data arrives.

## Numbers

280 tests · 71% line coverage of the app code (the gates, journal, scorecard and dashboard are 86–99%; the backtester and a CSV importer pull it down) · lint-clean (ruff) · 48+ commits · running since August 2026.
