#!/bin/bash
# KADIMA expected 5-minute swings on the original forecast (PR #50 branch claude/forecast-swings-20261021,
# payload commit 466c4333dd7f1531ae47a43addb7f26f6dd46777). Forecast display only: the average path is
# still what is scored. Also fixes saving new forecasts on the migrated forecast.db (named columns).
# Replaces kadima/hourly_forecast.py, kadima/hourly_forecast_model.py and /var/www/html/forecast.html;
# adds kadima/nifty_breadth.py (PR #49 shadow, unchanged) if it is not already there.
# No Gamma Blast, CAS, Shield, Angel One or trading change. Run on EC2 after 15:45 IST.
#   bash deploy_forecast_swings_20261021.sh            # gates, backup, install, restart, health
#   bash deploy_forecast_swings_20261021.sh --rollback # restore the backups and restart
set -u
ROOT=${KADIMA_ROOT:-$HOME/kadima-py}; DATA=${KADIMA_DATA_DIR:-/var/lib/kadima}; WWW=${KADIMA_WWW:-/var/www/html}
COMMIT=466c4333dd7f1531ae47a43addb7f26f6dd46777
F=$ROOT/kadima/hourly_forecast.py; MD=$ROOT/kadima/hourly_forecast_model.py; BR=$ROOT/kadima/nifty_breadth.py
HTML=$WWW/forecast.html
PREV_F_48=16b92751a7a1dfc024ac09fdeaa4fb7759943b587452e98912968e64022598e8   # PR #48
PREV_F_49=85b70cb8d71d446db214d4f84d69f07e253b9fba1cabc5b6a2c23ddee7cf32a2   # PR #49
PREV_MD=4856671e3fec23a113648ba0ff1d649a8b49d22e3375f58a0d6636101be11fd3
PREV_HTML=c8e88118879903e42c11fb0bcdae25d7b232e565fad58741b30d61c6b80e4f6f
NEW_F=9944d5bb2f713813846349937427bd7a5730a0f8c5feb1343f5b4cbfc0bd7486
NEW_MD=ce3126133cfbb8b3dcb8ca2508831174a5efb365ccdd7c3026889317370c5f27
NEW_HTML=afb60587bccd3d9c4581af0cb0a9273e786c6af52acf6f4f1b36cbd4ba81bb9b
BR_SHA=50abc24f2ff0e61d44cc4bda14063032e4687a9122263addf867a7d27b7ee2a4
MARK=$ROOT/.swings-20261021-backup
h() { sha256sum "$1" 2>/dev/null | cut -c1-64; }
health() {
  sleep 25
  systemctl is-active --quiet kadima || { echo "HEALTH FAIL: kadima not active"; return 1; }
  if journalctl -u kadima --since "-1 min" --no-pager | grep -q "Traceback"; then echo "HEALTH FAIL: traceback"; return 1; fi
  echo "health ok"; return 0
}
restore() {   # $1 = backup dir
  local BD=$1
  case "$(h $BD/hourly_forecast.py)" in $PREV_F_48|$PREV_F_49) ;; *) echo "ABORT: backup in $BD is not a known predecessor"; return 1;; esac
  cp -p "$BD/hourly_forecast.py" "$F"; cp -p "$BD/hourly_forecast_model.py" "$MD"
  [ -f "$BD/added_nifty_breadth" ] && rm -f "$BR"
  sudo cp "$BD/forecast.html" "$HTML"
  sudo systemctl restart kadima && health
  echo "restored: hourly_forecast.py $(h $F | cut -c1-12), model $(h $MD | cut -c1-12), forecast.html $(h $HTML | cut -c1-12)"
}
if [ "${1:-}" = "--rollback" ]; then
  [ -f "$MARK" ] || { echo "ABORT: no backup marker $MARK"; exit 1; }
  restore "$(cat $MARK)"; exit
