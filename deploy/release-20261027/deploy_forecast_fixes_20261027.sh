#!/bin/bash
# KADIMA forecast fixes after the 26-Oct review (branch claude/forecast-fixes-20261027, payload 4af3469ec3e4f08e867819ca7941c3ee2e4cf589).
# Forecast/display only: no Gamma Blast, CAS, Shield, stop-loss, Angel One or trading change.
#  1. live inputs read once per loop (6 of 7 hourly slots were wrongly "late" on 26 Oct)
#  2. finished sessions missing 1-2 five-minute bars repaired for the similar-days search
#  3. self-tuning: no mid-session reweighting, confidence floor 0.40, 2% gain needed;
#     the 26-Oct 14:15 single-slot weight change is reverted to the 15-Oct weights
#  4. day reforecast needs two closed candles in a row beyond the limit
# Run on EC2 after 15:45 IST.
#   bash deploy_forecast_fixes_20261027.sh            # gates, backup, install, restart, health
#   bash deploy_forecast_fixes_20261027.sh --rollback # restore files and weights, restart
set -u
ROOT=${KADIMA_ROOT:-$HOME/kadima-py}; DATA=${KADIMA_DATA_DIR:-/var/lib/kadima}; WWW=${KADIMA_WWW:-/var/www/html}
COMMIT=4af3469ec3e4f08e867819ca7941c3ee2e4cf589
PY=$ROOT/venv/bin/python
F=$ROOT/kadima/hourly_forecast.py; MD=$ROOT/kadima/hourly_forecast_model.py; DFP=$ROOT/kadima/day_forecast.py
NT=$ROOT/tools/forecast_nightly.py; HTML=$WWW/forecast.html
OLD_F=64740d3aa5e9d966a1a99d3d41bbc2c39e5ceb686c82698fc91919a8304d9639
OLD_MD=e421503a75ef696c6e6c3b4773fc878752df2b9ac97020ee516e07d6fea2a56c
OLD_DF=1296b5dbaafde690fd087688831a866570e92f82d76c71a7d587c259a8ee9916
OLD_NT=c5062898066d9cf525440a01a982e35006d05a379582eb96220e9e3cc93655d5
OLD_HTML=9dbce546dbd4de26e70abff4936bc8b1f8e6ec083107fc082073d040fb953beb
NEW_F=74d8f5834a1ee6ff3da9b74daf1bcc4fddbc474216fae1b94a101afe83517ef8
NEW_MD=0a1f4a3999ae081163192b64ccbb1989dcfa21252039eb88865bb21cd4f2e332
NEW_DF=5402345148fdbe755c2bad51ea28eb4cd3db9427afca5d2a77eb797b34590358
NEW_NT=f0f3de9b44dd4382ea975dd4f89bfaeb4321b9b52c2680e8f9622b4131639999
NEW_HTML=994d3b4435584270d7ebb5f4a73bea7d261f4310266665528f98077dc8de1662
MARK=$ROOT/.forecast-fixes-20261027-backup
h() { sha256sum "$1" 2>/dev/null | cut -c1-64; }
health() {
  sleep 25
  systemctl is-active --quiet kadima || { echo "HEALTH FAIL: kadima not active"; return 1; }
  if journalctl -u kadima --since "-1 min" --no-pager | grep -q "Traceback"; then echo "HEALTH FAIL: traceback"; return 1; fi
  echo "health ok"; return 0
}
weights() {   # $1 = save <file> | restore-15oct | put <file>
  KADIMA_DATA_DIR=$DATA $PY -I - "$@" <<'PYEOF'
import sqlite3, sys, json, os
from datetime import datetime, timezone, timedelta
db = os.path.join(os.environ['KADIMA_DATA_DIR'], 'forecast.db')
c = sqlite3.connect(db, timeout=30)
now = datetime.now(timezone(timedelta(hours=5, minutes=30))).isoformat()
act = sys.argv[1]
if act == 'save':
    r = c.execute("select params, updated_at from hourly_forecast_model where index_name='NIFTY'").fetchone()
    json.dump({'params': r[0], 'updated_at': r[1]}, open(sys.argv[2], 'w')); print('weights saved', r[1])
elif act == 'restore-15oct':
    cur = c.execute("select updated_at from hourly_forecast_model where index_name='NIFTY'").fetchone()
    if not cur or not cur[0].startswith('2026-10-26T14:15'):
        print('weights: changed since 26-Oct 14:15 (', cur, '), left as they are'); sys.exit(0)
    r = c.execute("select params from hourly_forecast_model_history where index_name='NIFTY' and ts < '2026-10-26T14:15' "
                  "order by ts desc limit 1").fetchone()
    p = json.loads(r[0]); p['prob_c'] = max(p.get('prob_c', 0.4), 0.4)
    with c:
        c.execute("update hourly_forecast_model set params=?, updated_at=? where index_name='NIFTY'", (json.dumps(p), now))
        c.execute("insert into hourly_forecast_model_history values (?,?,?,?)", (now, 'NIFTY', json.dumps(p),
                  'restored 15-Oct weights: the 26-Oct 14:15 change came from a single mid-session slot (27-Oct guard)'))
    print('weights restored:', {k: p[k] for k in ('beta_reg','beta_analog','beta_tech','beta_shield','beta_trend','beta_oi','band_z','prob_c')})
elif act == 'put':
    s = json.load(open(sys.argv[2]))
    with c:
        c.execute("update hourly_forecast_model set params=?, updated_at=? where index_name='NIFTY'", (s['params'], s['updated_at']))
    print('weights put back', s['updated_at'])
PYEOF
}
restore() {   # $1 = backup dir
  local BD=$1
  [ "$(h $BD/hourly_forecast.py)" = "$OLD_F" ] || { echo "ABORT: backup in $BD is not the expected predecessor"; return 1; }
  cp -p "$BD/hourly_forecast.py" "$F"; cp -p "$BD/hourly_forecast_model.py" "$MD"; cp -p "$BD/day_forecast.py" "$DFP"
  [ -f "$BD/forecast_nightly.py" ] && cp -p "$BD/forecast_nightly.py" "$NT"
  sudo cp "$BD/forecast.html" "$HTML"
  [ -f "$BD/weights.json" ] && weights put "$BD/weights.json"
  sudo systemctl restart kadima && health
  echo "restored: hourly_forecast.py $(h $F | cut -c1-12), model $(h $MD | cut -c1-12), day $(h $DFP | cut -c1-12), panel $(h $HTML | cut -c1-12)"
}
if [ "${1:-}" = "--rollback" ]; then
  [ -f "$MARK" ] || { echo "ABORT: no backup marker $MARK"; exit 1; }
  restore "$(cat $MARK)"; exit
