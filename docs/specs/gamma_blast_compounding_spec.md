# Gamma Blast Compounding Spec

Oct 3, 2026 · @MANCHALA KUSUMAKAR

## Status and purpose

Draft for review, not locked. No code until Kusumakar confirms.

Revised 13 Oct 2026: added the 91-step goal with time estimates, a capacity ceiling, a performance kill switch and a backtest phase before any code change.

A new KADIMA mode that trades only Gamma Blast signals and compounds a ₹1,00,000 starting capital. Each trade uses the full compounded capital, but loss per trade is capped by a tight stop rather than by trading smaller.

## Evidence basis

Manual testing of 20–30 trades with gamma score above 50 showed premium gains of more than 30% within 2–5 minutes.

- Failure count and loss size of the non-winning trades: not yet recorded
- Results were observed manually; live fills through KADIMA (with EC2-to-Angel latency) are not yet tested
- This sample meets the 30-occurrence guideline only at its upper end

## Capital and compounding rules

Capital base = ₹1,00,000 + all realised P&L of this mode, recalculated after every closed trade.

1. Trade 1 deploys up to ₹1,00,000.
2. Trade 2 deploys ₹1,00,000 + Trade 1's realised P&L, and so on.
3. After a loss, the next trade automatically uses the reduced capital.
4. Lots = floor(capital base ÷ (entry premium × lot size)); leftover cash stays idle.
5. One open position at a time in this mode.
6. Capital is tracked as a separate sleeve, so it does not mix with the main engine's tiered sizing.

## Entry rules

All conditions must hold at the same time.

| Rule | Value |
| --- | --- |
| Gamma score | Above 50 |
| Projected return | ≥10% (delta × expected move) |
| Strike | Existing tradeability ranking: near ATM, volume, tight spread, delta responsiveness |
| Order type | LIMIT, CE/PE buy only |
| Signal cutoff | 15:10 IST |
| Macro filter | No entry during active FOMC/RBI/major US data windows |
| Open positions | None already open in this mode |

## Exit rules

A failed blast fails fast, so exits are tighter than the current 30% flat stop.

| Exit | Rule (proposed) |
| --- | --- |
| Hard stop | −10% of entry premium, also placed as a broker-side STOPLOSS\_LIMIT disaster stop |
| Time stop | Exit if premium has not moved favourably within 3 minutes of fill |
| Trailing stop | Activates at +10%; exit if 30% of peak gain is given back (peak +30% → exit near +21%) |
| EOD | Existing force-close and watchdog timings apply |

## Risk guards

Maximum loss per trade is about 10% of the capital base (₹10,000 at the start).

- **Daily loss limit:** at ₹1,00,000, one full stop-out reaches the existing ₹10,000 limit and stops the day. Decided: keep the limit fixed at ₹10,000. Once capital grows above ₹1,00,000, a single −10% stop-out will exceed ₹10,000 before the limit blocks further entries.
- **Loss streak pause:** after 2 consecutive losses, no new entries that day.
- **Fill check:** skip entry if the LIMIT order is not filled within a few seconds, since a stale fill in a 2–5 minute move erodes most of the edge.
- **Liquidity cap:** order quantity limited to a set share of the strike's recent traded volume, so larger compounded orders still fill cleanly.
- **Performance kill switch:** if the win rate over the last 20 trades falls below the backtested level, the mode pauses itself and alerts. It resumes only on Kusumakar's confirmation.
- **No martingale:** size never increases after a loss, and there is no averaging down.

## Math and expected outcomes

With a −10% stop, full-capital compounding breaks even at about a 53% win rate even if every win is only +10%.

**Break-even win rate (full-capital compounding)**

| Average win | Stop loss | Break-even win rate |
| --- | --- | --- |
| +10% | −30% | \~79% |
| +10% | −10% | \~53% |
| +20% | −10% | \~37% |
| +30% | −10% | \~29% |

**Example sequences from ₹1,00,000**

