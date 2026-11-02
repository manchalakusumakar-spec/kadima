#!/bin/bash
# KADIMA forecast autofix 02-Nov (branch claude/forecast-autofix-20261102). NEEDS KUSUMAKAR'S YES:
# it lets the forecast option-chain collector take one fair FIFO turn of the SHARED quote budget
# (2 calls per 5 min, 3 at EOD) when idle capacity never appears, so trading quotes can wait one
# extra ~1.1 s slot at those moments. chain_wide/chain_eod have had 0 rows 27 Oct-02 Nov.
# Only kadima/forecast_chain_data.py changes; rate_limiter.py is gated, not modified.
# No Gamma Blast, CAS, Shield, stop-loss, order, Angel One login or weight change.
# Run on EC2 after 15:45 IST.
#   bash deploy_forecast_autofix_20261027.sh             # gates, backup, install, restart, health
#   bash deploy_forecast_autofix_20261027.sh --rollback  # restore the file, restart
set -u
ROOT=${KADIMA_ROOT:-$HOME/kadima-py}
BRANCH=claude/forecast-autofix-20261102
COMMIT=${AUTOFIX_COMMIT:?set AUTOFIX_COMMIT to the reviewed commit of $BRANCH}
PY=$ROOT/venv/bin/python
CD=$ROOT/kadima/forecast_chain_data.py
OLD_CD=5a8cf4d6f42bb79212f4a6cde7a62e745cf33e96fa08e011bacecf2418c93626
NEW_CD=4dbc4849470c4e3239a53763dd4055a1c6158774539d634929978cf04606c7ca
# The shared quote wrapper this fix relies on (_kadima_budgeted FIFO quote) must be the live one.
LIVE_RL=bacccec8287f985115ac5a359f6db388c32989868dd8503ad580f4ab0b70a6a0
# The live forecast release (27-Oct + 28-Oct live line) this fix sits on.
LIVE_F=abb20fc43fb2dd9c571bc256f036614f5aabd52446a3a101dc3857fe9f1d1122
LIVE_MD=0a1f4a3999ae081163192b64ccbb1989dcfa21252039eb88865bb21cd4f2e332
LIVE_DF=5402345148fdbe755c2bad51ea28eb4cd3db9427afca5d2a77eb797b34590358
MARK=$ROOT/.forecast-autofix-20261102-backup
h() { sha256sum "$1" 2>/dev/null | cut -c1-64; }
health() {
  sleep 25
  systemctl is-active --quiet kadima || { echo "HEALTH FAIL: kadima not active"; return 1; }
  if journalctl -u kadima --since "-1 min" --no-pager | grep -q "Traceback"; then echo "HEALTH FAIL: traceback"; return 1; fi
  echo "health ok"; return 0
}
restore() {
  local BD=$1
  [ "$(h $BD/forecast_chain_data.py)" = "$OLD_CD" ] || { echo "ABORT: backup in $BD is not the expected predecessor"; return 1; }
  cp -p "$BD/forecast_chain_data.py" "$CD"
  sudo systemctl restart kadima && health
  echo "restored forecast_chain_data.py $(h $CD | cut -c1-12)"
}
if [ "${1:-}" = "--rollback" ]; then
  [ -f "$MARK" ] || { echo "ABORT: no backup marker $MARK"; exit 1; }
  restore "$(cat $MARK)"; exit
fi
echo "== 1. gates"
echo "live forecast_chain_data.py $(h $CD | cut -c1-12)"
[ "$(h $CD)" = "$NEW_CD" ] && { echo "already installed"; exit 0; }
[ "$(h $CD)" = "$OLD_CD" ] || { echo "ABORT: forecast_chain_data.py is not the expected live version; nothing changed"; exit 1; }
[ "$(h $ROOT/kadima/hourly_forecast.py)" = "$LIVE_F" ] && [ "$(h $ROOT/kadima/hourly_forecast_model.py)" = "$LIVE_MD" ] \
  && [ "$(h $ROOT/kadima/day_forecast.py)" = "$LIVE_DF" ] && [ "$(h $ROOT/kadima/rate_limiter.py)" = "$LIVE_RL" ] || { echo "ABORT: live forecast/rate_limiter files are not the expected versions; nothing changed"; exit 1; }
HM=$(TZ=Asia/Kolkata date +%H%M); DOW=$(TZ=Asia/Kolkata date +%u)
if [ "$DOW" -le 5 ] && [ "$HM" -ge 0900 ] && [ "$HM" -lt 1545 ]; then echo "ABORT: market hours (IST $HM); run after 15:45"; exit 1; fi
git -C "$ROOT" cat-file -e "$COMMIT" 2>/dev/null || { echo "ABORT: commit ${COMMIT:0:12} not fetched; run: git -C $ROOT fetch origin $BRANCH"; exit 1; }
TMP=$(mktemp -d)
git -C "$ROOT" show "$COMMIT:kadima/forecast_chain_data.py" > "$TMP/cd.py"
[ "$(h $TMP/cd.py)" = "$NEW_CD" ] || { echo "ABORT: payload hash mismatch; nothing changed"; rm -rf "$TMP"; exit 1; }
$PY -m py_compile "$TMP/cd.py" || { echo "ABORT: payload does not compile; nothing changed"; rm -rf "$TMP"; exit 1; }
if [ "${AUTOFIX_CONFIRMED:-}" != "YES" ]; then
  read -r -p "No open orders/positions and OK to restart kadima now? type YES: " OK
  [ "$OK" = "YES" ] || { rm -rf "$TMP"; echo "stopped, nothing changed"; exit 1; }
fi
echo "== 2. backup"
TS=$(date +%Y%m%d_%H%M%S); mkdir -p $HOME/backups; BD=$(mktemp -d $HOME/backups/forecast-autofix-20261102-$TS-XXXX)
cp -p "$CD" "$BD/" || { echo "ABORT: backup failed"; rm -rf "$TMP"; exit 1; }
echo "$BD" > "$MARK"; sha256sum "$BD"/*.py
echo "== 3. install"
cp "$TMP/cd.py" "$CD.new-fix" && mv "$CD.new-fix" "$CD"; rm -rf "$TMP"
[ "$(h $CD)" = "$NEW_CD" ] || { echo "== AUTO ROLLBACK (file not as expected after install)"; restore "$BD"; exit 1; }
echo "== 4. restart + health"
sudo systemctl restart kadima
if ! health; then echo "== AUTO ROLLBACK"; restore "$BD"; exit 1; fi
echo "DONE: forecast_chain_data.py ${NEW_CD:0:12} live from the next session. Backup in $BD. Rollback: bash $0 --rollback"
