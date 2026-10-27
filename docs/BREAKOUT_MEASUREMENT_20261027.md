# Breakout lead-indicator measurement — 27 Oct 2026 (step 1 of 3, read-only)

Question: which signals would have warned 5–15 minutes **before** big NIFTY moves, so the
hourly forecast stops chasing breakouts (example: 27-Oct 14:25–14:40, price pinned under
22,700 with CE and PE walls both at 22,700, then an ~80-pt run through it).

Nothing on the server, engine or live branches was changed. All DBs were opened read-only
(`mode=ro`); the analysis script is `docs/breakout_measurement_20261027.py` (run on the
server with `nice -n 15`, venv python `-I`, no kadima imports).

## 1. Data actually available

| Source | Depth | Used for |
|---|---|---|
| `forecast.db history_candles` NIFTY 5-min | 2 years stored; last **60 trading days** used (31-Jul → 27-Oct), 4,391 bars, 9 bad bars dropped (e.g. 27-Oct 15:20 open 23,215) | events, squeeze |
| `kadima.db candles` NIFTY + BANKNIFTY 1-min | **6 days** (20–27 Oct) | spot, BANKNIFTY lead |
| `kadima.db oi_snapshots` NIFTY | **6 days**, ~30 s cadence, 11 strikes × CE/PE (±250 pts), current weekly expiry only | OI walls, ATM premiums |
| `forecast.db chain_wide`, `chain_eod` | **empty** | — |
| `forecast.db hourly_forecast_breadth` | 1,660 rows, **5 days** (21-Oct 11:34 → 27-Oct) | breadth |
| audit `gamma_score` / `gamma_activity_no_direction` (NIFTY) | 5,340 + 34,705 events, **44 trading days** (from 21-Aug) | gamma |
| NIFTY futures candles | 1 day each for two contracts (26-Oct token 48704, 27-Oct token 61471) — 24 h retention | not measurable |
| India VIX (token 99926017) / GIFT Nifty | no candles; no VIX/GIFT keys in `price_history.db` | not measurable |

## 2. Big-move events

Rule: any 15-min window with |close − open| ≥ 40 pts, or one 5-min candle with range ≥ 35 pts;
one event per 30 min. Event time T = **start** of the window (so lead times are conservative —
the 27-Oct breakout is logged at 14:25 because the 14:25–14:40 window moved +81).

* 226 events in 60 days (94 up / 132 down), median size 48 pts.
* **85 of 226 (38 %) start in the opening hour 09:15–10:15**, and **39 of the 59 events ≥ 60 pts**
  are opening-hour moves. No intraday indicator can see those (GIFT/VIX not stored).
* Last week was unusually busy: 7–9 events per day, so on the 6 live-data days an event starts
  within 15 min after **33 %** of all check times (60-day base rate: 11–14 %). Any indicator has
  to beat that base rate to count as a lead.

## 3. Method

For each event, every indicator is evaluated at T−15, T−10 and T−5 using only data up to that
moment. *Hit* = fires at any of the three. *FA* = share of quiet anchors (bar starts 09:45–15:10
with no event within ±30 min) where it fires at the same offsets. *Precision* = of all firings at
every 5-min check 09:30–15:10, the share followed by an event within 15 min (compare with the
base rate). Thresholds are simple; P90 thresholds were taken from the indicator's own distribution.

## 4. Results

| Indicator (rule) | Days | Events | Hit | Dir. correct | FA (quiet) | Precision vs base | Median lead |
|---|---|---|---|---|---|---|---|
| a1 Wall unwind: nearest CE wall above / PE wall below loses ≥15 % OI in 10 min | 6 | 39 | 2.6 % | 1/1 | 0 % (n=39) | 1 firing in 6 days | 10 min |
| a2 Wall pin: max-OI CE strike = max-OI PE strike, spot within 0.2 % | 6 | 39 | 10.3 % | n/a | 0 % (n=39) | **47 %** vs 33 % (15 firings) | 15 min |
| b Gamma score ≥ 50 (NIFTY), CE=up / PE=down | 44 | 187 | 11.2 % | 10/21 events, 20/34 firings | 5.4 % (n=1,598) | 21 % vs 14 % (163 firings) | 10 min |
| b' Gamma ≥ 40 or no-direction raw ≥ 40 | 44 | 187 | 27.3 % | 28/50 | 17.2 % | 20 % vs 14 % | 15 min |
| c Premium lead: ATM (ΔCE − ΔPE) − Δspot over 5 min, \|resid\| ≥ 19.9 pts (P90) | 6 | 39 | 15.4 % | **6/6** events, 6/8 firings | 0 % (n=39) | **73 %** vs 33 % (11 firings) | 12.5 min |
| d Squeeze: 6-bar / 24-bar mean range ≤ day-so-far P20 | 60 | 132* | 22.7 % | n/a | 14.5 % (n=2,167) | 15 % vs 11 % (326 firings) | 15 min |
| e Breadth jump: \|Δbreadth 10 min\| ≥ 0.835 (P90) | 5 | 35 | 22.9 % | 5/8 | 12.8 % | 25 % vs 32 % — below base | 15 min |
| e' Breadth divergence: breadth moves ≥ 0.58 while NIFTY < 0.05 % | 5 | 35 | 8.6 % | 3/3 | 2.6 % | 33 % vs 32 % — no edge | 5 min |
| f BANKNIFTY lead: \|BN 5m ret − NIFTY 5m ret\| ≥ 0.075 % (P90) | 6 | 39 | 17.9 % | 4/7 | 10.3 % | 26 % vs 33 % — below base | 15 min |
| g Futures premium / volume | 1 day per contract | — | — | — | — | not measurable | — |
| h Opening: GIFT Nifty / India VIX | none stored | 85 opening events | — | — | — | not measurable | — |

