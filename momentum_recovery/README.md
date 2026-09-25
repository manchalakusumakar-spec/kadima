# Momentum recovery

This release adds a spot-led path independent of Gamma direction and fixes
Stock Fast recovery/diagnostics. It does not guarantee catching a move or a fill.

## Evidence and scope

The 24 September POLICYBZR audit shows preparation errors at 09:37:09,
10:01:59 and 10:06:25 IST. It does not retain their exceptions. The precise
historical cause remains unproven; these changes address reproducible failure
modes, not a reconstructed exception. The supplied 26 option snapshots cannot
establish the 09:37 LTP, true day high, executable entry or missed profit.

## Behavior

- Resolve F&O stock spot tokens plus configured indices; validate exchange
  timestamps; maintain bounded 1/3/5/15-minute price and volume windows.
- Detect normalized impulses and volume-confirmed continuation in either
  direction. Gamma/OI direction is not an entry prerequisite for this path.
- Continuation starting thresholds: aligned 1-minute return >=0.25%, aligned
  3-minute return >=1%, local relative volume >=1.2, aligned observed VWAP,
  and location within the directional 20% of the observed range. These are
  uncalibrated hypotheses, not demonstrated optimal thresholds.
- Promote at most ten names. Fairly rotate preparation slots. Select liquid
  nearest-expiry options using the existing ranker, preserving a valid selected
  contract through two fresh samples separated by 2.5–12 seconds. Confirm
  premium growth >=0.25%, increasing volume and a non-falling book.
- Dispatch through the installed serialized executor and stable episode ID.
  Capacity rejection can retry while evidence remains current; submission
  deduplication remains authoritative. No extra broker order writer, forced
  liquidation, market-order fallback or unbounded price chase.
- Preserve global/stock intent, news coverage, reconciliation, daily loss,
  session, liquidity, sizing and broker permissions. News availability remains
  a possible blocker, reported explicitly. There is no new predictive news model.
- Preserve the selected holding deadline and existing position protection.
  A spot-derived initial stop is converted by existing trade policy; this release
  does not introduce tick-driven exits, broker-hosted stops or partial booking.
- Recover Stock Fast history from the latest real contiguous suffix after a
  gap. Minimum candle warmup remains required. Classify preparation exceptions
  by stage and safe reason; never log arbitrary exception bodies.
- Retain up to 32 selected option contracts for ten-second sampled observation
  through the session, even after they leave the ATM range. This does not
  measure every exchange tick or certify the exchange day high.

The feed is a supervised thread inside the engine and shares its broker session;
startup failure is isolated from existing position protection. It is not a
separate systemd process, so it does not provide process/memory crash isolation.
Do not simultaneously run the standalone feed entry point. Memory and broker
throughput on the 908MB host still require measurement during rollout.

## Source-matched AWS rollout

Use a separate checkout of this release. Do not copy the repository's raw
auto_executor.py over the installed MCX/session overlay. The focused installer
checks that overlay and its dependencies, but only changes eleven listed files.
Unknown source, exposure, controls or process identity fail closed.

The AWS news collector blob `98c0e36ef614da41096df686fa49d6333d62114b`
is a reviewed predecessor of release blob
`0cfb897286adb3c64f56f2b2f7d7e626cb46c3d4`. Its only functional upgrade is
canonicalising trusted NSE relative/protocol-relative RSS document links to
HTTPS. Existing mapping, event expiry, scoring and source validation survive.
The installer upgrades this exact predecessor; unknown news source still blocks.

Plan only (no broker call or service restart):

```sh
sudo -n python3 momentum_recovery/install.py
```

Install and request live activation, from this release directory on AWS:

```sh
sudo -n python3 momentum_recovery/go_live.py
```

The existing deployment contract requires global auto paused, stock live intent
off and no unresolved intraday exposure before replacement. Resolve exposure
through normal controls; do not clear risk gates to make installation pass.
The command performs read-only broker checks, exact source checks, backup and
guarded restart, then adds KADIMA_MOVE_CAPTURE_LIVE=1 and fast permission.
Existing Gap/MCX flags and capital limits are preserved. It sets stock intent
through its revision-checked authenticated endpoint after fresh feed and worker
health, then requests guarded global resume. Outside market hours or before feed
warmup, activation remains blocked with auto paused; retry during the session.
No test order is submitted. A failure after resume requires reading live
state; do not assume a failed command means trading is paused.

## Verification and known limits

Inspect authenticated `/api/kadima/stock-fast`: `move_capture_feed`,
`move_capture_worker`, candidate rows, blockers and positions. Fresh feed health
must show a connected socket and recent ticks. Candidate state and dispatch
results are audited separately from broker fills. `LIVE_READY` means requested
mode, not a guarantee of readiness. A service being active is insufficient.

No guaranteed detection during warmup, gaps, hot-list overflow or absent data.
Price gaps can happen before an executable option quote exists. Thresholds need
session replay with real bid/ask and latency, then controlled live observation.
Do not infer profitability from a previous-close percentage or sampled maximum.

Run `python3 -m unittest discover -s tests` offline. Tests use synthetic tapes and
mock broker boundaries; they do not establish live fill quality or expectancy.
