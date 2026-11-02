#!/bin/bash
# KADIMA Celestial 2-Nov night release (branch claude/electric-pulse-20261102). Display only.
#  * PULSE line drawn as an electric trace: GREEN where it rises, RED where it falls, crackling ~10x/s
#  * dynamic maximum stop-loss on the PULSE plan (recomputed every second, only tightens)
#  * flashing trade card (strike, premium, entry window, status, max SL, target, exit by, hold),
#    NOW strip (current numbers, flash on change), magnifier toggle (last 3 min -> next 6 min)
#  * forecast kept visible after market hours (last snapshot saved to forecast.db every minute)
# No order, sizing, stop, Gamma, CAS, Shield or Angel One change.
#   bash deploy_electric_pulse_20261102.sh             # gates, backup, install, restart, health
#   bash deploy_electric_pulse_20261102.sh --rollback  # restore all files, restart
set -u
ROOT=${KADIMA_ROOT:-$HOME/kadima-py}; WWW=${KADIMA_WWW:-/var/www/html}
COMMIT=${RELEASE_COMMIT:?set RELEASE_COMMIT to the reviewed commit of claude/electric-pulse-20261102}
PY=$ROOT/venv/bin/python
PF=$ROOT/kadima/pulse_forecast.py; LF=$ROOT/kadima/live_forecast.py
HTML=$WWW/forecast.html; EMB=$WWW/celestial_forecast_embed.js; DASH=$WWW/dashboard.html
OLD_PF=c1c04a5ba05e8d70aa3cca30a19c7d72371556d1861fd30050e118b550c44a37
OLD_LF=b32dcd128cf61d8a145637f829d0ff8486b8c3100326114a9c5cde0bfa6154db
OLD_HTML=7896781a80c2b03fd5796f64b1e4292b55e05a60ef91a379a36129d8bdc6d9f1
OLD_EMB=2e7284673924f981720d8b511a95cea86fc41eb47fe69fba304e9466bf690fb2
OLD_DASH=942e8280080c8459efe609fc15bde011eb002d09dd1fd7a34b892f4c37436d18
NEW_PF=d2838033fb90e308e18acb121e39001c73061fb5918cc761b095716f01debb86
NEW_LF=6690a2d7bdc37861b4e144c8fc2e9e8f48ed74b13c8d59c9de2eccc59d2fd70a
NEW_HTML=dd3d6ee10961c33838230cc45d4d0cdc30416fde18c7b24e76b8f2e8045d0593
MARK=$ROOT/.electric-pulse-20261102-backup
h() { sha256sum "$1" 2>/dev/null | cut -c1-64; }
health() {
  sleep 25
  systemctl is-active --quiet kadima || { echo "HEALTH FAIL: kadima not active"; return 1; }
  if journalctl -u kadima --since "-1 min" --no-pager | grep -q "Traceback"; then echo "HEALTH FAIL: traceback"; return 1; fi
  curl -s -m 10 "http://127.0.0.1:8082/forecast/live?index=NIFTY" | grep -q '"index"' || { echo "HEALTH FAIL: live endpoint"; return 1; }
  echo "health ok"; return 0
}
restore() {
  local BD=$1
  cp -p "$BD/pulse_forecast.py" "$PF"; cp -p "$BD/live_forecast.py" "$LF"
  sudo cp -p "$BD/forecast.html" "$HTML"; sudo cp -p "$BD/celestial_forecast_embed.js" "$EMB"; sudo cp -p "$BD/dashboard.html" "$DASH"
  sudo systemctl restart kadima && health
  echo "restored from $BD"
}
if [ "${1:-}" = "--rollback" ]; then
  [ -f "$MARK" ] || { echo "ABORT: no backup marker $MARK"; exit 1; }
  restore "$(cat $MARK)"; exit
