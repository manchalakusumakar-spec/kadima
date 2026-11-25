#!/bin/bash
# KADIMA forecast 26-Nov (branch claude/forecast-nse-vix-20261126), trader: "use the data from NSE and
# improve the accuracy"; rollout chosen: "install if replay is better".
# - new kadima/nse_daily.py: NSE archive data per session (India VIX, NIFTY official OHLC,
#   participant-wise index OI, advances/declines) into forecast.db table nse_daily, nightly at 16:05.
# - kadima/hourly_forecast.py (anchored patch, tools/patch_nse_vix_20261126.py): the 09:15 hourly slot
#   uses 0.9 x India VIX as its IV when no ATM IV exists, and then skips the fixed 1.4x open widening.
#   Other slots, the day forecast, weights, trading, stops, orders, Gamma, CAS, Shield: unchanged.
# - tools/forecast_nightly.py: calls nse_daily after the bhavcopy step (never blocks the retune).
# Switch without a code change: KADIMA_VIX_OPEN=0 in the kadima service environment.
# Run on EC2 outside market hours, no open orders.
#   AUTOFIX_COMMIT=<commit> bash deploy_nse_vix_20261126.sh   # gates, backup, install, restart, health
#   bash deploy_nse_vix_20261126.sh --rollback                 # restore the 2 files, remove nse_daily.py, restart
set -u
ROOT=${KADIMA_ROOT:-$HOME/kadima-py}
BRANCH=claude/forecast-nse-vix-20261126
COMMIT=${AUTOFIX_COMMIT:-}
PY=$ROOT/venv/bin/python
HFP=$ROOT/kadima/hourly_forecast.py
NGP=$ROOT/tools/forecast_nightly.py
NDP=$ROOT/kadima/nse_daily.py
OLD_HF=0ec9e145771ba296e369a5dd3709e15a73c7e0504b112ce51995eccaead57e27
NEW_HF=a932b2d3fac9de0cbaa08dc7ce1154f45572309b4c64a32a184ca0535c6f39bc
OLD_NG=f0f3de9b44dd4382ea975dd4f89bfaeb4321b9b52c2680e8f9622b4131639999
NEW_NG=313d1de1dd526ba42ec7684740c98d3ce2aa8902d4a907dd3180c982b653eff1
NEW_ND=d95ee3e7e531734a938d7d565cff29f82d15bcba9a266b6922556322acab476f
LIVE_MD=0a1f4a3999ae081163192b64ccbb1989dcfa21252039eb88865bb21cd4f2e332
LIVE_DF=463770df7bb2cafae86258b1c02880a8850349e4bde71a636400e101c5461994
MARK=$ROOT/.nse-vix-20261126-backup
h() { sha256sum "$1" 2>/dev/null | cut -c1-64; }
health() {
  sleep 25
  systemctl is-active --quiet kadima || { echo "HEALTH FAIL: kadima not active"; return 1; }
  if journalctl -u kadima --since "-1 min" --no-pager | grep -q "Traceback"; then echo "HEALTH FAIL: traceback"; return 1; fi
  (cd "$ROOT" && $PY -I -c "import sys; sys.path.insert(0,'.'); import kadima.hourly_forecast as H, kadima.nse_daily as N; assert H.VIX_OPEN and callable(N.vix_for_forecast)" 2>&1) || { echo "HEALTH FAIL: import"; return 1; }
  echo "health ok"; return 0
}
restore() {
  local BD=$1
  [ "$(h $BD/hourly_forecast.py)" = "$OLD_HF" ] && [ "$(h $BD/forecast_nightly.py)" = "$OLD_NG" ] \
    || { echo "ABORT: backup in $BD is not the expected predecessor"; return 1; }
  cp -p "$BD/hourly_forecast.py" "$HFP"; cp -p "$BD/forecast_nightly.py" "$NGP"; rm -f "$NDP"
  sudo systemctl restart kadima; sleep 20; systemctl is-active --quiet kadima && echo "restored hourly_forecast.py $(h $HFP | cut -c1-12), forecast_nightly.py $(h $NGP | cut -c1-12)"
}
if [ "${1:-}" = "--rollback" ]; then
  [ -f "$MARK" ] || { echo "ABORT: no backup marker $MARK"; exit 1; }
  restore "$(cat $MARK)"; exit
