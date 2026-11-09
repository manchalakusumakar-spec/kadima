#!/bin/bash
# KADIMA forecast autofix 09-Nov (branch claude/forecast-autofix-20261109), part 1: nightly bhavcopy retry.
# NSE publishes the F&O bhavcopy in the evening, after the 16:05 nightly run, so every session
# 29 Oct-09 Nov was 'both_archives_404' once and never asked again (bhav_options empty).
# Only kadima/forecast_bhavcopy.py changes. It is imported only by tools/forecast_nightly.py
# (kadima-forecast-nightly timer), so the kadima engine is NOT restarted.
# No trading, Gamma Blast, CAS, Shield, stop-loss, order, login or weight change; no live forecast input changes.
#   bash deploy_forecast_autofix_20261109.sh             # gates, backup, install, check
#   bash deploy_forecast_autofix_20261109.sh --rollback  # restore the file
set -u
ROOT=${KADIMA_ROOT:-$HOME/kadima-py}
BRANCH=claude/forecast-autofix-20261109
COMMIT=${AUTOFIX_COMMIT:?set AUTOFIX_COMMIT to the reviewed commit of $BRANCH}
PY=$ROOT/venv/bin/python
BH=$ROOT/kadima/forecast_bhavcopy.py
OLD_BH=2d5843a0e65cf2045fc153956d9a9b811140c65c2cd95826b13b94bda6501d75
NEW_BH=7f2f1398d20a8c94fc753edc7194d469701839281b53ab4a21316d9b94e61449
# The nightly runner this sits under must be the live one.
LIVE_NIGHTLY=f0f3de9b44dd4382ea975dd4f89bfaeb4321b9b52c2680e8f9622b4131639999
MARK=$ROOT/.forecast-autofix-20261109-backup
h() { sha256sum "$1" 2>/dev/null | cut -c1-64; }
check() {
  (cd "$ROOT" && $PY -I -c "import sys; sys.path.insert(0,'.'); import kadima.forecast_bhavcopy as B; assert B.RETRY_SESSIONS==3" 2>&1) || { echo "CHECK FAIL: import"; return 1; }
  systemctl is-active --quiet kadima || { echo "CHECK FAIL: kadima not active"; return 1; }
  echo "check ok"; return 0
}
restore() {
  local BD=$1
  [ "$(h $BD/forecast_bhavcopy.py)" = "$OLD_BH" ] || { echo "ABORT: backup in $BD is not the expected predecessor"; return 1; }
  cp -p "$BD/forecast_bhavcopy.py" "$BH"
  echo "restored forecast_bhavcopy.py $(h $BH | cut -c1-12)"
}
if [ "${1:-}" = "--rollback" ]; then
  [ -f "$MARK" ] || { echo "ABORT: no backup marker $MARK"; exit 1; }
  restore "$(cat $MARK)"; exit
fi
echo "== 1. gates"
echo "live forecast_bhavcopy.py $(h $BH | cut -c1-12)"
[ "$(h $BH)" = "$NEW_BH" ] && { echo "already installed"; exit 0; }
[ "$(h $BH)" = "$OLD_BH" ] || { echo "ABORT: forecast_bhavcopy.py is not the expected live version; nothing changed"; exit 1; }
[ "$(h $ROOT/tools/forecast_nightly.py)" = "$LIVE_NIGHTLY" ] || { echo "ABORT: tools/forecast_nightly.py is not the expected version; nothing changed"; exit 1; }
HM=$(TZ=Asia/Kolkata date +%H%M); DOW=$(TZ=Asia/Kolkata date +%u)
if [ "$DOW" -le 5 ] && [ "$HM" -ge 0900 ] && [ "$HM" -lt 1545 ]; then echo "ABORT: market hours (IST $HM); run after 15:45"; exit 1; fi
if systemctl is-active --quiet kadima-forecast-nightly.service; then echo "ABORT: nightly run in progress; try again later"; exit 1; fi
git -C "$ROOT" cat-file -e "$COMMIT" 2>/dev/null || { echo "ABORT: commit ${COMMIT:0:12} not fetched; run: git -C $ROOT fetch origin $BRANCH"; exit 1; }
TMP=$(mktemp -d)
git -C "$ROOT" show "$COMMIT:kadima/forecast_bhavcopy.py" > "$TMP/bh.py"
[ "$(h $TMP/bh.py)" = "$NEW_BH" ] || { echo "ABORT: payload hash mismatch; nothing changed"; rm -rf "$TMP"; exit 1; }
$PY -m py_compile "$TMP/bh.py" || { echo "ABORT: payload does not compile; nothing changed"; rm -rf "$TMP"; exit 1; }
if [ "${AUTOFIX_CONFIRMED:-}" != "YES" ]; then
  read -r -p "No open orders and OK to install now? type YES: " OK
  [ "$OK" = "YES" ] || { rm -rf "$TMP"; echo "stopped, nothing changed"; exit 1; }
fi
echo "== 2. backup"
TS=$(date +%Y%m%d_%H%M%S); mkdir -p $HOME/backups; BD=$(mktemp -d $HOME/backups/forecast-autofix-20261109-$TS-XXXX)
cp -p "$BH" "$BD/" || { echo "ABORT: backup failed"; rm -rf "$TMP"; exit 1; }
echo "$BD" > "$MARK"; sha256sum "$BD"/*.py
echo "== 3. install"
cp "$TMP/bh.py" "$BH.new-fix" && mv "$BH.new-fix" "$BH"; rm -rf "$TMP"
[ "$(h $BH)" = "$NEW_BH" ] || { echo "== AUTO ROLLBACK (file not as expected after install)"; restore "$BD"; exit 1; }
echo "== 4. check"
if ! check; then echo "== AUTO ROLLBACK"; restore "$BD"; exit 1; fi
echo "DONE: forecast_bhavcopy.py ${NEW_BH:0:12} live from the next 16:05 nightly run. Backup in $BD. Rollback: bash $0 --rollback"
