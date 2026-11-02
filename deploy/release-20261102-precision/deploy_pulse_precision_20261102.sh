#!/bin/bash
# KADIMA PULSE PRECISION + all-index PULSE cards, 2-Nov night (branch claude/pulse-precision-20261102). Display only.
#  * move alarm: 5-min surge AND Marubozu agreeing with the day, not in the first 10 min (backtest: 510 days)
#  * projected move 1x median 5-min range (stretch 1.8x); stop room >= 1x median 5-min range
#  * every alarm scored RIGHT / WRONG / EXPIRED, stored (forecast.db pulse_alarms), 30-day precision shown
#  * PULSE for NIFTY, BANKNIFTY, SENSEX, MIDCPNIFTY, BANKEX; dashboard 'Observed Price History' replaced by PULSE cards
#  * incremental candles: constant cost per second
# No order, sizing, stop, Gamma, CAS, Shield or Angel One change.
#   bash deploy_pulse_precision_20261102.sh             # gates, backup, install, restart, health
#   bash deploy_pulse_precision_20261102.sh --rollback  # restore all files, restart
set -u
ROOT=${KADIMA_ROOT:-$HOME/kadima-py}; WWW=${KADIMA_WWW:-/var/www/html}
COMMIT=${RELEASE_COMMIT:?set RELEASE_COMMIT to the reviewed commit of claude/pulse-precision-20261102}
PY=$ROOT/venv/bin/python
PF=$ROOT/kadima/pulse_forecast.py; LF=$ROOT/kadima/live_forecast.py; CA=$ROOT/kadima/control_api.py
HTML=$WWW/forecast.html; EMB=$WWW/celestial_forecast_embed.js; DASH=$WWW/dashboard.html
OLD_PF=d2838033fb90e308e18acb121e39001c73061fb5918cc761b095716f01debb86
OLD_LF=6690a2d7bdc37861b4e144c8fc2e9e8f48ed74b13c8d59c9de2eccc59d2fd70a
OLD_CA=d4c1545e37019ad53165b07a11960164b425cacf91fc85ad44cf928d1f75a092
OLD_HTML=dd3d6ee10961c33838230cc45d4d0cdc30416fde18c7b24e76b8f2e8045d0593
OLD_EMB=cf41630dd011deeff1a999dac272fe32a5ad124b07be98b444d9013db8bf4141
OLD_DASH=be3c75f0056257c90365bc657d135dd21813066640085d40195e74bf9a46781a
NEW_PF=a59bf32c4d543ea0be8652d59a5784608df9a0e086c3a497838bd2a4eaba9de1
NEW_LF=debe17d985c113619f389adc112d4f77bf056728869dd97324cff626c7d05b9d
NEW_CA=687fd572fa084c8f28cedc5b3af548d72589ce7126e4dbecf7d4fd39ae55fc99
NEW_HTML=0bad2cffc970313036879f4c594281dba846ff998fa393c4c226ed46039f30a2
NEW_DASH=cdead44caff64e28a936605d2ce12b98f7e747d1bfbc0ad15564d0c3dfc89185
MARK=$ROOT/.pulse-precision-20261102-backup
h() { sha256sum "$1" 2>/dev/null | cut -c1-64; }
health() {
  sleep 25
  systemctl is-active --quiet kadima || { echo "HEALTH FAIL: kadima not active"; return 1; }
  if journalctl -u kadima --since "-1 min" --no-pager | grep -q "Traceback"; then echo "HEALTH FAIL: traceback"; return 1; fi
  curl -s -m 10 "http://127.0.0.1:8082/forecast/live?index=NIFTY" | grep -q '"index"' || { echo "HEALTH FAIL: NIFTY live endpoint"; return 1; }
  curl -s -m 10 "http://127.0.0.1:8082/forecast/live?index=BANKNIFTY" | grep -q '"index"' || { echo "HEALTH FAIL: BANKNIFTY live endpoint"; return 1; }
  echo "health ok"; return 0
}
restore() {
  local BD=$1
  cp -p "$BD/pulse_forecast.py" "$PF"; cp -p "$BD/live_forecast.py" "$LF"; cp -p "$BD/control_api.py" "$CA"
  sudo cp -p "$BD/forecast.html" "$HTML"; sudo cp -p "$BD/celestial_forecast_embed.js" "$EMB"; sudo cp -p "$BD/dashboard.html" "$DASH"
  sudo systemctl restart kadima && health
  echo "restored from $BD"
}
if [ "${1:-}" = "--rollback" ]; then
  [ -f "$MARK" ] || { echo "ABORT: no backup marker $MARK"; exit 1; }
  restore "$(cat $MARK)"; exit
