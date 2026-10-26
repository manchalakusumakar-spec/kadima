#!/bin/bash
# CONFIRM v3 (2026-10-26): gamma HHI >= 0.09 back on top of v2, scalp 70-90. Run on EC2 outside market hours.
#   bash deploy_confirm_v3_20261026.sh             # install, restart, health (auto-rollback on failure)
#   bash deploy_confirm_v3_20261026.sh --rollback  # restore v2 files, restart
set -u
ROOT=${KADIMA_ROOT:-$HOME/kadima-py}; PY=$ROOT/venv/bin/python; DATA=${KADIMA_DATA:-/var/lib/kadima}
SRC=$(cd "$(dirname "$0")/.." && pwd); T=$SRC/tools/install_confirm_v3_20261026.py
health() { sleep 25; systemctl is-active --quiet kadima || { echo "HEALTH FAIL: not active"; return 1; }
  journalctl -u kadima --since "-1 min" --no-pager | grep -q Traceback && { echo "HEALTH FAIL: traceback"; return 1; }; echo "health ok"; }
rollback() { sudo "$PY" "$T" --root "$ROOT" --data "$DATA" --rollback; sudo systemctl restart kadima; health; }
[ "${1:-}" = "--rollback" ] && { rollback; exit; }
HM=$(TZ=Asia/Kolkata date +%H%M); DOW=$(TZ=Asia/Kolkata date +%u)
if [ "$DOW" -le 5 ] && [ "$HM" -ge 0900 ] && [ "$HM" -lt 1545 ]; then echo "ABORT: market hours"; exit 1; fi
sudo "$PY" "$T" --root "$ROOT" --data "$DATA" --check || { echo "ABORT: checks; nothing changed"; exit 1; }
cp -p "$T" "$ROOT/tools/"
sudo "$PY" "$T" --root "$ROOT" --data "$DATA" || { echo "ABORT: install failed"; exit 1; }
"$PY" -m py_compile "$ROOT/kadima/gamma_confirm.py" "$ROOT/kadima/scalp_v2.py" || { echo "ROLLING BACK: compile"; rollback; exit 1; }
sudo systemctl restart kadima
if ! health; then echo "ROLLING BACK"; rollback; exit 1; fi
cd "$ROOT" && "$PY" -c "from kadima import gamma_confirm as g, scalp_v2 as s; c=g.config(); print('gamma', c); print('scalp', s.CONFIRM_SCORE, s.CONFIRM_WINDOW); assert c['min_hhi']==0.09 and c['require_direction'] and s.CONFIRM_SCORE==(70.0, 90.0)" \
  || { echo "ROLLING BACK: live values are not v3"; rollback; exit 1; }
echo "DONE: CONFIRM v3 live"
