# KADIMA — Continuous Data Engine

## Celestial draft (not a live release)

This branch adds the [Celestial control plane and Top 20 dashboard](docs/celestial_release.md)
on top of PR #1. The rollout flag defaults off; legacy execution remains unchanged
until deliberately switched. Celestial auto-entry defaults paused. A verified
per-symbol news collector, coverage validation, server preflight and browser QA
remain release requirements. Do not interpret offline tests as live readiness.

Tick-by-tick collection across NIFTY, BANKNIFTY, SENSEX and MIDCPNIFTY, feeding
a Bull Shield v2.1 scoring layer, four-index confirmation, strike ranking and
Telegram alerts.

**This package includes live order execution.** The deployed configuration controls
whether broker orders are enabled; check it before starting the engine.

For the reviewed baseline-to-release update, use [the release instructions](docs/release_readiness.md).
The consolidated `executor_fix/install.py` preserves deployed configuration and
installs the complete backend dependency set.

---

## What this covers from your audit

| Audit row | Status here |
|---|---|
| Tick-by-tick feed | `feed.py` — SmartWebSocketV2, snap-quote mode |
| Automatic reconnection | `feed.py` — watchdog, backoff, session re-login |
| Tick storage | `storage.py` — SQLite WAL |
| 1/3/5-minute candles | `candles.py` |
| Technical indicators | `indicators.py` — VWAP, EMA20/50, MA55, RSI, Ichimoku |
| Option-chain changes | `option_chain.py` — OI, PCR, max pain, walls |
| Greeks (SENSEX gap) | `greeks.py` — Black-Scholes + IV solver |
| Four-index confirmation | `engine.py` — weighted vote |
| Best-strike selection | `strike_selector.py` |
| Immediate alerts | `alerts.py` — Telegram, debounced |
| Emergency shutdown | `killswitch.py` |
| Audit log | `audit.py` — append-only JSONL, fsync'd |
| Monitoring schedule | `run.py` — pre-market wait, 15:30 shutdown |

Execution now includes durable order intents, confirmed fills, partial-fill
reconciliation and position protection. See [executor safeguards](docs/executor_safeguards.md).

---

## Install

```bash
sudo mkdir -p /var/lib/kadima && sudo chown $USER /var/lib/kadima
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

Credentials go in the environment, never in the repo:

```bash
export ANGEL_API_KEY=...
export ANGEL_CLIENT_ID=...
export ANGEL_PASSWORD=...        # MPIN
export ANGEL_TOTP_SECRET=...     # base32 seed, not the 6-digit code
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
export KADIMA_DATA_DIR=/var/lib/kadima
```

## Run

```bash
python run.py --preflight     # do this the evening before, not at 09:14
python run.py
pm2 start run.py --name kadima --interpreter ./venv/bin/python3
```

Kill switch:

```bash
touch /var/lib/kadima/KILL     # engine halts within 5 seconds
rm /var/lib/kadima/KILL        # release
```

---

## The bridge to ChatGPT / Claude

Two read-only endpoints, served on 127.0.0.1:8080 behind your existing Nginx.

```
GET /state     current picture, four indices, ~40 lines JSON
GET /session   today's signals + outcomes at a fixed horizon
POST /journal/{id}   your 16:00 note (the only write, and only your words)
```

Auth is a bearer token in `KADIMA_API_TOKEN`. **Generate a new one** — do not
reuse the key your audit flagged as exposed.

```bash
export KADIMA_API_TOKEN=$(openssl rand -hex 32)
```

Point a ChatGPT Action at `openapi.yaml` (edit the server URL first). Then:

- **During the session** — ask "what's KADIMA showing" → it fetches `/state`
  and reads the full checklist back to you.
- **Every 20–30 min while at the branch** — a ChatGPT Scheduled Task calling
  `getState`, so something reaches you without you asking.
- **At 16:00** — `prompts/session_review.md` plus `/session`.

`/state` ships an `unavailable` block naming the checklist items that cannot
be computed from SmartAPI (iceberg orders, dealer gamma, absorption, retail
traps, intraday FII/DII). This is deliberate: an LLM handed a gap will fill
it fluently, and a fabricated input carries the same arithmetic weight as a
real one. Naming the gaps is what stops that.

`/session` ships a `review_guardrail` string instructing the reviewer to
refuse weight recommendations below 30 resolved occurrences of a trigger.
Keep it. Without it you will get confident advice built on 20 samples.

---

## Bring-up order — do not skip step 2

1. **Verify tokens.** The index tokens in `config.py` are the ones that change
   most often. Check them against the downloaded scrip master before day one.

2. **Verify candles for a full session.**
   ```bash
   python tools/verify_candles.py NIFTY 2026-08-06
   ```
   This diffs your bars against Angel One's own. Until mismatches are **zero**,
   every indicator, every score and every signal downstream is built on sand.
   This is the single highest-value step in the whole build and the one most
   likely to get skipped.

3. **Run alert-only for two weeks.** Engine live on real data, alerts to
   Telegram, you trade manually as you do today. Log what the engine said
   against what actually happened.

4. **Then read the tracker.** `signals` and `outcomes` tables in
   `kadima.db`. If the CONFIRMED tier isn't beating your own discretion, the
   weights in `engine.py:WEIGHTS` are wrong — and no amount of further
   infrastructure fixes that.

```bash
python3 tools/test_offline.py    # pure-logic tests, no broker needed
```

---

## Two design constraints worth knowing

**OI is polled, not streamed.** Angel One's WebSocket carries price and volume,
not option OI. OI comes from REST `getMarketData(FULL)` every 30s. Your
"wall loses >15% in 10 minutes" rule therefore sees ~20 samples per window, not
thousands. If samples are missed, the window is marked degraded and velocity
alerts are suppressed rather than computed on partial data.

**Volume is cumulative.** `volume_trade_for_the_day` is a running total.
Candle volume is the difference between successive values. Treating it as
per-tick volume inflates it enormously and would make your 15%-weight
volume-spike trigger fire on almost every candle. Handled in `candles.py`, and
tested — but check it yourself in step 2.

---

## On "best strikes to maximise profits"

`strike_selector.py` ranks contracts on liquidity, spread, delta band and
position relative to the OI walls. That is a tradeability ranking, not a profit
ranking — expected profit isn't observable from the option chain.

It reliably keeps you out of 4%-spread contracts and far-OTM strikes needing a
200-point move to break even. It cannot tell you the direction is right.

Your own postmortems put your losses down to strike-ladder chasing and sizing,
not strike quality. This engine doesn't fix either — which is why
`MAX_LOTS_PER_SIGNAL`, `MAX_CONCURRENT_POSITIONS` and
`NO_NEW_SIGNALS_AFTER_HOUR` are historical settings in `config.py`. Verify the
active gates in the executor; live safeguards must remain in force even when
the scanner score is high.
