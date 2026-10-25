#!/bin/bash
# BOTTLENECK FIX v1 (2026-10-25). CONFIRMED trades skip the checks that duplicated the v2 rule.
#   bash deploy_bottleneck_fix_20261025.sh             # install, restart, health (auto-rollback)
#   bash deploy_bottleneck_fix_20261025.sh --rollback  # restore, restart
set -u
ROOT=${KADIMA_ROOT:-$HOME/kadima-py}; PY=$ROOT/venv/bin/python
SRC=$(cd "$(dirname "$0")/.." && pwd); T=$SRC/tools/install_bottleneck_fix_20261025.py
health() { sleep 25; systemctl is-active --quiet kadima || { echo "HEALTH FAIL: not active"; return 1; }
  journalctl -u kadima --since "-1 min" --no-pager | grep -q Traceback && { echo "HEALTH FAIL: traceback"; return 1; }; echo "health ok"; }
rollback() { "$PY" "$T" --root "$ROOT" --rollback; sudo systemctl restart kadima; health; }
[ "${1:-}" = "--rollback" ] && { rollback; exit; }
HM=$(TZ=Asia/Kolkata date +%H%M); DOW=$(TZ=Asia/Kolkata date +%u)
if [ "$DOW" -le 5 ] && [ "$HM" -ge 0900 ] && [ "$HM" -lt 1545 ]; then echo "ABORT: market hours"; exit 1; fi
"$PY" "$T" --root "$ROOT" --check || { echo "ABORT: checks; nothing changed"; exit 1; }
cp -p "$T" "$ROOT/tools/"
"$PY" "$T" --root "$ROOT" || { echo "ABORT: install failed"; exit 1; }
"$PY" -m py_compile "$ROOT/kadima/auto_executor.py" "$ROOT/kadima/sniper.py" "$ROOT/kadima/scalp_v2.py" "$ROOT/kadima/gamma_confirm.py" || { echo "ROLLING BACK: compile"; rollback; exit 1; }
sudo systemctl restart kadima
if ! health; then echo "ROLLING BACK"; rollback; exit 1; fi
cd "$ROOT" && "$PY" -c "from kadima import sniper, gamma_confirm as g; c=sniper.config(); print('sniper', c['mode'], 'confirmed_bypass', c['confirmed_bypass'], 'max_trades', c['max_trades'], 'stop_after_loss', c['stop_after_loss']); print('is_confirmed', hasattr(g,'is_confirmed'))" 2>&1 | grep -v smartConnect
grep -c "BOTTLENECK FIX v1" "$ROOT/kadima/auto_executor.py" "$ROOT/kadima/sniper.py" "$ROOT/kadima/scalp_v2.py" "$ROOT/kadima/gamma_confirm.py"
echo "DONE: BOTTLENECK FIX v1"
