#!/bin/bash
# KADIMA live forecast line (1 s) + sharp-move meter (shadow), branch claude/live-forecast-20261028.
# Display only: no order, stop, Gamma, CAS, Shield, Angel One or weight change; the meter is logged
# for measurement and never feeds the band. Install AFTER the 27-Oct option-chain autofix (PR #55).
# Run on EC2 after 15:45 IST as ubuntu (sudo is used for the panel, gateway and restarts).
#   bash deploy_live_forecast_20261028.sh            # gates, backup, install, restart, health
#   bash deploy_live_forecast_20261028.sh --rollback
set -u
ROOT=${KADIMA_ROOT:-$HOME/kadima-py}; WWW=${KADIMA_WWW:-/var/www/html}
COMMIT=24033bc50a8f82f3c57c65b00b98f07c81cd5f30
PY=$ROOT/venv/bin/python
F=$ROOT/kadima/hourly_forecast.py; LF=$ROOT/kadima/live_forecast.py; HTML=$WWW/forecast.html
OLD_F=74d8f5834a1ee6ff3da9b74daf1bcc4fddbc474216fae1b94a101afe83517ef8
OLD_HTML=994d3b4435584270d7ebb5f4a73bea7d261f4310266665528f98077dc8de1662
NEW_F=abb20fc43fb2dd9c571bc256f036614f5aabd52446a3a101dc3857fe9f1d1122
NEW_LF=495a73637b273ae0f000f300f908b92e99c84b4ebe99e76e897513da04edb820
NEW_HTML=5fc0cdfd6825def064483b96773c1875ca71df63ce7e2add41d2adbed99005f2
MARK=$ROOT/.live-forecast-20261028-backup
h() { sha256sum "$1" 2>/dev/null | cut -c1-64; }
health() {
  sleep 25
  systemctl is-active --quiet kadima || { echo "HEALTH FAIL: kadima not active"; return 1; }
  systemctl is-active --quiet kadima-dashboard || { echo "HEALTH FAIL: kadima-dashboard not active"; return 1; }
  if journalctl -u kadima --since "-1 min" --no-pager | grep -q "Traceback"; then echo "HEALTH FAIL: traceback"; return 1; fi
  echo "health ok"; return 0
}
restore() {
  local BD=$1
  [ "$(h $BD/hourly_forecast.py)" = "$OLD_F" ] || { echo "ABORT: backup in $BD is not the expected predecessor"; return 1; }
  cp -p "$BD/hourly_forecast.py" "$F"; rm -f "$LF"; sudo cp "$BD/forecast.html" "$HTML"
  [ -f "$BD/api_installer.py" ] && sudo KADIMA_ROOT=$ROOT KADIMA_GATEWAY=${KADIMA_GATEWAY:-/opt/kadima-dashboard/gateway.py} $PY -I "$BD/api_installer.py" --rollback
  sudo systemctl restart kadima kadima-dashboard && health
  echo "restored: hourly_forecast.py $(h $F | cut -c1-12), panel $(h $HTML | cut -c1-12), live_forecast.py removed"
}
if [ "${1:-}" = "--rollback" ]; then
  [ -f "$MARK" ] || { echo "ABORT: no backup marker $MARK"; exit 1; }
  restore "$(cat $MARK)"; exit
