#!/bin/bash
# GAMMA CONFIRM v1 + UNBLOCK (2026-10-25). Run on EC2 AFTER Sniper is installed, outside market hours.
#   bash deploy_gamma_confirm_20261025.sh            # install, gamma_confirm.json mode=live, restart, health
#   bash deploy_gamma_confirm_20261025.sh --shadow   # tier shown, no auto block (within 30 s)
#   bash deploy_gamma_confirm_20261025.sh --rollback # restore files, remove module, restart
set -u
ROOT=${KADIMA_ROOT:-$HOME/kadima-py}; DATA=/var/lib/kadima; PY=$ROOT/venv/bin/python
SRC=$(cd "$(dirname "$0")/.." && pwd)
setmode() { echo "{\"mode\": \"$1\"}" | sudo tee $DATA/gamma_confirm.json >/dev/null; sudo chown --reference=$DATA/kadima.db $DATA/gamma_confirm.json; echo "gamma_confirm.json mode=$1"; }
health() { sleep 25; systemctl is-active --quiet kadima || { echo "HEALTH FAIL: not active"; return 1; }
  journalctl -u kadima --since "-1 min" --no-pager | grep -q Traceback && { echo "HEALTH FAIL: traceback"; return 1; }; echo "health ok"; }
case "${1:-}" in
  --shadow) setmode shadow; exit;;
  --rollback) setmode off; "$PY" "$ROOT/tools/install_gamma_unblock_20261025.py" --root "$ROOT" --rollback; sudo systemctl restart kadima; health; exit;;
esac
HM=$(TZ=Asia/Kolkata date +%H%M); DOW=$(TZ=Asia/Kolkata date +%u)
if [ "$DOW" -le 5 ] && [ "$HM" -ge 0900 ] && [ "$HM" -lt 1545 ]; then echo "ABORT: market hours"; exit 1; fi
grep -q "SNIPER v1" "$ROOT/kadima/auto_executor.py" || { echo "ABORT: install Sniper first"; exit 1; }
"$PY" "$SRC/tools/install_gamma_unblock_20261025.py" --root "$ROOT" --check || { echo "ABORT: anchors; nothing changed"; exit 1; }
cp -p "$SRC/tools/install_gamma_unblock_20261025.py" "$ROOT/tools/"
"$PY" "$SRC/tools/install_gamma_unblock_20261025.py" --root "$ROOT" || { echo "ABORT: install failed"; exit 1; }
sudo chown --reference="$ROOT/kadima/auto_executor.py" "$ROOT/kadima/gamma_confirm.py"
"$PY" -m py_compile "$ROOT/kadima/gamma_confirm.py" "$ROOT/kadima/auto_executor.py" "$ROOT/kadima/celestial.py" "$ROOT/kadima/celestial_scanner.py" || {
  echo "ROLLING BACK: compile"; "$PY" "$ROOT/tools/install_gamma_unblock_20261025.py" --root "$ROOT" --rollback; exit 1; }
setmode live; sudo systemctl restart kadima
if ! health; then echo "ROLLING BACK"; setmode off; "$PY" "$ROOT/tools/install_gamma_unblock_20261025.py" --root "$ROOT" --rollback; sudo systemctl restart kadima; health; exit 1; fi
echo "DONE: GAMMA CONFIRM v1 live"
