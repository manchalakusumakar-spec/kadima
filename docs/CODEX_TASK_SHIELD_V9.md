# Codex task — install SHIELD v9 phase 1 (shadow) on KADIMA-BRIDGE

Owner: Kusumakar Manchala (trader). Approved 12 Oct 2026: phase 1 = index-futures VWAP and
volume, the Master Checklist v9.0 Bull/Bear Shield score in SHADOW, a live switch left OFF,
and five candidate filters logged only. Trading behaviour must not change.

## Source
- Repo `manchalakusumakar-spec/kadima`, branch `claude/shield-v9-futures`, based on
  `live/2026-10-11` (the captured server code). Read the branch's commit messages.
- Files (SHA-256):

| File | Live (before) | New (after) |
|---|---|---|
| `kadima/engine.py` | `e04de181ed97bc274f217a526e0a22e576dda76dc38125854dd59c32c0b026bc` | `d4ed0266dd08514e7c2d3c3d4e3f3df1cd34f5cf2b0b6b0ae1d3cd29a616895d` |
| `kadima/api.py` | `e38de8a505253abff0b074ee8da0e3223fce38c38aef932ddd9faad65988aa00` | `9cfcf3d6ee4582d11a775a552410ec24d868dd478defbe0a80551f07ce7a0ce4` |
| `kadima/shield_v9.py` | (new) | `5e0ac730ca75cf99b2aaa02d16e4cdb8eecb73d85796db5c093c1b650354595e` |
| `tools/test_shield_v9.py` | (new) | `40915f77eb1a8b128a3c8ac56f2cef4979c6ea98576d44890cd7aed40c72b8a1` |
| `tools/v9_engine_diff.py` | (new) | `5a515b42d89453f181efc1d3a3b1d0c4af338f99747c8540e0c1e48fb0066eb6` |

## Hard rules (stop and report if any would be broken)
1. Never place, modify or cancel a broker order. No Angel One login from your scripts.
2. **Never set `KADIMA_SHIELD_V9_LIVE`** anywhere. Phase 1 is shadow only.
3. Never `git checkout`, `reset`, `pull` or `stash` in `~/kadima-py`.
4. Change only the files in the table. No config, `.env`, systemd or other module edits.
5. Steps B–D only after 15:45 IST on a trading day, or on a non-trading day.
6. If a live "before" SHA-256 differs from the table, stop and report. Do not merge by hand.
7. Your sandbox cannot write into `~/kadima-py` or restart systemd: put verified files in
   `~/codex_handoff/shield_v9/` and let the trader run the copy and restart commands.

## Step A — read-only checks (any time)
- SHA-256 of the live `kadima/engine.py` and `kadima/api.py` vs the "before" column.
- From the live scrip-master cache (read-only): the near-month `FUTIDX` rows for NIFTY,
  BANKNIFTY, MIDCPNIFTY (NFO) and SENSEX (BFO): token, symbol, expiry.
- Whether `~/kadima-py/state/macro_snapshot.json` exists and its `ts`. The v9 tiers cap at
  WATCH when it is missing or older than 15 minutes (checklist: macro status unknown).
Output: `codex_reports/20_shield_v9_preflight.md`.

## Step B — offline verification (after 15:45)
1. Fetch the five files from the branch into `~/codex_handoff/shield_v9/` and verify every hash.
2. On a scratch copy of `~/kadima-py` with the new files in place, with a temporary
   `KADIMA_DATA_DIR` and `HOME`, run: `tools/test_shield_v9.py`, `tools/test_audit_fixes.py`,
   `tools/test_cas_executor.py`, `tools/test_manual_protection.py`, `tools/test_offline.py`.
3. Run `tools/v9_engine_diff.py --root <scratch copy of the CURRENT live tree>` and
   `--root <scratch copy with new files>`; drop the `SHIELD_V9` events and confirm the two
   outputs are identical (switch off = unchanged scoring and tiers).
4. Delete the scratch copies. Show the trader B1–B3 and wait for **go**.

## Step C — install (trader runs it after "go")
Give the trader this block to run as one command; it refuses to change anything if a hash differs:
```bash
bash -euo pipefail <<'EOF'
SRC=~/codex_handoff/shield_v9
cd ~/kadima-py
echo "e04de181ed97bc274f217a526e0a22e576dda76dc38125854dd59c32c0b026bc  kadima/engine.py" | sha256sum -c
echo "e38de8a505253abff0b074ee8da0e3223fce38c38aef932ddd9faad65988aa00  kadima/api.py" | sha256sum -c
( cd "$SRC" && sha256sum -c <<'SUMS'
d4ed0266dd08514e7c2d3c3d4e3f3df1cd34f5cf2b0b6b0ae1d3cd29a616895d  engine.py
9cfcf3d6ee4582d11a775a552410ec24d868dd478defbe0a80551f07ce7a0ce4  api.py
5e0ac730ca75cf99b2aaa02d16e4cdb8eecb73d85796db5c093c1b650354595e  shield_v9.py
40915f77eb1a8b128a3c8ac56f2cef4979c6ea98576d44890cd7aed40c72b8a1  test_shield_v9.py
5a515b42d89453f181efc1d3a3b1d0c4af338f99747c8540e0c1e48fb0066eb6  v9_engine_diff.py
SUMS
)
B=~/kadima-py/backups/shield_v9_$(date +%Y%m%d_%H%M%S); mkdir -p "$B"
cp -p kadima/engine.py kadima/api.py "$B"/
cp "$SRC"/engine.py "$SRC"/api.py "$SRC"/shield_v9.py kadima/
cp "$SRC"/test_shield_v9.py "$SRC"/v9_engine_diff.py tools/
venv/bin/python -m py_compile kadima/engine.py kadima/api.py kadima/shield_v9.py
sha256sum kadima/engine.py kadima/api.py kadima/shield_v9.py
echo "BACKUP: $B"
EOF
```
Then: `sudo systemctl restart kadima`.

## Step D — after restart
1. `systemctl is-active kadima`; `systemctl show kadima -p NRestarts` unchanged after 5 minutes.
2. `curl -s localhost:8080/health` and `curl -s localhost:8081/health` ok.
3. Today's audit log has `shield_v9_setup` with `live: false` and a futures contract for each
   index (or `shield_v9_future_unavailable` naming the one missing), and no `Traceback`,
   `position_monitor_loop_error` or `shield_v9_setup_failed`.
4. Next trading session, after 09:30: `SHIELD_V9` events every 5 minutes per index with a
   `futures` block; `shield_v9_error` count; `/state` shows `shield_v9` per index.
5. Rollback: copy `engine.py` and `api.py` back from the BACKUP folder, delete
   `kadima/shield_v9.py`, `py_compile`, restart, confirm the "before" hashes and health.

## Report back
`codex_reports/21_shield_v9_install.md`: preflight, verification, installed hashes, health,
the first `SHIELD_V9` lines, and **Broker orders placed: NO**, **KADIMA_SHIELD_V9_LIVE set: NO**.
Commit the reports only to `claude/shield-v9-futures` and push.
