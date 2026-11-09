#!/bin/bash
# KADIMA WINDOW EXIT v1 (branch claude/window-exit-20261109). Trader's rules of 09-Nov for trades
# ENTERED 09:45-10:10 IST:
#   Gamma Blast : engine stop; at T1 (+50%) stop -> T1; after T2 (+100%) trail 25% below peak; close 13:00.
#   Scalp v2    : engine stop; at T2 (+20%) book half, stop -> entry; rest runs, close 13:00.
#                 (1 lot cannot book half: it exits whole at T2, as today.)
# Changes kadima/scalp_v2.py and kadima/auto_executor.py (hooks only) and adds kadima/window_exit.py.
# No entry, score, Shield, CAS, order-routing, login or weight change. Trades entered outside the
# window are untouched. Switch: $KADIMA_DATA_DIR/window_exit.json {"mode": "off|shadow|live"}.
# Run on EC2 OUTSIDE market hours (before 09:00 or after 15:45 IST), with no open positions.
#   bash deploy_window_exit_20261109.sh --check              # verify the live files only; changes nothing
#   WINDOW_EXIT_MODE=shadow bash deploy_window_exit_20261109.sh   # install, log-only
#   WINDOW_EXIT_MODE=live   bash deploy_window_exit_20261109.sh   # install, rules trade for real
#   bash deploy_window_exit_20261109.sh --mode shadow|live|off    # change the switch later (no restart)
#   bash deploy_window_exit_20261109.sh --rollback           # restore the two files, remove the module, restart
set -u
ROOT=${KADIMA_ROOT:-$HOME/kadima-py}
DATA=${KADIMA_DATA_DIR:-/var/lib/kadima}
BRANCH=claude/window-exit-20261109
PY=$ROOT/venv/bin/python
SV=$ROOT/kadima/scalp_v2.py; AE=$ROOT/kadima/auto_executor.py; WE=$ROOT/kadima/window_exit.py
# Live versions this patch was written against (EC2 snapshot 10-Nov 00:03 IST, kadima-audit live-snapshot-20261110-000309).
OLD_SV=e0e81a12c6d931c31d55ef9ce9fc34a32cd6d1d55a09484e4e0e589bf984aa2a
OLD_AE=36bfd4330042de1c0c99afbeace7b37f9c1711c9ad5719277548155088b88cfc
NEW_SV=910a881176a75d69878ee871e0c4547e4960a565168a3be8b7d3d48eab0a6264
NEW_AE=192338467176e7ae38ba84af2b6279ad232577a9b0733c0a5c75d988c9c74757
NEW_WE=6ef02f16e9e5bfcbb4550beda20fdef58570d2e5d7f0c05602094c87aaf55b64
# Modules the new code calls into must be the same versions it was tested against.
LIVE_EL=de4770027c7ee1638d2714181b59e8f0d7dc33d056170431b4eee09300e6eabb   # execution_ledger.py
LIVE_ST=234da3afc344c999a45757d57e5ff56e487d46103ee21cdce016f78ef879de87   # storage.py
LIVE_FM=771b7d9fdf331a82f68799fac642764b7f01fea3909a1e36118e92f50e53e29c   # fast_momentum.py
LIVE_RH=0b45f592b25889e1caf5eb4e95e84d2342913c0704281d0747386d954186b7d7   # runner_hold.py
LIVE_CB=8fb391f3f0de9fdb8420372fbfd345ef013da9f3a5a3a54566da3832e99b8eb9   # capital_builder.py
MARK=$ROOT/.window-exit-20261109-backup
h() { sha256sum "$1" 2>/dev/null | cut -c1-64; }
st() { [ "$(h "$1")" = "$2" ] && echo "expected" || { [ "$(h "$1")" = "$3" ] && echo "already-patched" || echo "DIFFERENT ($(h "$1" | cut -c1-12))"; }; }
health() {
  sleep 25
  systemctl is-active --quiet kadima || { echo "HEALTH FAIL: kadima not active"; return 1; }
  if journalctl -u kadima --since "-1 min" --no-pager | grep -q "Traceback"; then echo "HEALTH FAIL: traceback"; return 1; fi
  echo "health ok"; return 0
}
setmode() {
  case "$1" in off|shadow|live) ;; *) echo "mode must be off|shadow|live"; return 1;; esac
  printf '{"mode": "%s"}\n' "$1" > "$DATA/window_exit.json.new" && mv "$DATA/window_exit.json.new" "$DATA/window_exit.json"
  echo "window_exit mode = $1 (picked up within 30 s, no restart)"
}
restore() {
  local BD=$1
  [ "$(h $BD/scalp_v2.py)" = "$OLD_SV" ] && [ "$(h $BD/auto_executor.py)" = "$OLD_AE" ] || { echo "ABORT: backup in $BD is not the expected predecessor"; return 1; }
  cp -p "$BD/scalp_v2.py" "$SV"; cp -p "$BD/auto_executor.py" "$AE"; rm -f "$WE"
  sudo systemctl restart kadima && health
  echo "restored scalp_v2.py $(h $SV | cut -c1-12) auto_executor.py $(h $AE | cut -c1-12); window_exit.py removed"
}
echo "live scalp_v2.py      : $(st $SV $OLD_SV $NEW_SV)"
echo "live auto_executor.py : $(st $AE $OLD_AE $NEW_AE)"
echo "live window_exit.py   : $([ -e $WE ] && st $WE none $NEW_WE || echo absent)"
echo "execution_ledger.py   : $([ "$(h $ROOT/kadima/execution_ledger.py)" = "$LIVE_EL" ] && echo expected || echo DIFFERENT)"
echo "storage.py            : $([ "$(h $ROOT/kadima/storage.py)" = "$LIVE_ST" ] && echo expected || echo DIFFERENT)"
echo "fast_momentum.py      : $([ "$(h $ROOT/kadima/fast_momentum.py)" = "$LIVE_FM" ] && echo expected || echo DIFFERENT)"
echo "runner_hold.py        : $([ "$(h $ROOT/kadima/runner_hold.py)" = "$LIVE_RH" ] && echo expected || echo DIFFERENT)"
echo "capital_builder.py    : $([ "$(h $ROOT/kadima/capital_builder.py)" = "$LIVE_CB" ] && echo expected || echo DIFFERENT)"
case "${1:-}" in
  --check) exit 0 ;;
  --mode) setmode "${2:-}"; exit ;;
  --rollback) [ -f "$MARK" ] || { echo "ABORT: no backup marker $MARK"; exit 1; }; restore "$(cat $MARK)"; exit ;;
