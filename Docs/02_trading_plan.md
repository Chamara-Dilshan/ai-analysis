# ₿ CHAMARA'S PERSONAL BITCOIN TRADING PLAN
### Day + Swing Trading | $200 Capital | Elliott Wave + ICT + SMC

---

## TRADER PROFILE
- **Name:** Chamara
- **Capital:** $200 USD
- **Experience:** Intermediate
- **Style:** Day Trading + Swing Trading
- **Platform:** Binance Futures — BTCUSDT Perpetual
- **Strategy:** SMC-primary (ICT/SMC), Elliott Wave as context only
- **Weaknesses to guard:** SL discipline, leverage, FOMO, emotion

> **MISSION: Grow $200 slowly through discipline — not prediction, not signals, not hope.**

---

## SECTION 0 — METHOD PRIORITY (read first)
Three methods mixed = three excuses to justify any trade. That is a FOMO trap. So there is a strict order:

| Method | Role | Use it to... |
|---|---|---|
| **ICT / SMC** | **PRIMARY — this is what you trade & test** | Find the setup: trend structure, Order Blocks, FVGs, liquidity, BOS/CHoCH. Exact entry, stop, target. |
| **Elliott Wave** | **CONTEXT ONLY — never an entry trigger** | Get a rough sense: are we trending or correcting? Nothing more. |

**Note:** ICT and SMC are the SAME family — ICT is the original (Order Blocks, FVGs, liquidity), SMC is the structure layer (BOS, CHoCH). They work together; that is why your setups use both.

**Rules:**
- Every trade must be a valid SMC setup. No SMC setup = no trade — even if "the wave count looks good."
- Elliott Wave may support a trade but can NEVER be the reason for one.
- Master ONE method first. One method traded well beats three methods mixed.

**Two entry styles (added 2026-08-29):** an SMC setup comes in two forms, and they are graded by two different checklists (Section 3A / 3B) — pick the one that matches what price is actually doing, never mix their rules.
- **Reversal** — price pulls back into a demand/supply zone (Order Block or FVG) after a change of character (CHoCH). Tight stop just beyond the zone. This is the original method below.
- **Continuation** — price is already trending and just broke a fresh high/low WITH that trend (a BOS, not a CHoCH). Enter on the break itself, no pullback wait — the wait is exactly what missed most of the 2026-08-19→08-28 rally. Wider stop, at the breakout candle's own low/high.

**Which style is working depends on the market's regime (corrected out-of-sample data, 2026-09-16).** Earlier versions of this plan quoted much larger continuation figures; those came from a look-ahead bug in the backtester, now fixed. The honest numbers:

| net R, out-of-sample | trending 2021-23 | choppy 2023-25 |
| --- | --- | --- |
| Continuation (5 symbols) | **+115.4R** | +12.8R |
| Reversal (BTC/ETH/BNB) | −10.6R | **+27.6R** |

Neither style is better than the other — they are mirror images. In a sustained trend, breakouts keep running and pullback entries get run over. In chop, breakouts fail and price reverts to its zones. That is the whole reason to keep both: one of them is usually suited to what the market is doing. Be honest about which regime you are in before deciding how hard to push a setup.

---

## SECTION 1 — MINDSET & PHILOSOPHY
Trading is a business, not gambling. Your biggest asset is **discipline**, not the ability to predict. The past loss was expensive tuition — now use it wisely.

**The 7 Mindset Rules**
1. The market is ALWAYS right. Your opinion means nothing against price.
2. A loss is tuition — not failure. Learn and continue.
3. Protect capital FIRST. Profit comes second, always.
4. No trade is always better than a bad trade.
5. Revenge trading = account destruction. Walk away after losses.
6. Consistency over big wins. Small daily edge beats monthly gambling.
7. **Hope is NOT a strategy. A Stop Loss IS.**

---

## SECTION 2 — CAPITAL MANAGEMENT
With $200, every dollar still matters. One trade without a stop can end everything.

| Per-Trade Risk | Drawdown Rule | Max Open Trades |
|---|---|---|
| **MAX 2% ($4)** | **Review at −$60 · paper-only at −$100** (see Section 7) | **MAX 2** |

**Position Size Formula**
```
Position Value = (Capital × Risk%) / Stop-Loss Distance %
Example: $200 × 1% = $2 risk;  SL = 0.5%  →  $2 / 0.005 = $400 position
At 3x leverage: margin ≈ $400 / 3 ≈ $133
```

**⚠️ Account-size warning:** On $200 this formula can still produce positions below Binance's minimum — **0.001 BTC _and_ $100 notional**, whichever is larger (an exchange rule — it does not scale with your capital) — OR force you to break your risk rule to meet it. If the math doesn't fit cleanly — **skip the trade.** Don't force size. (The `calculate_position_size` tool flags this automatically.)