fi
echo "== 1. gates"
echo "live ${F##*/} $(h $F | cut -c1-12)  model $(h $MD | cut -c1-12)  day $(h $DFP | cut -c1-12)  nightly $(h $NT | cut -c1-12)  panel $(h $HTML | cut -c1-12)"
[ "$(h $F)" = "$NEW_F" ] && [ "$(h $MD)" = "$NEW_MD" ] && [ "$(h $DFP)" = "$NEW_DF" ] && [ "$(h $HTML)" = "$NEW_HTML" ] && { echo "already installed"; exit 0; }
[ "$(h $F)" = "$OLD_F" ] || { echo "ABORT: hourly_forecast.py is not the 23-Oct release; nothing changed"; exit 1; }
[ "$(h $MD)" = "$OLD_MD" ] || { echo "ABORT: hourly_forecast_model.py is not the 23-Oct release; nothing changed"; exit 1; }
[ "$(h $DFP)" = "$OLD_DF" ] || { echo "ABORT: day_forecast.py is not the 23-Oct release; nothing changed"; exit 1; }
[ "$(h $HTML)" = "$OLD_HTML" ] || { echo "ABORT: $HTML is not the 23-Oct panel; nothing changed"; exit 1; }
DO_NT=0; [ "$(h $NT)" = "$OLD_NT" ] && DO_NT=1 || echo "note: forecast_nightly.py differs from the repo; left unchanged (only the nightly gap repair is skipped)"
HM=$(TZ=Asia/Kolkata date +%H%M); DOW=$(TZ=Asia/Kolkata date +%u)
if [ "$DOW" -le 5 ] && [ "$HM" -ge 0900 ] && [ "$HM" -lt 1545 ]; then echo "ABORT: market hours (IST $HM); run after 15:45"; exit 1; fi
TMP=$(mktemp -d)
git -C "$ROOT" cat-file -e "$COMMIT" 2>/dev/null || { echo "ABORT: commit ${COMMIT:0:12} not fetched; run: git -C $ROOT fetch origin claude/forecast-fixes-20261027"; exit 1; }
git -C "$ROOT" show "$COMMIT:kadima/hourly_forecast.py" > "$TMP/hf.py"
git -C "$ROOT" show "$COMMIT:kadima/hourly_forecast_model.py" > "$TMP/model.py"
git -C "$ROOT" show "$COMMIT:kadima/day_forecast.py" > "$TMP/df.py"
git -C "$ROOT" show "$COMMIT:tools/forecast_nightly.py" > "$TMP/nt.py"
git -C "$ROOT" show "$COMMIT:web/hourly_forecast.desk.html" > "$TMP/forecast.html"
ok=1
[ "$(h $TMP/hf.py)" = "$NEW_F" ] || { echo "ABORT: hourly_forecast.py payload hash mismatch"; ok=0; }
[ "$(h $TMP/model.py)" = "$NEW_MD" ] || { echo "ABORT: model payload hash mismatch"; ok=0; }
[ "$(h $TMP/df.py)" = "$NEW_DF" ] || { echo "ABORT: day_forecast payload hash mismatch"; ok=0; }
[ "$(h $TMP/nt.py)" = "$NEW_NT" ] || { echo "ABORT: nightly payload hash mismatch"; ok=0; }
[ "$(h $TMP/forecast.html)" = "$NEW_HTML" ] || { echo "ABORT: forecast.html payload hash mismatch"; ok=0; }
for x in hf.py model.py df.py nt.py; do [ $ok = 1 ] && { $PY -m py_compile "$TMP/$x" || { echo "ABORT: $x does not compile"; ok=0; }; }; done
[ $ok = 1 ] || { rm -rf "$TMP"; echo "nothing changed"; exit 1; }
free -m | head -3
read -r -p "No open positions and OK to restart kadima now? type YES: " OK; [ "$OK" = "YES" ] || { rm -rf "$TMP"; echo "stopped, nothing changed"; exit 1; }
echo "== 2. backup"
TS=$(date +%Y%m%d_%H%M%S); mkdir -p $HOME/backups; BD=$(mktemp -d $HOME/backups/forecast-fixes-$TS-XXXX)
cp -p "$F" "$MD" "$DFP" "$HTML" "$BD/" || { echo "ABORT: backup failed"; exit 1; }
[ $DO_NT = 1 ] && cp -p "$NT" "$BD/"
weights save "$BD/weights.json" || { echo "ABORT: weights backup failed"; exit 1; }
KADIMA_DATA_DIR=$DATA $PY -I -c "import sqlite3,sys; s=sqlite3.connect('$DATA/forecast.db'); d=sqlite3.connect('$BD/forecast.db'); s.backup(d); print('forecast.db backed up')" || echo "(forecast.db backup skipped)"
echo "$BD" > "$MARK"; sha256sum $BD/*.py $BD/*.html
echo "== 3. install"
cp "$TMP/df.py" "$DFP.new-fix" && mv "$DFP.new-fix" "$DFP"
cp "$TMP/model.py" "$MD.new-fix" && mv "$MD.new-fix" "$MD"
cp "$TMP/hf.py" "$F.new-fix" && mv "$F.new-fix" "$F"
[ $DO_NT = 1 ] && { cp "$TMP/nt.py" "$NT.new-fix" && mv "$NT.new-fix" "$NT"; }
sudo cp "$TMP/forecast.html" "$HTML"
rm -rf "$TMP"
if [ "$(h $F)" != "$NEW_F" ] || [ "$(h $MD)" != "$NEW_MD" ] || [ "$(h $DFP)" != "$NEW_DF" ] || [ "$(h $HTML)" != "$NEW_HTML" ]; then
  echo "== AUTO ROLLBACK (files not as expected after install)"; restore "$BD"; exit 1
fi
weights restore-15oct
echo "installed hourly_forecast.py ${NEW_F:0:12}, model ${NEW_MD:0:12}, day ${NEW_DF:0:12}, panel ${NEW_HTML:0:12}, nightly $([ $DO_NT = 1 ] && echo ${NEW_NT:0:12} || echo unchanged)"
echo "== 4. restart + health"
sudo systemctl restart kadima
if ! health; then echo "== AUTO ROLLBACK"; restore "$BD"; exit 1; fi
free -m | head -2
echo "DONE: forecast fixes live from the next session. Backup in $BD. Rollback: bash $0 --rollback"