esac
MODE=${WINDOW_EXIT_MODE:?set WINDOW_EXIT_MODE=shadow or live}
case "$MODE" in shadow|live) ;; *) echo "WINDOW_EXIT_MODE must be shadow or live"; exit 1;; esac
echo "== 1. gates"
if [ "$(h $SV)" = "$NEW_SV" ] && [ "$(h $AE)" = "$NEW_AE" ] && [ "$(h $WE)" = "$NEW_WE" ]; then echo "already installed"; setmode "$MODE"; exit; fi
[ "$(h $SV)" = "$OLD_SV" ] && [ "$(h $AE)" = "$OLD_AE" ] && [ ! -e "$WE" ] || { echo "ABORT: live scalp_v2.py/auto_executor.py are not the 10-Nov 00:03 versions this patch was written against (or window_exit.py exists); nothing changed. Push a fresh live snapshot and ask for a re-base."; exit 1; }
[ "$(h $ROOT/kadima/execution_ledger.py)" = "$LIVE_EL" ] && [ "$(h $ROOT/kadima/storage.py)" = "$LIVE_ST" ] && [ "$(h $ROOT/kadima/fast_momentum.py)" = "$LIVE_FM" ] \
  && [ "$(h $ROOT/kadima/runner_hold.py)" = "$LIVE_RH" ] && [ "$(h $ROOT/kadima/capital_builder.py)" = "$LIVE_CB" ] || { echo "ABORT: execution_ledger/storage/fast_momentum/runner_hold/capital_builder changed since 10-Nov 00:03; nothing changed"; exit 1; }