fi
[ -n "$COMMIT" ] || { echo "ABORT: set AUTOFIX_COMMIT to the reviewed commit of $BRANCH"; exit 1; }
echo "== 1. gates"
echo "live hourly_forecast.py $(h $HFP | cut -c1-12)  forecast_nightly.py $(h $NGP | cut -c1-12)"
[ "$(h $HFP)" = "$NEW_HF" ] && [ "$(h $NGP)" = "$NEW_NG" ] && [ "$(h $NDP)" = "$NEW_ND" ] && { echo "already installed"; exit 0; }
[ "$(h $HFP)" = "$OLD_HF" ] && [ "$(h $NGP)" = "$OLD_NG" ] || { echo "ABORT: live files are not the expected versions; nothing changed"; exit 1; }
[ -e "$NDP" ] && { echo "ABORT: $NDP already exists; nothing changed"; exit 1; }
[ "$(h $ROOT/kadima/hourly_forecast_model.py)" = "$LIVE_MD" ] && [ "$(h $ROOT/kadima/day_forecast.py)" = "$LIVE_DF" ] \
  || { echo "ABORT: live model/day files are not the expected versions; nothing changed"; exit 1; }
HM=$(TZ=Asia/Kolkata date +%H%M); DOW=$(TZ=Asia/Kolkata date +%u)
if [ "$DOW" -le 5 ] && [ "$HM" -ge 0900 ] && [ "$HM" -lt 1545 ]; then echo "ABORT: market hours (IST $HM); run after 15:45"; exit 1; fi
git -C "$ROOT" cat-file -e "$COMMIT" 2>/dev/null || { echo "ABORT: commit ${COMMIT:0:12} not fetched; run: git -C $ROOT fetch origin $BRANCH"; exit 1; }
TMP=$(mktemp -d); mkdir -p "$TMP/kadima" "$TMP/tools"
git -C "$ROOT" show "$COMMIT:kadima/nse_daily.py" > "$TMP/kadima/nse_daily.py"
git -C "$ROOT" show "$COMMIT:tools/patch_nse_vix_20261126.py" > "$TMP/patch.py"
cp -p "$HFP" "$TMP/kadima/hourly_forecast.py"; cp -p "$NGP" "$TMP/tools/forecast_nightly.py"
$PY -I "$TMP/patch.py" "$TMP" || { echo "ABORT: patch did not apply; nothing changed"; rm -rf "$TMP"; exit 1; }
[ "$(h $TMP/kadima/hourly_forecast.py)" = "$NEW_HF" ] && [ "$(h $TMP/tools/forecast_nightly.py)" = "$NEW_NG" ] \
  && [ "$(h $TMP/kadima/nse_daily.py)" = "$NEW_ND" ] || { echo "ABORT: payload hash mismatch; nothing changed"; rm -rf "$TMP"; exit 1; }
$PY -m py_compile "$TMP/kadima/hourly_forecast.py" "$TMP/tools/forecast_nightly.py" "$TMP/kadima/nse_daily.py" \
  || { echo "ABORT: payload does not compile; nothing changed"; rm -rf "$TMP"; exit 1; }
if [ "${AUTOFIX_CONFIRMED:-}" != "YES" ]; then
  read -r -p "No open orders/positions and OK to restart kadima now? type YES: " OK
  [ "$OK" = "YES" ] || { rm -rf "$TMP"; echo "stopped, nothing changed"; exit 1; }
fi
echo "== 2. backup"
TS=$(date +%Y%m%d_%H%M%S); mkdir -p $HOME/backups; BD=$(mktemp -d $HOME/backups/nse-vix-20261126-$TS-XXXX)
cp -p "$HFP" "$NGP" "$BD/" || { echo "ABORT: backup failed"; rm -rf "$TMP"; exit 1; }
echo "$BD" > "$MARK"; sha256sum "$BD"/*.py
echo "== 3. install"
cp "$TMP/kadima/nse_daily.py" "$NDP.new-fix" && mv "$NDP.new-fix" "$NDP"
cp "$TMP/tools/forecast_nightly.py" "$NGP.new-fix" && mv "$NGP.new-fix" "$NGP"
cp "$TMP/kadima/hourly_forecast.py" "$HFP.new-fix" && mv "$HFP.new-fix" "$HFP"; rm -rf "$TMP"
[ "$(h $HFP)" = "$NEW_HF" ] && [ "$(h $NGP)" = "$NEW_NG" ] && [ "$(h $NDP)" = "$NEW_ND" ] \
  || { echo "== AUTO ROLLBACK (files not as expected after install)"; restore "$BD"; exit 1; }
echo "== 4. restart + health"
sudo systemctl restart kadima
if ! health; then echo "== AUTO ROLLBACK"; restore "$BD"; exit 1; fi
echo "== 5. NSE archive backfill (last 10 weekdays; read-only towards trading)"
(cd "$ROOT" && timeout 300 $PY -I -c "import sys; sys.path.insert(0,'.'); from kadima import nse_daily as N; sys.exit(N.main(['--backfill','10']))") || echo "backfill failed (harmless: the 09:15 slot then uses live VIX or the old 1.4x band)"
echo "DONE: 09:15 slot sized by India VIX from the next session. Backup in $BD. Rollback: bash $0 --rollback"
