# Shared Shield confirmation and broker trace

This change is based on GitHub commit `615660d8572469999d65193da253436f2f329925`
(the head of draft PR #2 when inspected). It is not evidence of what AWS currently runs.

## Findings

The inspected source already used a shared 65 minimum. No separate 90-point
Shield execution gate was present. The 90-point Gamma/OTM classifications are a
separate strategy and remain unchanged.

The following source defects were reproduced:

- `_seed_last_known` restored a CONFIRMED label but neither its episode timestamp
  nor confirmation evidence. The subsequent `None == None` episode comparison
  suppressed signal emission.
- Display confirmation used score alone; holding/technical checks happened later.
- Holding was elapsed wall time over 3-minute scoring callbacks, not an additional
  completed 5-minute bar.
- Choosing only the highest-scoring index could obscure another eligible index.
- Marking a signal as emitted before a pre-submission rejection prevented retry
  for the remainder of the episode.

These explain possible failures, not the confirmed cause of the September 16
missed order. Deployment identity and that day's dispatch logs are unavailable here.

## Rule

`shield_confirmation.is_shield_confirmed()` is used for `/state`, event emission,
trade eligibility, and the final check before submitting an entry intent.

Both CE and PE require score >= 65, directional VWAP/EMA/RSI alignment, fresh spot
and technicals, fresh directional OI velocity confirmation, no invalidation, and
two consecutive completed 5-minute bars in the same direction. Duplicate, missing,
future, previous-session and stale bars cannot satisfy the hold. A VWAP/EMA/RSI
flip cancels the hold; restored display state cannot provide it.

The existing 3-minute indicator lookbacks remain unchanged. Five-minute closes
supply holding evidence. BANKNIFTY additionally needs at least two of HDFCBANK,
ICICIBANK, KOTAKBANK and SBIN with fresh quotes and aligned VWAP, EMA20/50 and RSI
from closed 5-minute bars. Tokens are resolved from the existing scrip master.
The existing validated history loader warms up bank indicators; absent/stale
history or missing token resolution blocks the bank vote and is logged.

65 is a score floor, not a success probability. With the current weights,
VWAP + EMA + RSI total 65 and directional OI adds 15, so an all-gates-pass engine
score will ordinarily be at least 80. The weights have not been changed to force
more trades.

Existing time, killswitch, reconciliation, position-count, liquidity, structure,
capital and risk gates remain in force. Celestial mode also requires a matching
eligible Top 20 contract and enabled controls; its configurable minimum may be
higher than 65. Shield score and the selected contract's ranking score are not
interchanged. Gamma's strategy and manual dashboard flow remain unchanged.

Pre-submission rejections may retry on a subsequent evaluation using the same
signal ID. Any durable order intent, including an uncertain request, blocks
resubmission. Broker acceptance remains distinct from a fill.

## Trace and state

- `SHIELD_SCORE` and `CONFIRMATION_GATE`: index, direction, bar close and checks.
- `CONFIRMED_EVENT`: same index/direction/bar, non-null since, components, signal ID.
- `SHIELD_DISPATCH_BLOCKED` / `SHIELD_DISPATCH_RESULT`: remaining blocker or result.
- `ANGELONE_REQUEST` / `ANGELONE_RESPONSE`: signal ID, intent, order ID and outcome.
- `ANGELONE_BLOCKED`: dry run, rate limit or session failure; no request is claimed.

`/state.indices.<index>.shield` includes confirmation checks, components and
signal ID. A stale confirmation is presented as ACTIVE. API responses do not
manufacture confirmed evidence from saved score/labels.

## Validation and deployment

Run `python -m unittest discover -s tests -v` with the project's dependencies plus
httpx, then `python -m compileall -q kadima tests`. Tests replace broker boundaries;
none place real orders. Coverage includes threshold boundaries, both directions,
missing gates, BANKNIFTY freshness, restart recovery, hold gaps, intra-bar flips,
retry identity, final revalidation, and correlated request/response logs.

Use the consolidated `executor_fix/install.py` deployment from this release. Its
manifest includes the new confirmation module and API changes and allows the
verified predecessor hashes. It permits updates 24/7; its source-identity,
flat-account, dependency and service-health checks remain intact. Do not bypass
an unknown deployed source hash.

Before calling this live: verify the deployed commit/settings, installed service
health, authenticated state checks and broker/local reconciliation. Observe the
trace through a legitimate signal or an explicitly configured dry run. This PR
neither deploys AWS nor places a test order.
