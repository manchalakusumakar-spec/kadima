#!/bin/bash
# KADIMA day forecast 13-Nov (branch claude/day-anchor-0920-20261113), Kusumakar's YES on 13-Nov:
# official day path from the verified 09:20 close (09:15 path provisional, kept for review), and
# a fast reforecast until 10:15 when one candle is max(30, half the 09:15 range) off the path.
# Only kadima/day_forecast.py changes. Display/forecast only: no trading, Gamma Blast, CAS, Shield,
# stop-loss, order, capital, login or weight change. Run on EC2 after 15:45 IST, no open orders.
#   AUTOFIX_COMMIT=<commit> bash deploy_day_anchor_20261113.sh   # gates, backup, install, restart, health
#   bash deploy_day_anchor_20261113.sh --rollback                 # restore the file, restart
set -u
ROOT=${KADIMA_ROOT:-$HOME/kadima-py}
BRANCH=claude/day-anchor-0920-20261113
COMMIT=${AUTOFIX_COMMIT:-}
PY=$ROOT/venv/bin/python
DFP=$ROOT/kadima/day_forecast.py
OLD_DF=396af2035e1c9f55577a942f63d47781cd97e3e1bea065021766c24680f9f7d6
NEW_DF=cac290381fa4a97de652b09a5444388f4579014c2017c2d708ea5aca3cd4df30
LIVE_HF=2a13903bc7de2741a60f5500ed4651fa0b6af75017f9311af887ae12983856d4
LIVE_MD=0a1f4a3999ae081163192b64ccbb1989dcfa21252039eb88865bb21cd4f2e332
MARK=$ROOT/.day-anchor-20261113-backup
h() { sha256sum "$1" 2>/dev/null | cut -c1-64; }
health() {
  sleep 25
  systemctl is-active --quiet kadima || { echo "HEALTH FAIL: kadima not active"; return 1; }
  if journalctl -u kadima --since "-1 min" --no-pager | grep -q "Traceback"; then echo "HEALTH FAIL: traceback"; return 1; fi
  (cd "$ROOT" && $PY -I -c "import sys; sys.path.insert(0,'.'); import kadima.day_forecast as D; assert D.ANCHOR_0920 and D.OFFICIAL_K==1" 2>&1) || { echo "HEALTH FAIL: import"; return 1; }
  echo "health ok"; return 0
}
restore() {
  local BD=$1
  [ "$(h $BD/day_forecast.py)" = "$OLD_DF" ] || { echo "ABORT: backup in $BD is not the expected predecessor"; return 1; }
  cp -p "$BD/day_forecast.py" "$DFP"
  sudo systemctl restart kadima; sleep 20; systemctl is-active --quiet kadima && echo "restored day_forecast.py $(h $DFP | cut -c1-12)"
}
if [ "${1:-}" = "--rollback" ]; then
  [ -f "$MARK" ] || { echo "ABORT: no backup marker $MARK"; exit 1; }
  restore "$(cat $MARK)"; exit
fi
[ -n "$COMMIT" ] || { echo "ABORT: set AUTOFIX_COMMIT to the reviewed commit of $BRANCH"; exit 1; }
echo "== 1. gates"
echo "live day_forecast.py $(h $DFP | cut -c1-12)"
[ "$(h $DFP)" = "$NEW_DF" ] && { echo "already installed"; exit 0; }
[ "$(h $DFP)" = "$OLD_DF" ] || { echo "ABORT: day_forecast.py is not the expected live version; nothing changed"; exit 1; }
[ "$(h $ROOT/kadima/hourly_forecast.py)" = "$LIVE_HF" ] && [ "$(h $ROOT/kadima/hourly_forecast_model.py)" = "$LIVE_MD" ] \
  || { echo "ABORT: live forecast files are not the expected versions; nothing changed"; exit 1; }
HM=$(TZ=Asia/Kolkata date +%H%M); DOW=$(TZ=Asia/Kolkata date +%u)
if [ "$DOW" -le 5 ] && [ "$HM" -ge 0900 ] && [ "$HM" -lt 1545 ]; then echo "ABORT: market hours (IST $HM); run after 15:45"; exit 1; fi
git -C "$ROOT" cat-file -e "$COMMIT" 2>/dev/null || { echo "ABORT: commit ${COMMIT:0:12} not fetched; run: git -C $ROOT fetch origin $BRANCH"; exit 1; }
TMP=$(mktemp -d)
git -C "$ROOT" show "$COMMIT:kadima/day_forecast.py" > "$TMP/df.py"
[ "$(h $TMP/df.py)" = "$NEW_DF" ] || { echo "ABORT: payload hash mismatch; nothing changed"; rm -rf "$TMP"; exit 1; }
$PY -m py_compile "$TMP/df.py" || { echo "ABORT: payload does not compile; nothing changed"; rm -rf "$TMP"; exit 1; }
if [ "${AUTOFIX_CONFIRMED:-}" != "YES" ]; then
  read -r -p "No open orders/positions and OK to restart kadima now? type YES: " OK
  [ "$OK" = "YES" ] || { rm -rf "$TMP"; echo "stopped, nothing changed"; exit 1; }
fi
echo "== 2. backup"
TS=$(date +%Y%m%d_%H%M%S); mkdir -p $HOME/backups; BD=$(mktemp -d $HOME/backups/day-anchor-20261113-$TS-XXXX)
cp -p "$DFP" "$BD/" || { echo "ABORT: backup failed"; rm -rf "$TMP"; exit 1; }
echo "$BD" > "$MARK"; sha256sum "$BD"/*.py
echo "== 3. install"
cp "$TMP/df.py" "$DFP.new-fix" && mv "$DFP.new-fix" "$DFP"; rm -rf "$TMP"
[ "$(h $DFP)" = "$NEW_DF" ] || { echo "== AUTO ROLLBACK (file not as expected after install)"; restore "$BD"; exit 1; }
echo "== 4. restart + health"
sudo systemctl restart kadima
if ! health; then echo "== AUTO ROLLBACK"; restore "$BD"; exit 1; fi
echo "DONE: 09:20 official day anchor live from the next session. Backup in $BD. Rollback: bash $0 --rollback"
