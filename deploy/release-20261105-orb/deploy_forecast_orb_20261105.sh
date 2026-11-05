#!/bin/bash
# KADIMA forecast 05-Nov: SHADOW-ONLY opening-range breakout / closing-fade lean (branch
# claude/forecast-autofix-20261105). NEEDS KUSUMAKAR'S YES (model-side change, even though it is
# shadow only). At the 12:15, 13:15 and 15:15 NIFTY slot starts it records a lean in its own
# table hourly_forecast_orb_shadow. It does NOT change the hourly/day forecast, its band, the
# weights, stop-losses, Gamma Blast, CAS, Shield, orders, capital or any other index.
# Files: kadima/hourly_forecast.py (hook) + new kadima/forecast_orb.py.
# Run on EC2 after 15:45 IST with no open orders.
#   AUTOFIX_COMMIT=<commit> bash deploy_forecast_orb_20261105.sh   # gates, backup, install, restart, health
#   bash deploy_forecast_orb_20261105.sh --rollback                 # restore both files, restart
set -u
ROOT=${KADIMA_ROOT:-$HOME/kadima-py}
BRANCH=claude/forecast-autofix-20261105
COMMIT=${AUTOFIX_COMMIT:-}
PY=$ROOT/venv/bin/python
HFP=$ROOT/kadima/hourly_forecast.py
ORBP=$ROOT/kadima/forecast_orb.py
OLD_HF=75d8f22b7ffa6f01654acaa59d4be33357955dc30c9ac8c2aace73e5faf6af13
NEW_HF=483de536deed9c797534eeb249f4ace5f55b20fb382ef2faf53d012fa806871c
NEW_ORB=a81fb0884bd243a73db420a8b25dbb3527b87dc710675f360e7d0726b319dd37
LIVE_MD=0a1f4a3999ae081163192b64ccbb1989dcfa21252039eb88865bb21cd4f2e332
LIVE_DF=396af2035e1c9f55577a942f63d47781cd97e3e1bea065021766c24680f9f7d6
LIVE_CD=4dbc4849470c4e3239a53763dd4055a1c6158774539d634929978cf04606c7ca
MARK=$ROOT/.forecast-orb-20261105-backup
h() { sha256sum "$1" 2>/dev/null | cut -c1-64; }
health() {
  sleep 25
  systemctl is-active --quiet kadima || { echo "HEALTH FAIL: kadima not active"; return 1; }
  if journalctl -u kadima --since "-1 min" --no-pager | grep -q "Traceback"; then echo "HEALTH FAIL: traceback"; return 1; fi
  echo "health ok"; return 0
}
restore() {
  local BD=$1
  [ "$(h $BD/hourly_forecast.py)" = "$OLD_HF" ] || { echo "ABORT: backup in $BD is not the expected predecessor"; return 1; }
  cp -p "$BD/hourly_forecast.py" "$HFP"
  if [ -f "$BD/NO_ORB" ]; then rm -f "$ORBP"; else [ -f "$BD/forecast_orb.py" ] && cp -p "$BD/forecast_orb.py" "$ORBP"; fi
  sudo systemctl restart kadima && health
  echo "restored hourly_forecast.py $(h $HFP | cut -c1-12)"
}
if [ "${1:-}" = "--rollback" ]; then
  [ -f "$MARK" ] || { echo "ABORT: no backup marker $MARK"; exit 1; }
  restore "$(cat $MARK)"; exit
fi
[ -n "$COMMIT" ] || { echo "ABORT: set AUTOFIX_COMMIT to the reviewed commit of $BRANCH"; exit 1; }
echo "== 1. gates"
echo "live hourly_forecast.py $(h $HFP | cut -c1-12)"
[ "$(h $HFP)" = "$NEW_HF" ] && [ "$(h $ORBP)" = "$NEW_ORB" ] && { echo "already installed"; exit 0; }
[ "$(h $HFP)" = "$OLD_HF" ] || { echo "ABORT: hourly_forecast.py is not the expected live version; nothing changed"; exit 1; }
[ "$(h $ROOT/kadima/hourly_forecast_model.py)" = "$LIVE_MD" ] && [ "$(h $ROOT/kadima/day_forecast.py)" = "$LIVE_DF" ] \
  && [ "$(h $ROOT/kadima/forecast_chain_data.py)" = "$LIVE_CD" ] || { echo "ABORT: live forecast files are not the expected versions; nothing changed"; exit 1; }
HM=$(TZ=Asia/Kolkata date +%H%M); DOW=$(TZ=Asia/Kolkata date +%u)
if [ "$DOW" -le 5 ] && [ "$HM" -ge 0900 ] && [ "$HM" -lt 1545 ]; then echo "ABORT: market hours (IST $HM); run after 15:45"; exit 1; fi
git -C "$ROOT" cat-file -e "$COMMIT" 2>/dev/null || { echo "ABORT: commit ${COMMIT:0:12} not fetched; run: git -C $ROOT fetch origin $BRANCH"; exit 1; }
TMP=$(mktemp -d)
git -C "$ROOT" show "$COMMIT:kadima/hourly_forecast.py" > "$TMP/hf.py"
git -C "$ROOT" show "$COMMIT:kadima/forecast_orb.py" > "$TMP/orb.py"
[ "$(h $TMP/hf.py)" = "$NEW_HF" ] && [ "$(h $TMP/orb.py)" = "$NEW_ORB" ] || { echo "ABORT: payload hash mismatch; nothing changed"; rm -rf "$TMP"; exit 1; }
$PY -m py_compile "$TMP/hf.py" "$TMP/orb.py" || { echo "ABORT: payload does not compile; nothing changed"; rm -rf "$TMP"; exit 1; }
if [ "${AUTOFIX_CONFIRMED:-}" != "YES" ]; then
  read -r -p "No open orders/positions and OK to restart kadima now? type YES: " OK
  [ "$OK" = "YES" ] || { rm -rf "$TMP"; echo "stopped, nothing changed"; exit 1; }
fi
echo "== 2. backup"
TS=$(date +%Y%m%d_%H%M%S); mkdir -p $HOME/backups; BD=$(mktemp -d $HOME/backups/forecast-orb-20261105-$TS-XXXX)
cp -p "$HFP" "$BD/" || { echo "ABORT: backup failed"; rm -rf "$TMP"; exit 1; }
if [ -f "$ORBP" ]; then cp -p "$ORBP" "$BD/"; else touch "$BD/NO_ORB"; fi
echo "$BD" > "$MARK"; sha256sum "$BD"/*.py
echo "== 3. install"
cp "$TMP/orb.py" "$ORBP.new-fix" && mv "$ORBP.new-fix" "$ORBP"
cp "$TMP/hf.py" "$HFP.new-fix" && mv "$HFP.new-fix" "$HFP"; rm -rf "$TMP"
[ "$(h $HFP)" = "$NEW_HF" ] && [ "$(h $ORBP)" = "$NEW_ORB" ] || { echo "== AUTO ROLLBACK (files not as expected after install)"; restore "$BD"; exit 1; }
echo "== 4. restart + health"
sudo systemctl restart kadima
if ! health; then echo "== AUTO ROLLBACK"; restore "$BD"; exit 1; fi
echo "DONE: ORB shadow lean live from the next session (12:15, 13:15, 15:15). Backup in $BD. Rollback: bash $0 --rollback"