| Sequence | Ending capital |
| --- | --- |
| 10 wins at +10% | ₹2,59,374 |
| 5 wins at +10%, then 1 loss at −10% | ₹1,44,946 |
| 5 wins at +10%, then 1 loss at −30% (current stop) | ₹1,12,736 |

## Goal: the 91-step target, no time limit

The target is the growth of 91 consecutive +10% trades: ₹1,00,000 to about ₹58.4 crore (1.1^91 ≈ 5,844×). There is no deadline. The mode does not try to win 91 in a row. At a 90% win rate the chance of that is about 0.007%, and at 95% about 1%. Instead it aims for the same ending capital with losses included, using the −10% stop.

| Win rate | Trades needed (+10% win / −10% stop) | Time at \~1 qualifying setup per trading day |
| --- | --- | --- |
| 90% | \~115 | \~5–6 months |
| 80% | \~157 | \~7–8 months |
| 70% | \~247 | \~1 year |
| 60% | \~577 | \~2.3 years |
| \~53% or below | Never reached | — |

Figures are before brokerage, STT and slippage, which add trades. Setup frequency is not yet measured; at one setup every two days, double the times. The win rate and frequency in this table are assumptions until Phase 0 replaces them with measured values.

**Design principles**

- **Selectivity over frequency:** with no deadline, only the highest-tier setups are taken. Days with zero trades are acceptable.
- **Measured win rate:** the win rate comes from the Phase 0 backtest and live 1-lot results, not from the manual sample.
- **Survivable losses:** a loss adds a few trades to the journey; the design must never let one trade end it.

## Capacity ceiling

Compounding at 10% per trade cannot continue indefinitely. As order size grows, a single strike cannot absorb the order without moving the premium, exchange freeze limits split the order, and slippage takes the edge. Tax on F&O profits also reduces what compounds.

- Reaching ₹1 crore takes about 49 winning steps, at order sizes that fill easily.
- The remaining \~42 steps, from ₹1 crore to ₹58.4 crore, happen at sizes where the ceiling is likely to bind. This stretch will be the slowest and may not be reachable at a full 10% per trade.

**Rule (proposed):** the maximum order per strike is set by the liquidity cap in Risk guards. Once the capital base exceeds what can be deployed within that cap, the excess is not compounded. Whether it is withdrawn or spread across strikes and indices is an open decision.

## Rollout plan

Three phases, each gated on the previous one's numbers.

1. **Phase 0, backtest (before any code change):** run the exact entry and exit rules over `price_history.db` and measure how often the premium reaches +10% before −10%, with a realistic fill delay for EC2-to-Angel latency. Count qualifying setups per day from the audit logs at `/var/lib/kadima/audit/`. The measured win rate and setup frequency replace the assumptions in the Goal table and set the kill-switch level. This is offline analysis and does not change the decision to go live at 1 lot.
2. **Live, 1 lot (decided; log-only phase skipped):** real orders at 1 lot to measure win rate, fill quality and slippage, while every score >50 signal is also logged with entry, 5-minute peak and outcome. Gate: win rate above 55% with the −10% stop over at least 20 live trades.
3. **Full compounding:** switch to capital-base sizing from ₹1,00,000.

## Open decisions

- [x] Hard stop: −10%
- [x] Daily loss limit: fixed ₹10,000
- [x] Capital: separate ₹1,00,000 sleeve, running alongside the main engine
- [x] Rollout: go live at 1 lot, no log-only phase
- [ ] How many of the 20–30 manual trades failed, and how large were the losses?
- [ ] Time stop: 3 minutes, or 2?
- [ ] Trailing giveback: 30% of peak gain, or tighter?
- [ ] Liquidity cap: what share of the strike's traded volume is the maximum order size?
- [ ] Entry threshold: gamma score above 50 (this spec), 60 (current Celestial setting), or top tier only?
- [ ] Capacity ceiling: once reached, withdraw the excess or spread it across strikes and indices?
- [ ] Kill-switch level: the backtested win rate, or a fixed floor such as 55%?
