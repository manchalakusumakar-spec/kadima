#!/bin/bash
# CONFIRMED MERGE v1 + SCALP CAP v1 (2026-10-28): merged CONFIRMED trades table in Celestial; index scalps max 2 lots.
#   bash deploy_confirmed_merge_20261028.sh             # install, restart, health (auto-rollback)
#   bash deploy_confirmed_merge_20261028.sh --rollback  # restore files + scalp_capital.json, restart
set -u
ROOT=${KADIMA_ROOT:-$HOME/kadima-py}; PY=$ROOT/venv/bin/python; DATA=/var/lib/kadima
HTML=/var/www/html/dashboard.html
SRC=$(cd "$(dirname "$0")/.." && pwd); T=$SRC/tools/install_confirmed_merge_20261028.py
SC=$DATA/scalp_capital.json; SCBAK=$DATA/scalp_capital.json.bak-confmerge-20261028
health() { sleep 25; systemctl is-active --quiet kadima || { echo "HEALTH FAIL: not active"; return 1; }
  journalctl -u kadima --since "-1 min" --no-pager | grep -q Traceback && { echo "HEALTH FAIL: traceback"; return 1; }; echo "health ok"; }
rollback() { sudo "$PY" "$T" --root "$ROOT" --html "$HTML" --rollback; [ -f $SCBAK ] && sudo cp -p $SCBAK $SC; sudo systemctl restart kadima; health; }
[ "${1:-}" = "--rollback" ] && { rollback; exit; }
HM=$(TZ=Asia/Kolkata date +%H%M); DOW=$(TZ=Asia/Kolkata date +%u)
if [ "$DOW" -le 5 ] && [ "$HM" -ge 0900 ] && [ "$HM" -lt 1545 ]; then echo "ABORT: market hours"; exit 1; fi
sudo "$PY" "$T" --root "$ROOT" --html "$HTML" --check || { echo "ABORT: checks; nothing changed"; exit 1; }
cp -p "$T" "$ROOT/tools/"
sudo "$PY" "$T" --root "$ROOT" --html "$HTML" || { echo "ABORT: install failed"; exit 1; }
"$PY" -m py_compile "$ROOT/kadima/trade_audit.py" "$ROOT/kadima/auto_executor.py" || { echo "ROLLING BACK: compile"; rollback; exit 1; }
sudo cp -p $SC $SCBAK
sudo "$PY" -c "import json;p='$SC';d=json.load(open(p));d['max_lots']=2;open(p,'w').write(json.dumps(d))"
sudo systemctl restart kadima
if ! health; then echo "ROLLING BACK"; rollback; exit 1; fi
cat $SC; echo
cd "$ROOT" && "$PY" -c "
from kadima import trade_audit as t, auto_executor as a
r=t.report(strategy='CONFIRMED'); print('confirmed view ok, rows today', r['total'])
print('scalp max lots', a._scalp_max_lots())
c=t.connection(); print('confirmed_at column', 'confirmed_at' in {x[1] for x in c.execute('PRAGMA table_info(trade_audit_plans)')})
" 2>&1 | grep -v smartConnect
grep -c 'CONFIRMED MERGE v1' $HTML | sed 's/^/dashboard marks: /'
echo "DONE: CONFIRMED MERGE v1"