fi
echo "== 1. gates"
CUR=$(h $F); echo "live ${CUR:0:12}  model $(h $MD | cut -c1-12)  panel $(h $HTML | cut -c1-12)  release ${NEW_F:0:12}"
[ "$CUR" = "$NEW_F" ] && [ "$(h $MD)" = "$NEW_MD" ] && [ "$(h $HTML)" = "$NEW_HTML" ] && { echo "already installed"; exit 0; }
case "$CUR" in $PREV_F_48|$PREV_F_49) ;; *) echo "ABORT: hourly_forecast.py is not PR #48 or #49; nothing changed"; exit 1;; esac
[ "$(h $MD)" = "$PREV_MD" ] || { echo "ABORT: hourly_forecast_model.py is not the reviewed version; nothing changed"; exit 1; }
[ "$(h $HTML)" = "$PREV_HTML" ] || { echo "ABORT: $HTML is not the PR #48 panel; nothing changed"; exit 1; }
[ ! -f "$BR" ] || [ "$(h $BR)" = "$BR_SHA" ] || { echo "ABORT: nifty_breadth.py is not the PR #49 version; nothing changed"; exit 1; }
HM=$(TZ=Asia/Kolkata date +%H%M); DOW=$(TZ=Asia/Kolkata date +%u)
if [ "$DOW" -le 5 ] && [ "$HM" -ge 0900 ] && [ "$HM" -lt 1545 ]; then echo "ABORT: market hours (IST $HM); run after 15:45"; exit 1; fi
TMP=$(mktemp -d)
git -C "$ROOT" cat-file -e "$COMMIT" 2>/dev/null || { echo "ABORT: commit ${COMMIT:0:12} not fetched; run: git -C $ROOT fetch origin claude/forecast-swings-20261021"; exit 1; }
git -C "$ROOT" show "$COMMIT:kadima/hourly_forecast.py" > "$TMP/hf.py"
git -C "$ROOT" show "$COMMIT:kadima/hourly_forecast_model.py" > "$TMP/model.py"
git -C "$ROOT" show "$COMMIT:web/hourly_forecast.html" > "$TMP/forecast.html"
git -C "$ROOT" show "$COMMIT:kadima/nifty_breadth.py" > "$TMP/nb.py"
ok=1
[ "$(h $TMP/hf.py)" = "$NEW_F" ] || { echo "ABORT: hourly_forecast.py payload hash mismatch"; ok=0; }
[ "$(h $TMP/model.py)" = "$NEW_MD" ] || { echo "ABORT: model payload hash mismatch"; ok=0; }
[ "$(h $TMP/forecast.html)" = "$NEW_HTML" ] || { echo "ABORT: forecast.html payload hash mismatch"; ok=0; }
[ "$(h $TMP/nb.py)" = "$BR_SHA" ] || { echo "ABORT: nifty_breadth payload hash mismatch"; ok=0; }
for x in hf.py model.py nb.py; do [ $ok = 1 ] && { $ROOT/venv/bin/python -m py_compile "$TMP/$x" || { echo "ABORT: $x does not compile"; ok=0; }; }; done
[ $ok = 1 ] || { rm -rf "$TMP"; echo "nothing changed"; exit 1; }
free -m | head -3
read -r -p "No open positions and OK to restart kadima now? type YES: " OK; [ "$OK" = "YES" ] || { rm -rf "$TMP"; echo "stopped, nothing changed"; exit 1; }
echo "== 2. backup"
TS=$(date +%Y%m%d_%H%M%S); mkdir -p $HOME/backups; BD=$(mktemp -d $HOME/backups/forecast-swings-$TS-XXXX)
cp -p "$F" "$MD" "$HTML" "$BD/" || { echo "ABORT: backup failed"; exit 1; }
[ -f "$BR" ] || touch "$BD/added_nifty_breadth"
sqlite3 $DATA/forecast.db ".backup '$BD/forecast.db'" || echo "(forecast.db backup skipped)"
echo "$BD" > "$MARK"; sha256sum $BD/*.py $BD/*.html
echo "== 3. install"
[ -f "$BR" ] || { cp "$TMP/nb.py" "$BR.new" && mv "$BR.new" "$BR"; }
cp "$TMP/model.py" "$MD.new-swings" && mv "$MD.new-swings" "$MD"
cp "$TMP/hf.py" "$F.new-swings" && mv "$F.new-swings" "$F"
sudo cp "$TMP/forecast.html" "$HTML"
rm -rf "$TMP"
if [ "$(h $F)" != "$NEW_F" ] || [ "$(h $MD)" != "$NEW_MD" ] || [ "$(h $HTML)" != "$NEW_HTML" ] || [ "$(h $BR)" != "$BR_SHA" ]; then
  echo "== AUTO ROLLBACK (files not as expected after install)"; restore "$BD"; exit 1
fi
echo "installed hourly_forecast.py ${NEW_F:0:12}, model ${NEW_MD:0:12}, forecast.html ${NEW_HTML:0:12}"
echo "== 4. restart + health"
sudo systemctl restart kadima
if ! health; then echo "== AUTO ROLLBACK"; restore "$BD"; exit 1; fi
free -m | head -2
echo "DONE: expected swings ${NEW_F:0:12} live. New forecasts from the next slot show the ups and downs;"
echo "slots made before this install keep the plain line. Backup in $BD. Rollback: bash $0 --rollback"
