#!/bin/bash
# KADIMA day path forecast + expected 5-minute swings on the hourly blue line
# (branch claude/day-forecast-20261023, payload commit 3c1fbed3f610aee3ca40ffca0eab3c71696cfbee).
# Display only. Replaces kadima/hourly_forecast.py, kadima/hourly_forecast_model.py and
# /var/www/html/forecast.html; adds kadima/day_forecast.py (and kadima/nifty_breadth.py, PR #49,
# if it is not already there). Accepts the live files as PR #48/#49 (incl. the named-columns hotfix and the
# read-only desk panel now live) or the 21-Oct swings release. Installs the read-only desk panel variant.
# No Gamma Blast, CAS, Shield, stop-loss, Angel One or trading change. Run on EC2 after 15:45 IST.
#   bash deploy_day_forecast_20261023.sh            # gates, backup, install, restart, health
#   bash deploy_day_forecast_20261023.sh --rollback # restore the backups and restart
set -u
ROOT=${KADIMA_ROOT:-$HOME/kadima-py}; DATA=${KADIMA_DATA_DIR:-/var/lib/kadima}; WWW=${KADIMA_WWW:-/var/www/html}
COMMIT=3c1fbed3f610aee3ca40ffca0eab3c71696cfbee
F=$ROOT/kadima/hourly_forecast.py; MD=$ROOT/kadima/hourly_forecast_model.py
DFP=$ROOT/kadima/day_forecast.py; BR=$ROOT/kadima/nifty_breadth.py; HTML=$WWW/forecast.html
OK_F="d73c49b5637922aeed6504d4d6a766f7fac22349fe35994cdc02413254d88870 16b92751a7a1dfc024ac09fdeaa4fb7759943b587452e98912968e64022598e8 85b70cb8d71d446db214d4f84d69f07e253b9fba1cabc5b6a2c23ddee7cf32a2 9944d5bb2f713813846349937427bd7a5730a0f8c5feb1343f5b4cbfc0bd7486"
OK_MD="4856671e3fec23a113648ba0ff1d649a8b49d22e3375f58a0d6636101be11fd3 ce3126133cfbb8b3dcb8ca2508831174a5efb365ccdd7c3026889317370c5f27"
OK_HTML="e00270f44731b9dda53f4797e82ae4acf77f715de33b2affe1677779433f4c06 c8e88118879903e42c11fb0bcdae25d7b232e565fad58741b30d61c6b80e4f6f afb60587bccd3d9c4581af0cb0a9273e786c6af52acf6f4f1b36cbd4ba81bb9b"
NEW_F=64740d3aa5e9d966a1a99d3d41bbc2c39e5ceb686c82698fc91919a8304d9639
NEW_MD=e421503a75ef696c6e6c3b4773fc878752df2b9ac97020ee516e07d6fea2a56c
NEW_HTML=a6f5edcb07beb198052c8c605c348b244a51b1b544b91c2d37faa344fb24e0dd
NEW_DF=1296b5dbaafde690fd087688831a866570e92f82d76c71a7d587c259a8ee9916
BR_SHA=50abc24f2ff0e61d44cc4bda14063032e4687a9122263addf867a7d27b7ee2a4
MARK=$ROOT/.day-forecast-20261023-backup
h() { sha256sum "$1" 2>/dev/null | cut -c1-64; }
in_list() { case " $2 " in *" $1 "*) return 0;; esac; return 1; }
health() {
  sleep 25
  systemctl is-active --quiet kadima || { echo "HEALTH FAIL: kadima not active"; return 1; }
  if journalctl -u kadima --since "-1 min" --no-pager | grep -q "Traceback"; then echo "HEALTH FAIL: traceback"; return 1; fi
  echo "health ok"; return 0
}
restore() {   # $1 = backup dir
  local BD=$1
  in_list "$(h $BD/hourly_forecast.py)" "$OK_F" || { echo "ABORT: backup in $BD is not a known predecessor"; return 1; }
  cp -p "$BD/hourly_forecast.py" "$F"; cp -p "$BD/hourly_forecast_model.py" "$MD"; rm -f "$DFP"
  [ -f "$BD/added_nifty_breadth" ] && rm -f "$BR"
  sudo cp "$BD/forecast.html" "$HTML"
  sudo systemctl restart kadima && health
  echo "restored: hourly_forecast.py $(h $F | cut -c1-12), model $(h $MD | cut -c1-12), forecast.html $(h $HTML | cut -c1-12); day_forecast.py removed"
}
if [ "${1:-}" = "--rollback" ]; then
  [ -f "$MARK" ] || { echo "ABORT: no backup marker $MARK"; exit 1; }
  restore "$(cat $MARK)"; exit