HM=$(TZ=Asia/Kolkata date +%H%M); DOW=$(TZ=Asia/Kolkata date +%u)
if [ "$DOW" -le 5 ] && [ "$HM" -ge 0900 ] && [ "$HM" -lt 1545 ]; then echo "ABORT: market hours (IST $HM); run after 15:45"; exit 1; fi
TMP=$(mktemp -d)
git -C "$ROOT" fetch -q https://github.com/manchalakusumakar-spec/kadima.git "$BRANCH" || { echo "ABORT: fetch failed"; rm -rf "$TMP"; exit 1; }
for f in scalp_v2 auto_executor window_exit; do git -C "$ROOT" show "FETCH_HEAD:kadima/$f.py" > "$TMP/$f.py" || { echo "ABORT: $f.py missing in $BRANCH"; rm -rf "$TMP"; exit 1; }; done
git -C "$ROOT" show "FETCH_HEAD:tools/test_window_exit.py" > "$TMP/test_window_exit.py"
[ "$(h $TMP/scalp_v2.py)" = "$NEW_SV" ] && [ "$(h $TMP/auto_executor.py)" = "$NEW_AE" ] && [ "$(h $TMP/window_exit.py)" = "$NEW_WE" ] || { echo "ABORT: payload hash mismatch; nothing changed"; rm -rf "$TMP"; exit 1; }
$PY -m py_compile "$TMP/scalp_v2.py" "$TMP/auto_executor.py" "$TMP/window_exit.py" || { echo "ABORT: payload does not compile; nothing changed"; rm -rf "$TMP"; exit 1; }
# offline tests against a copy of the live package with the payload dropped in (temp data dir)
mkdir -p "$TMP/pkg/tools" && cp -r "$ROOT/kadima" "$TMP/pkg/" && cp "$TMP/"{scalp_v2,auto_executor,window_exit}.py "$TMP/pkg/kadima/" && cp "$TMP/test_window_exit.py" "$TMP/pkg/tools/"
(cd "$TMP/pkg" && KADIMA_DATA_DIR=$(mktemp -d) $PY tools/test_window_exit.py | tail -1) | grep -q "ALL WINDOW EXIT TESTS PASSED" || { echo "ABORT: offline tests failed; nothing changed"; rm -rf "$TMP"; exit 1; }
echo "offline tests passed"
if [ "${WINDOW_EXIT_CONFIRMED:-}" != "YES" ]; then
  read -r -p "No open orders/positions, mode=$MODE, OK to restart kadima now? type YES: " OK
  [ "$OK" = "YES" ] || { rm -rf "$TMP"; echo "stopped, nothing changed"; exit 1; }
fi
echo "== 2. backup"
TS=$(date +%Y%m%d_%H%M%S); mkdir -p $HOME/backups; BD=$(mktemp -d $HOME/backups/window-exit-20261109-$TS-XXXX)
cp -p "$SV" "$AE" "$BD/" || { echo "ABORT: backup failed"; rm -rf "$TMP"; exit 1; }
echo "$BD" > "$MARK"; sha256sum "$BD"/*.py
echo "== 3. install"
setmode "$MODE"
cp "$TMP/window_exit.py" "$WE.new" && mv "$WE.new" "$WE"
cp "$TMP/scalp_v2.py" "$SV.new" && mv "$SV.new" "$SV"
cp "$TMP/auto_executor.py" "$AE.new" && mv "$AE.new" "$AE"; rm -rf "$TMP"
[ "$(h $SV)" = "$NEW_SV" ] && [ "$(h $AE)" = "$NEW_AE" ] && [ "$(h $WE)" = "$NEW_WE" ] || { echo "== AUTO ROLLBACK (files not as expected)"; restore "$BD"; exit 1; }
echo "== 4. restart + health"
sudo systemctl restart kadima
if ! health; then echo "== AUTO ROLLBACK"; restore "$BD"; exit 1; fi
echo "DONE: WINDOW EXIT v1 installed, mode=$MODE, from the next session. Backup in $BD."
echo "Watch: grep window_exit $DATA/audit/*\$(TZ=Asia/Kolkata date +%F)* | tail   Rollback: bash $0 --rollback"
