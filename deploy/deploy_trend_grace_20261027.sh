#!/bin/bash
# TREND GRACE v1 (2026-10-27): gamma direction = Shield OR day trend; scalp grace 5 min; v2 no-progress 10 min.
#   bash deploy_trend_grace_20261027.sh             # install, restart, health (auto-rollback)
#   bash deploy_trend_grace_20261027.sh --rollback  # restore files + best_entry.json, restart
set -u
ROOT=${KADIMA_ROOT:-$HOME/kadima-py}; PY=$ROOT/venv/bin/python; DATA=/var/lib/kadima
SRC=$(cd "$(dirname "$0")/.." && pwd); T=$SRC/tools/install_trend_grace_20261027.py
BE=$DATA/best_entry.json; BEBAK=$DATA/best_entry.json.bak-trendgrace-20261027
health() { sleep 25; systemctl is-active --quiet kadima || { echo "HEALTH FAIL: not active"; return 1; }
  journalctl -u kadima --since "-1 min" --no-pager | grep -q Traceback && { echo "HEALTH FAIL: traceback"; return 1; }; echo "health ok"; }
rollback() { "$PY" "$T" --root "$ROOT" --rollback; [ -f $BEBAK ] && sudo cp -p $BEBAK $BE; sudo systemctl restart kadima; health; }
[ "${1:-}" = "--rollback" ] && { rollback; exit; }
HM=$(TZ=Asia/Kolkata date +%H%M); DOW=$(TZ=Asia/Kolkata date +%u)
if [ "$DOW" -le 5 ] && [ "$HM" -ge 0900 ] && [ "$HM" -lt 1545 ]; then echo "ABORT: market hours"; exit 1; fi
"$PY" "$T" --root "$ROOT" --check || { echo "ABORT: checks; nothing changed"; exit 1; }
cp -p "$T" "$ROOT/tools/"
"$PY" "$T" --root "$ROOT" || { echo "ABORT: install failed"; exit 1; }
"$PY" -m py_compile "$ROOT/kadima/gamma_confirm.py" "$ROOT/kadima/fast_momentum.py" || { echo "ROLLING BACK: compile"; rollback; exit 1; }
sudo cp -p $BE $BEBAK
sudo "$PY" -c "import json;p='$BE';d=json.load(open(p));d['scalp_grace_min']=5.0;open(p,'w').write(json.dumps(d,indent=1))"
sudo systemctl restart kadima
if ! health; then echo "ROLLING BACK"; rollback; exit 1; fi
cat $BE; echo
cd "$ROOT" && "$PY" -c "from kadima import gamma_confirm as g, fast_momentum as f, best_entry as b; print('gamma', g.config()); print('trend_agrees', hasattr(g,'trend_agrees'), 'np v2', f.NO_PROGRESS_V2_SEC, 'scalp grace', b.config()['scalp_grace_min'], b.config()['mode'])" 2>&1 | grep -v smartConnect
echo "DONE: TREND GRACE v1"