\* squeeze needs 7 bars, so it cannot cover events before ~09:50.

**Combined score** (count of a1, a2, b, c, d, e, f firing at a check; 5 days, 44 events):

| Cut-off | Hit | FA (quiet, n=39) | Precision vs base 33 % |
|---|---|---|---|
| ≥ 1 | 68 % | 54 % | 35 % — no edge |
| ≥ 2 | 18 % | 18 % | 27 % — worse than base |
| ≥ 3 | 4.5 % | 0 % | 2 firings only |

Adding signals together mostly adds noise. The two that carry the edge are better used on their own.

## 5. The 27-Oct 14:25 breakout, step by step

| Time | Spot | CE wall above (OI) | PE wall below (OI) | What fired |
|---|---|---|---|---|
| 14:15 | 22,659 | 22,700 (31.2 M) | 22,600 (27.2 M); max-PE also 22,700 | **wall pin**; gamma said PE/down |
| 14:20 | 22,672 | 22,700 (31.7 M) | 22,600 (25.7 M) | breadth jump, up |
| 14:25 | 22,659 | 22,700 (33.4 M) | 22,600 (27.3 M) | gamma said PE/down |
| 14:30 | 22,673 | 22,700 (36.0 M) | 22,600 (27.6 M) | — |
| 14:35 | 22,680 | 22,700 (34.7 M) | 22,600 (26.1 M) | — |
| 14:40 | 22,738 | 22,800 (27.9 M) | 22,700 (26.0 M) | (move under way) |
| 14:45 | 22,722 | 22,800 | 22,700 (29.7 M) | premium lead, up (after the move) |

CE writers were *adding* at 22,700 right up to 14:30 (31 M → 36 M). The wall gave way in one candle,
with no 15 % unwinding beforehand, so the Bull Shield unwind rule could not have warned. The pin at
14:15 and the breadth jump at 14:20 were the only early hints. Gamma pointed the wrong way.

## 6. Conclusions

* **Nothing leads reliably.** The best signals catch only 10–15 % of big moves, and most big moves
  (38 % overall, two-thirds of the ≥ 60-pt ones) happen in the opening hour, where no stored data exists.
* **Worth carrying to shadow mode** (small samples, 6 days — treat as hypotheses):
  * **c ATM premium lead** — 11 firings, 73 % followed by a big move, direction 6/8; about 12 min warning.
  * **a2 Wall pin (CE and PE walls at the same strike, spot within 0.2 %)** — 47 % vs 33 % base,
    direction-less ("breakout risk high"); about 15 min warning. This is exactly the 27-Oct setup.
  * **b Gamma ≥ 50** — 1.5× base rate over 44 days, but its direction is a coin flip.
    Use it as a "risk up" flag only, never for direction.
  * **d Squeeze** — weak (1.4× base) but measured over 60 days; low weight, no direction.
* **Do not use:** BANKNIFTY lead and breadth jump/divergence (at or below base rate);
  15 %/10-min wall unwinding (fired once in 6 days — walls break suddenly rather than unwinding first).
* **Not measurable:** futures basis/volume (24 h retention), GIFT Nifty and India VIX (not stored),
  wider chain history (`chain_wide` empty, `oi_snapshots` only ±250 pts and 6 days).

## 7. Suggested step 2 (shadow only)

A panel-only "breakout-risk meter", logged to audit, with **no effect on the forecast, entries or stops**,
for 2–3 sessions:

* Level = premium-lead (directional) + wall-pin + gamma ≥ 50 + squeeze (each 0/1; direction only from premium lead).
* Record every reading, and score it after the session against the same event rule, so step 3 has
  out-of-sample hit, false-alarm and lead numbers.
* Separately (data only, no logic): keep NIFTY futures 1-min and India VIX candles past 24 h, and capture
  GIFT Nifty pre-open, so the opening hour (where most big moves are) can be measured later.