fi
echo "== 1. gates"
echo "live $(h $F | cut -c1-12)  model $(h $MD | cut -c1-12)  panel $(h $HTML | cut -c1-12)  day $(h $DFP | cut -c1-12)"
[ "$(h $F)" = "$NEW_F" ] && [ "$(h $MD)" = "$NEW_MD" ] && [ "$(h $HTML)" = "$NEW_HTML" ] && [ "$(h $DFP)" = "$NEW_DF" ] && { echo "already installed"; exit 0; }
in_list "$(h $F)" "$OK_F" || { echo "ABORT: hourly_forecast.py is not PR #48/#49 or the swings release; nothing changed"; exit 1; }
in_list "$(h $MD)" "$OK_MD" || { echo "ABORT: hourly_forecast_model.py is not a reviewed version; nothing changed"; exit 1; }
in_list "$(h $HTML)" "$OK_HTML" || { echo "ABORT: $HTML is not the PR #48 or swings panel; nothing changed"; exit 1; }
[ ! -f "$DFP" ] || { echo "ABORT: $DFP already exists and is not this release; nothing changed"; exit 1; }
[ ! -f "$BR" ] || [ "$(h $BR)" = "$BR_SHA" ] || { echo "ABORT: nifty_breadth.py is not the PR #49 version; nothing changed"; exit 1; }
HM=$(TZ=Asia/Kolkata date +%H%M); DOW=$(TZ=Asia/Kolkata date +%u)
if [ "$DOW" -le 5 ] && [ "$HM" -ge 0900 ] && [ "$HM" -lt 1545 ]; then echo "ABORT: market hours (IST $HM); run after 15:45"; exit 1; fi
TMP=$(mktemp -d)
git -C "$ROOT" cat-file -e "$COMMIT" 2>/dev/null || { echo "ABORT: commit ${COMMIT:0:12} not fetched; run: git -C $ROOT fetch origin claude/day-forecast-20261023"; exit 1; }
git -C "$ROOT" show "$COMMIT:kadima/hourly_forecast.py" > "$TMP/hf.py"
git -C "$ROOT" show "$COMMIT:kadima/hourly_forecast_model.py" > "$TMP/model.py"
git -C "$ROOT" show "$COMMIT:kadima/day_forecast.py" > "$TMP/df.py"
git -C "$ROOT" show "$COMMIT:web/hourly_forecast.desk.html" > "$TMP/forecast.html"   # read-only desk panel (no token, no trade buttons), as live
git -C "$ROOT" show "$COMMIT:kadima/nifty_breadth.py" > "$TMP/nb.py"
ok=1
[ "$(h $TMP/hf.py)" = "$NEW_F" ] || { echo "ABORT: hourly_forecast.py payload hash mismatch"; ok=0; }
[ "$(h $TMP/model.py)" = "$NEW_MD" ] || { echo "ABORT: model payload hash mismatch"; ok=0; }
[ "$(h $TMP/df.py)" = "$NEW_DF" ] || { echo "ABORT: day_forecast payload hash mismatch"; ok=0; }
[ "$(h $TMP/forecast.html)" = "$NEW_HTML" ] || { echo "ABORT: forecast.html payload hash mismatch"; ok=0; }
[ "$(h $TMP/nb.py)" = "$BR_SHA" ] || { echo "ABORT: nifty_breadth payload hash mismatch"; ok=0; }
for x in hf.py model.py df.py nb.py; do [ $ok = 1 ] && { $ROOT/venv/bin/python -m py_compile "$TMP/$x" || { echo "ABORT: $x does not compile"; ok=0; }; }; done
[ $ok = 1 ] || { rm -rf "$TMP"; echo "nothing changed"; exit 1; }
free -m | head -3
read -r -p "No open positions and OK to restart kadima now? type YES: " OK; [ "$OK" = "YES" ] || { rm -rf "$TMP"; echo "stopped, nothing changed"; exit 1; }
echo "== 2. backup"
TS=$(date +%Y%m%d_%H%M%S); mkdir -p $HOME/backups; BD=$(mktemp -d $HOME/backups/day-forecast-$TS-XXXX)
cp -p "$F" "$MD" "$HTML" "$BD/" || { echo "ABORT: backup failed"; exit 1; }
[ -f "$BR" ] || touch "$BD/added_nifty_breadth"
sqlite3 $DATA/forecast.db ".backup '$BD/forecast.db'" || echo "(forecast.db backup skipped)"
echo "$BD" > "$MARK"; sha256sum $BD/*.py $BD/*.html
echo "== 3. install"
[ -f "$BR" ] || { cp "$TMP/nb.py" "$BR.new" && mv "$BR.new" "$BR"; }
cp "$TMP/df.py" "$DFP.new-day" && mv "$DFP.new-day" "$DFP"
cp "$TMP/model.py" "$MD.new-day" && mv "$MD.new-day" "$MD"
cp "$TMP/hf.py" "$F.new-day" && mv "$F.new-day" "$F"
sudo cp "$TMP/forecast.html" "$HTML"
rm -rf "$TMP"
if [ "$(h $F)" != "$NEW_F" ] || [ "$(h $MD)" != "$NEW_MD" ] || [ "$(h $HTML)" != "$NEW_HTML" ] || [ "$(h $DFP)" != "$NEW_DF" ] || [ "$(h $BR)" != "$BR_SHA" ]; then
  echo "== AUTO ROLLBACK (files not as expected after install)"; restore "$BD"; exit 1
fi
echo "installed hourly_forecast.py ${NEW_F:0:12}, model ${NEW_MD:0:12}, day_forecast.py ${NEW_DF:0:12}, forecast.html ${NEW_HTML:0:12}"
echo "== 4. restart + health"
sudo systemctl restart kadima
if ! health; then echo "== AUTO ROLLBACK"; restore "$BD"; exit 1; fi
free -m | head -2
echo "DONE: day path + hourly swings live. The day card fills at 09:15 on the next session;"
echo "hourly slots show the 5-minute ups and downs from the next slot. Backup in $BD. Rollback: bash $0 --rollback"