**No exceptions to 2% (since 2026-10-21).** Continuation trades used to be sized UP when their breakout-candle stop was too wide for 2% to reach Binance's $100 minimum (the 2026-08-29 exception). That was tested against simply skipping those setups — pre-registered, on 2019–2020 data never used before (Docs/05) — and skipping won: +$179 vs +$176, worst drop −$54 vs −$57, and it had already won 2 of 3 earlier periods. So now every trade, reversal or continuation, risks at most 2%. A stop wider than about 4% of price means the setup is skipped, and the monitor logs it as `risk_ok` failing.

**Leverage by condition**
- Strong trend + clean setup → max 5x
- Ranging / unclear → max 3x
- News / weekend → max 2x
- Confused / no clear signal → **NO TRADE**

---

## SECTION 3A — ENTRY RULES: REVERSAL (FOMO FIX)
A pullback into a zone after a change of character (CHoCH). **ALL 7 must be YES.** Even one NO = NO TRADE.

- [ ] HTF (Daily + 4H) trend confirmed — trade WITH the trend only
- [ ] Minimum 3 confluences align (Elliott Wave + ICT level + SMC structure)
- [ ] Entry is at an Order Block, FVG, or key SMC zone — NOT mid-range
- [ ] Kill Zone timing correct (London 07–09 UTC / NY 13–15 UTC)
- [ ] Stop Loss level DEFINED and SET before entry — never after
- [ ] Risk:Reward minimum 1:2 (prefer 1:3+)
- [ ] No high-impact news within 30 min — check the calendar first

> **If any answer is NO — close the chart and walk away. No exceptions.**

---

## SECTION 3B — ENTRY RULES: CONTINUATION (added 2026-08-29)
Price is already trending and just broke a fresh high/low WITH it. **ALL 6 must be YES.** No pullback wait — that wait is the whole point of this checklist existing.

- [ ] Daily trend already clearly established (up or down)
- [ ] A FRESH 4H BOS just confirmed, SAME direction as the Daily trend — a CHoCH is a reversal signal, use 3A instead, not this one
- [ ] The breakout candle closed within the last ~4 hours — older than that is stale, skip it
- [ ] Stop Loss = the breakout candle's own low (long) or high (short) — the structurally-correct level, not shrunk to fit 2% risk (see Section 2's exception)
- [ ] No high-impact news within 30 min — check the calendar first
- [ ] Fees still a reasonable share of the risk at this stop width — the sizing tool checks this automatically

> No Kill Zone requirement here on purpose — a 4H candle closes on its own clock (00/04/08/12/16/20 UTC), and waiting for the next London/NY window just re-creates the "confirmed but already stale" gap this checklist exists to close.

---

## SECTION 4 — STOP LOSS & TAKE PROFIT

| DO — Always | NEVER — Zero Exceptions |
|---|---|
| Set SL before entry | Trade without a Stop Loss |
| Reversal: SL beyond the Order Block. Continuation: SL beyond the breakout candle. | Move SL against your position |
| Use min 1:2 RR | Hope price reverses back |
| Move SL to breakeven at TP1 | Add to a losing position |
| Accept small losses quickly | Skip SL because "the chart looks good" |

**Take Profit — Reversal (3A)**
- TP1 (50%) at 1:2 → move SL to breakeven
- TP2 (30%) at 1:3 → let it run
- TP3 (20%) at 1:4+ → trail or target a major level

**Take Profit — Continuation (3B), updated 2026-08-29**
- Breakeven stop at 1R (same as always)
- Half off at 1:2 → **the rest TRAILS behind each newly-confirmed 4H swing, no fixed final target**
- Trailing beats a fixed-1:2-and-done exit overall on corrected out-of-sample data — **+128.2R vs +49.1R** across two windows — so it stays the default. The old ETH long that closed at 2R when 6R+ was briefly available (2026-08-12) is exactly the mistake trailing fixes.
- **But it is not unconditional, and the earlier "+0.68R vs +0.50R" figures here were bug-inflated.** Trailing's advantage comes almost entirely from trending markets (+115.4R vs +27.9R). In the choppy window the fixed 1:2 actually won (+21.2R vs +12.8R) — trailing hands back in chop what it earns in trends.
- So: default to trailing, but if price has been whipsawing rather than trending, closing the full position at 1:2 is a reasonable judgement call, not a mistake.

**Take Profit — Reversal (3A): keep the full close at 1:2.** Re-tested on corrected data across three windows: fixed won two of them (+27.6R and +3.0R), trailing won one (+22.6R in the trending window). Note the old note claiming "trailing reversal trades lost money in testing" was **wrong** — trailing was positive in all three windows, just not better. The fixed 1:2 stands on the evidence, not on that claim.

> **Rule: never let a winning trade become a loser. SL to entry after TP1.**

---

## SECTION 5 — ANALYSIS FRAMEWORK (TOP-DOWN)
Always go highest → lowest timeframe. Never trade against the Daily trend.

