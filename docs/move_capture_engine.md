# Move Capture Engine — shadow release

## Purpose

Move Capture is a spot-first discovery lane for explosive single-stock moves.
It exists because option OI/Gamma structure can react after the underlying has
already accelerated. This service watches F&O underlyings continuously over the
spot WebSocket and ranks abnormal price/volume impulses before option-side
confirmation is requested.

This first release is **observe/shadow only**. It has no broker-order imports,
no shared-executor call, no control that can arm trading, and no ability to
modify Gamma Blast or CAS state.

## Isolation

The intended deployment is a separate `kadima-move-capture.service`.

It does not run inside `kadima.service`, the F&O universe scanner, Gamma scorer
or CAS scanner. A Move Capture crash/reconnect therefore must not restart those
services. The process uses the existing authenticated SmartAPI session logic but
opens its own spot-only SmartWebSocket connection.

The service deliberately does **not** reuse `TickFeed`: `TickFeed` persists
every received tick to the production tick table. Doing that for the whole F&O
universe would create unnecessary database/IO pressure. Move Capture keeps only
bounded in-memory one-second samples and writes a small Top-N snapshot plus
promotion events.

Suggested systemd resource envelope for the first live-shadow deployment:

- `Restart=on-failure`
- `RestartSec=5`
- `MemoryMax=160M`
- `CPUQuota=75%`
- same service account/environment/credentials as the existing scanner
- no dependency relationship that restarts `kadima.service`, Gamma or CAS

These values are deployment starting points, not benchmarked guarantees.

## Coverage

`move_capture_service.universe()` reads every resolved F&O underlying spot token
from the compact instrument cache and adds five index spot references when
available: NIFTY, BANKNIFTY, SENSEX, MIDCPNIFTY and BANKEX.

Only spot tokens are subscribed at this stage. No option token is consumed by
Move Capture v1.

The service hard-fails if its resolved spot universe exceeds 512 names rather
than silently truncating it.

## Layer 0 — bounded spot tape

For each symbol the detector retains at most 1000 one-second samples, enough for
roughly 16 minutes. Multiple ticks inside one second collapse to the latest
price/cumulative volume for that second.

Per-symbol state includes:

- 1/3/5/15-minute returns
- one-minute rolling volatility
- short-horizon price acceleration
- cumulative-volume acceleration against a recent intraday baseline
- session VWAP estimated from cumulative-volume increments
- gap versus broker-supplied previous close when available
- day high/low and range location

Invalid/naive timestamps, non-finite prices and negative volume never refresh
detector state.

## Layer 1 — normalized impulse score

The shadow score is 0–100 and combines:

- 40 points: strongest volatility-normalized 1/3/5/15-minute move
- 25 points: normalized acceleration
- 20 points: recent-volume-rate expansion
- 10 points: VWAP displacement
- 5 points: location near the directional day extreme

The default promotion threshold is 65 and release threshold is 45, with a Top-10
hot-list cap and 120-second promotion cooldown.

These are **heuristic starting values, not probabilities or calibrated trading
thresholds**. They exist to collect evidence. A 65 score does not mean a 65%
chance of profit.

### Important v1 limitation

The requested long-run design calls for volume relative to each stock's
**historical time-of-day baseline**. Shadow v1 intentionally starts with a
recent same-session volume-rate baseline because no clean historical baseline
dataset exists yet. Trigger events are recorded so that time-of-day baselines
and thresholds can be calibrated from real observations rather than invented.

## Output

Default snapshot:

`/var/lib/kadima/move_capture.json`

Default promotion event log:

`/var/lib/kadima/move_capture_events.jsonl`

Snapshot output contains service health, subscribed symbol count, tick count,
Top-10 hot rows, feature values and an explicit `SHADOW_OBSERVE_ONLY` mode.

Only promotion events are appended to the event log; the service does not write
the whole universe every second to disk.

## Gamma Blast and CAS boundaries

Move Capture does not import or mutate Gamma Blast or CAS.

Future integration should use an evidence/candidate bus:

```
spot impulse -> hot list -> option confirmation -> candidate arbitration
                                         gamma ->/
                                           CAS ->/
                                               shared executor
```

Gamma remains an independent OI/IV/structure scorer. CAS remains a separate
closing-session far-OTM strategy. Neither should call Move Capture directly.

## Explicitly not implemented in this shadow release

- no option-token promotion/subscription
- no option confirmation
- no marketable-limit/chase logic
- no shared-executor dispatch
- no real or paper broker order
- no position pre-emption
- no partial-profit/trailing exit changes
- no historical time-of-day volume baseline
- no dashboard mutation/arming control
- no changes to Gamma Blast scoring
- no changes to CAS triggers/execution

Those are follow-up layers after this detector produces real-session evidence.

## Promotion criteria for Layer 2

Before adding option-side live execution, collect at minimum:

- trigger count per day
- unique symbols triggered
- latency from first abnormal spot move to trigger
- maximum favorable/adverse excursion after trigger
- false-positive/fade rate
- score distribution
- time-of-day distribution
- gap-day versus non-gap-day behavior

The purpose of shadow mode is not to delay development. It lets the spot engine
start collecting calibration data immediately while the option-confirmation and
execution layers are developed independently.