fi
echo "== 1. gates"
[ "$(h $PF)" = "$NEW_PF" ] && [ "$(h $LF)" = "$NEW_LF" ] && [ "$(h $CA)" = "$NEW_CA" ] && [ "$(h $DASH)" = "$NEW_DASH" ] && { echo "already installed"; exit 0; }
ok=1
for pair in "$PF:$OLD_PF" "$LF:$OLD_LF" "$CA:$OLD_CA" "$HTML:$OLD_HTML" "$EMB:$OLD_EMB" "$DASH:$OLD_DASH"; do
  f=${pair%%:*}; want=${pair##*:}
  [ "$(h $f)" = "$want" ] || { echo "ABORT: $f is not the expected live version ($(h $f | cut -c1-12)); nothing changed"; ok=0; }
done
[ $ok = 1 ] || exit 1
HM=$(TZ=Asia/Kolkata date +%H%M); DOW=$(TZ=Asia/Kolkata date +%u)
if [ "$DOW" -le 5 ] && [ "$HM" -ge 0900 ] && [ "$HM" -lt 1545 ]; then echo "ABORT: market hours (IST $HM); run after 15:45"; exit 1; fi
git -C "$ROOT" cat-file -e "$COMMIT" 2>/dev/null || { echo "ABORT: commit ${COMMIT:0:12} not fetched; run: git -C $ROOT fetch origin claude/pulse-precision-20261102"; exit 1; }
TMP=$(mktemp -d)
for pair in "kadima/pulse_forecast.py:pf.py:$NEW_PF" "kadima/live_forecast.py:lf.py:$NEW_LF" "kadima/control_api.py:ca.py:$NEW_CA" \
            "www/forecast.html:forecast.html:$NEW_HTML" "www/dashboard.html:dashboard.html:$NEW_DASH"; do
  src=${pair%%:*}; rest=${pair#*:}; dst=${rest%%:*}; want=${rest#*:}
  git -C "$ROOT" show "$COMMIT:$src" > "$TMP/$dst"
  [ "$(h $TMP/$dst)" = "$want" ] || { echo "ABORT: payload hash mismatch for $src"; rm -rf "$TMP"; exit 1; }
done
$PY -m py_compile "$TMP/pf.py" "$TMP/lf.py" "$TMP/ca.py" || { echo "ABORT: payload does not compile"; rm -rf "$TMP"; exit 1; }
cp "$EMB" "$TMP/emb.js"; sed -i 's/forecast\.html?embed=1&v=11/forecast.html?embed=1\&v=12/' "$TMP/emb.js"
grep -q 'forecast.html?embed=1&v=12' "$TMP/emb.js" || { echo "ABORT: cache-version bump failed"; rm -rf "$TMP"; exit 1; }
if [ "${RELEASE_CONFIRMED:-}" != "YES" ]; then
  read -r -p "No open orders/positions and OK to restart kadima now? type YES: " OK
  [ "$OK" = "YES" ] || { rm -rf "$TMP"; echo "stopped, nothing changed"; exit 1; }
fi
echo "== 2. backup"
TS=$(date +%Y%m%d_%H%M%S); mkdir -p $HOME/backups; BD=$(mktemp -d $HOME/backups/pulse-precision-$TS-XXXX)
cp -p "$PF" "$LF" "$CA" "$BD/" && cp -p "$HTML" "$EMB" "$DASH" "$BD/" || { echo "ABORT: backup failed"; rm -rf "$TMP"; exit 1; }
echo "$BD" > "$MARK"
echo "== 3. install"
cp "$TMP/pf.py" "$PF"; cp "$TMP/lf.py" "$LF"; cp "$TMP/ca.py" "$CA"
sudo cp "$TMP/forecast.html" "$HTML"; sudo cp "$TMP/emb.js" "$EMB"; sudo cp "$TMP/dashboard.html" "$DASH"
sudo chmod 664 "$HTML" "$EMB"; sudo chmod 644 "$DASH"
rm -rf "$TMP"
echo "== 4. restart + health"
sudo systemctl restart kadima
if ! health; then echo "== AUTO-ROLLBACK"; restore "$BD"; exit 1; fi
echo "DONE: PULSE precision + all-index PULSE cards. Backup $BD. Rollback: bash $0 --rollback"