fi
echo "== 1. gates"
[ "$(h $PF)" = "$NEW_PF" ] && [ "$(h $LF)" = "$NEW_LF" ] && [ "$(h $HTML)" = "$NEW_HTML" ] && { echo "already installed"; exit 0; }
ok=1
for pair in "$PF:$OLD_PF" "$LF:$OLD_LF" "$HTML:$OLD_HTML" "$EMB:$OLD_EMB" "$DASH:$OLD_DASH"; do
  f=${pair%%:*}; want=${pair##*:}
  [ "$(h $f)" = "$want" ] || { echo "ABORT: $f changed since the 22:30 snapshot ($(h $f | cut -c1-12)); nothing changed"; ok=0; }
done
[ $ok = 1 ] || exit 1
HM=$(TZ=Asia/Kolkata date +%H%M); DOW=$(TZ=Asia/Kolkata date +%u)
if [ "$DOW" -le 5 ] && [ "$HM" -ge 0900 ] && [ "$HM" -lt 1545 ]; then echo "ABORT: market hours (IST $HM); run after 15:45"; exit 1; fi
git -C "$ROOT" cat-file -e "$COMMIT" 2>/dev/null || { echo "ABORT: commit ${COMMIT:0:12} not fetched; run: git -C $ROOT fetch origin claude/electric-pulse-20261102"; exit 1; }
TMP=$(mktemp -d)
git -C "$ROOT" show "$COMMIT:kadima/pulse_forecast.py" > "$TMP/pf.py"
git -C "$ROOT" show "$COMMIT:kadima/live_forecast.py" > "$TMP/lf.py"
git -C "$ROOT" show "$COMMIT:www/forecast.html" > "$TMP/forecast.html"
[ "$(h $TMP/pf.py)" = "$NEW_PF" ] && [ "$(h $TMP/lf.py)" = "$NEW_LF" ] && [ "$(h $TMP/forecast.html)" = "$NEW_HTML" ] || { echo "ABORT: payload hash mismatch"; rm -rf "$TMP"; exit 1; }
$PY -m py_compile "$TMP/pf.py" "$TMP/lf.py" || { echo "ABORT: payload does not compile"; rm -rf "$TMP"; exit 1; }
cp "$EMB" "$TMP/emb.js"; cp "$DASH" "$TMP/dash.html"
sed -i 's/forecast\.html?embed=1&v=10/forecast.html?embed=1\&v=11/' "$TMP/emb.js"
sed -i 's/celestial_forecast_embed\.js?v=9/celestial_forecast_embed.js?v=11/' "$TMP/dash.html"
grep -q 'forecast.html?embed=1&v=11' "$TMP/emb.js" && grep -q 'celestial_forecast_embed.js?v=11' "$TMP/dash.html" || { echo "ABORT: cache-version bump failed"; rm -rf "$TMP"; exit 1; }
if [ "${RELEASE_CONFIRMED:-}" != "YES" ]; then
  read -r -p "No open orders/positions and OK to restart kadima now? type YES: " OK
  [ "$OK" = "YES" ] || { rm -rf "$TMP"; echo "stopped, nothing changed"; exit 1; }
fi
echo "== 2. backup"
TS=$(date +%Y%m%d_%H%M%S); mkdir -p $HOME/backups; BD=$(mktemp -d $HOME/backups/electric-pulse-$TS-XXXX)
cp -p "$PF" "$LF" "$BD/" && cp -p "$HTML" "$EMB" "$DASH" "$BD/" || { echo "ABORT: backup failed"; rm -rf "$TMP"; exit 1; }
echo "$BD" > "$MARK"
echo "== 3. install"
cp "$TMP/pf.py" "$PF"; cp "$TMP/lf.py" "$LF"
sudo cp "$TMP/forecast.html" "$HTML"; sudo cp "$TMP/emb.js" "$EMB"; sudo cp "$TMP/dash.html" "$DASH"
sudo chmod 664 "$HTML" "$EMB"; sudo chmod 644 "$DASH"
rm -rf "$TMP"
echo "== 4. restart + health"
sudo systemctl restart kadima
if ! health; then echo "== AUTO-ROLLBACK"; restore "$BD"; exit 1; fi
echo "DONE: electric PULSE, dynamic max SL, trade card, NOW strip, magnifier, after-hours view. Backup $BD. Rollback: bash $0 --rollback"
