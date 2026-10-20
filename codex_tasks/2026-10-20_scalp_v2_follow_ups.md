# Codex task — scalp v2 follow-ups (2026-10-20)

Context: branch `claude/scalp-v2-early-entry-20261020`, decision log `docs/DECISIONS.md` (20-Oct entry).
Do not change the established architecture; every change behind a switch, shadow first.

## 1. Fast entry order: cancel and retry once (plan step 6)
- For kind `fast_momentum` entries only: after placing the entry LIMIT, poll the order book; if not
  filled within 3 s, cancel; if the cancel is confirmed AND no fill exists, re-price once from a fresh
  quote (still capped at the trigger entry x 1.005) and resend. Never resend if cancel status is unknown.
- Reuse the reconciliation in `fix/parallel-orders-retry`; one test with a 1-lot order in market hours.

## 2. Session issuer deployment
- `install_scalp_v2_20261020.py --session-runner /opt/kadima-session-auto/runner.py` patches the
  root-owned runner (adds the `index` lane). Confirm `systemctl status kadima-session-auto.timer`
  and that `/run/kadima-session-auto/lease.json` shows `lanes.index` on the next trading morning.

## 3. Shadow review after 3 sessions
- Export the Trade Audit CSV for each day; compare "Scalping v2" vs "Scalping" rows: observed outcome,
  time to T1/T2/SL, and `signal_delay_sec` / `extension_w_at_entry` from SCALP_V2_CANDIDATE audit lines.
- Switch live with `install_scalp_v2_20261020.py ~/kadima-py --live` only after the trader approves.

## 4. Option tick feed (later)
- v2 measures premium response from chain polls. Subscribing the ATM +/- 2 option tokens of an armed
  index to the WebSocket would give tick-level premium confirmation; the feed module is reviewed code,
  so this needs its own review.