fi
echo "== 1. gates"
echo "live hourly $(h $F | cut -c1-12)  panel $(h $HTML | cut -c1-12)  live_forecast $(h $LF | cut -c1-12)"
[ "$(h $F)" = "$NEW_F" ] && [ "$(h $LF)" = "$NEW_LF" ] && [ "$(h $HTML)" = "$NEW_HTML" ] && { echo "already installed"; exit 0; }
[ "$(h $F)" = "$OLD_F" ] || { echo "ABORT: hourly_forecast.py is not the 27-Oct release; nothing changed"; exit 1; }
[ "$(h $HTML)" = "$OLD_HTML" ] || { echo "ABORT: $HTML is not the 27-Oct panel; nothing changed"; exit 1; }
[ ! -f "$LF" ] || { echo "ABORT: $LF already exists; nothing changed"; exit 1; }
HM=$(TZ=Asia/Kolkata date +%H%M); DOW=$(TZ=Asia/Kolkata date +%u)
if [ "$DOW" -le 5 ] && [ "$HM" -ge 0900 ] && [ "$HM" -lt 1545 ]; then echo "ABORT: market hours (IST $HM); run after 15:45"; exit 1; fi
TMP=$(mktemp -d)
git -C "$ROOT" cat-file -e "$COMMIT" 2>/dev/null || { echo "ABORT: commit ${COMMIT:0:12} not fetched; run: git -C $ROOT fetch origin claude/live-forecast-20261028"; exit 1; }
git -C "$ROOT" show "$COMMIT:kadima/hourly_forecast.py" > "$TMP/hf.py"
git -C "$ROOT" show "$COMMIT:kadima/live_forecast.py" > "$TMP/lf.py"
git -C "$ROOT" show "$COMMIT:web/hourly_forecast.desk.html" > "$TMP/forecast.html"
git -C "$ROOT" show "$COMMIT:tools/install_live_forecast_api_20261028.py" > "$TMP/api_installer.py"
ok=1
[ "$(h $TMP/hf.py)" = "$NEW_F" ] || { echo "ABORT: hourly_forecast.py payload hash mismatch"; ok=0; }
[ "$(h $TMP/lf.py)" = "$NEW_LF" ] || { echo "ABORT: live_forecast.py payload hash mismatch"; ok=0; }
[ "$(h $TMP/forecast.html)" = "$NEW_HTML" ] || { echo "ABORT: forecast.html payload hash mismatch"; ok=0; }
for x in hf.py lf.py api_installer.py; do [ $ok = 1 ] && { $PY -m py_compile "$TMP/$x" || { echo "ABORT: $x does not compile"; ok=0; }; }; done
[ $ok = 1 ] && { sudo KADIMA_ROOT=$ROOT KADIMA_GATEWAY=${KADIMA_GATEWAY:-/opt/kadima-dashboard/gateway.py} $PY -I "$TMP/api_installer.py" --check || ok=0; }
[ $ok = 1 ] || { rm -rf "$TMP"; echo "nothing changed"; exit 1; }
free -m | head -3
read -r -p "No open positions and OK to restart kadima now? type YES: " OK; [ "$OK" = "YES" ] || { rm -rf "$TMP"; echo "stopped, nothing changed"; exit 1; }
echo "== 2. backup"
TS=$(date +%Y%m%d_%H%M%S); mkdir -p $HOME/backups; BD=$(mktemp -d $HOME/backups/live-forecast-$TS-XXXX)
cp -p "$F" "$HTML" "$BD/" && cp "$TMP/api_installer.py" "$BD/" || { echo "ABORT: backup failed"; exit 1; }
echo "$BD" > "$MARK"
echo "== 3. install"
cp "$TMP/lf.py" "$LF.new" && mv "$LF.new" "$LF"
cp "$TMP/hf.py" "$F.new-live" && mv "$F.new-live" "$F"
sudo cp "$TMP/forecast.html" "$HTML"
sudo KADIMA_ROOT=$ROOT KADIMA_GATEWAY=${KADIMA_GATEWAY:-/opt/kadima-dashboard/gateway.py} $PY -I "$TMP/api_installer.py" || { echo "== AUTO ROLLBACK (api patch failed)"; restore "$BD"; exit 1; }
rm -rf "$TMP"
if [ "$(h $F)" != "$NEW_F" ] || [ "$(h $LF)" != "$NEW_LF" ] || [ "$(h $HTML)" != "$NEW_HTML" ]; then
  echo "== AUTO ROLLBACK (files not as expected)"; restore "$BD"; exit 1
fi
echo "== 4. restart + health"
sudo systemctl restart kadima kadima-dashboard
if ! health; then echo "== AUTO ROLLBACK"; restore "$BD"; exit 1; fi
free -m | head -2
echo "DONE: live forecast line + sharp-move meter live from the next session (shadow). Backup $BD. Rollback: bash $0 --rollback"