1. **Weekly / Daily** — macro trend. Bullish = HH/HL. Bearish = LH/LL.
2. **4H Structure** — Order Blocks, FVGs, BOS, CHoCH; premium vs discount.
3. **Entry** — two paths, see Section 3A/3B: **reversal** waits for a 1H BOS/CHoCH and enters only at an OB/FVG inside the 4H zone; **continuation** enters on a fresh 4H BOS itself, same direction as the Daily trend, no pullback wait.
4. **Elliott Wave** — where are we? Wave 3 = best. Wave 5 = caution. ABC = counter-trend only.
5. **Kill Zones** — London 07–09 UTC / NY 13–15 UTC.
6. **Funding + Fear** — deeply negative funding = bullish pressure; Extreme Fear <20 = possible bottom.

> Run this with your coach — **you** read it, the coach checks it. (The coach can pull all three timeframes at once via `get_top_down`.)

---

## SECTION 6 — DAILY ROUTINE (UTC)
| Time | Session | Action |
|---|---|---|
| 06:00–07:00 | Prep | News check, Daily + 4H analysis, mark levels, write plan |
| 07:00–09:00 | London | Trade window — full 7-point checklist |
| 09:00–12:00 | Rest | Monitor only. No new entries. |
| 12:00–13:00 | NY Prep | Review, update journal, mark NY levels |
| 13:00–15:00 | NY | Trade window — full checklist |
| 15:00–17:00 | Afternoon | Close day trades, monitor swings |
| 17:00–18:00 | Close | Complete journal, plan tomorrow |
| 18:00+ | OFF | No charts. Rest is part of the job. |

> This routine's trade windows are a Section 3A (reversal) rule — continuation setups (3B) have no Kill Zone requirement and can fire outside these hours. The "no charts after 18:00" rest rule still matters for your own sanity either way; the automated monitor watches continuation setups even while you're off.

---

## SECTION 7 — EMERGENCY RULES
**Drawdown rule (replaces the old daily $10 / weekly $20 / monthly $40 breaks — 2026-10-20).**
Drawdown = how far the account sits below its highest point so far.
- **Down $60 from the high (30%) → REVIEW.** Keep trading, but check every recent trade against its alert: same entry, same stop, same exit? If my trades drifted from the system, fix that first.
- **Down $100 from the high (50%) → STOP real money.** Paper only until the paper record makes a new high again. No tested period ever fell this far, so reaching it means something is actually broken.
- The monitor tracks this on the paper record and sends a Telegram at both lines.

**Expect this — it is normal, not failure.** Backtested on three different markets (2021–2026, both setups, max 2 open, fees included):
- Worst drop from a high: **$78–$91 (about 40–45%)**.
- Time spent below the last high: **8 to 13 months** at a stretch.
- Losing streaks of **20+ trades** happened. Win rate is about 29%; the winners are bigger than the losers.

**Why the calendar limits were removed:** tested, they did not protect the account. Taking a 3-day or 1-week break after a bad week/month earned *less* ($374 vs $395) and in one period made the worst drop *bigger* (−$141 vs −$89) — the breaks mostly skipped the winners that come after losses. The real danger is a slow bleed over months, which no daily or weekly limit ever sees.

**What a bad DAY still means:** if I've lost 2 trades in a day and I feel the urge to "win it back", I close the platform. That is a mood rule, not a money rule — and it still matters.

**Revenge-trading warning — STOP the moment you think:**
- "I need to recover this loss right now" → close the platform
- "Just one more trade to get it back" → close the platform
- "I'll double my size this time" → close the platform
- You feel angry, frustrated, or desperate → close the platform

> When in doubt, do nothing. The market will be there tomorrow.

---

## SECTION 8 — GROWTH ROADMAP (honest)
| Period | Real Goal | Focus |
|---|---|---|
| Month 1 | Survive + learn | Paper-trade setups first, journal daily |
| Month 2 | Break even after fees | Consistency, one clean setup at a time |
| Month 3 | Small positive | 50%+ win rate, refine entries |
| Month 6 | Steady, provable edge | Only scale when discipline is proven |

> This is a marathon. Never rush to recover a loss.

---

## SECTION 9 — TRADE JOURNAL (see file 05)
Complete one for EVERY trade. No exceptions. Your journal is your most valuable tool.

---

## SECTION 10 — CHAMARA'S COMMITMENT
I, Chamara, commit to:
- ALWAYS set a Stop Loss before entering — no exceptions.
- NEVER risk more than 2% on a single trade. No exceptions — setups that would need more are skipped.
- NEVER use more than 5x leverage.
- ALWAYS complete my journal after every trade.
- Sit through normal drawdowns (up to ~$90 has happened in testing) without quitting or changing the rules mid-drop.
- REVIEW at −$60 from my high; STOP real money at −$100 and go back to paper.
- NEVER enter out of FOMO, impatience, or emotion.
- Treat every loss as a lesson — never a reason for revenge.
- Use my coach to **pressure-test my own analysis** — and make my own decisions.
- Be patient. Be consistent. Grow slowly and safely.

Signature: _________________________   Date: __________

> "The past loss was the price of my education. Now I trade with rules, patience, and discipline — not with hope."
