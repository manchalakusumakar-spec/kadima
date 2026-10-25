#!/bin/bash
# LOSS CONTROLS v1 (2026-10-25): daily loss = min(10k, 30% of morning cash); no Sniper entry after a Sniper loss.
#   bash deploy_loss_controls_20261025.sh            # install, restart, health (auto-rollback)
#   bash deploy_loss_controls_20261025.sh --rollback
set -u
ROOT=${KADIMA_ROOT:-$HOME/kadima-py}; PY=$ROOT/venv/bin/python; SRC=$(cd "$(dirname "$0")/.." && pwd)
health() { sleep 25; systemctl is-active --quiet kadima || { echo "HEALTH FAIL"; return 1; }
  journalctl -u kadima --since "-1 min" --no-pager | grep -q Traceback && { echo "HEALTH FAIL: traceback"; return 1; }; echo "health ok"; }
if [ "${1:-}" = "--rollback" ]; then "$PY" "$ROOT/tools/install_loss_controls_20261025.py" --root "$ROOT" --rollback; sudo systemctl restart kadima; health; exit; fi
HM=$(TZ=Asia/Kolkata date +%H%M); DOW=$(TZ=Asia/Kolkata date +%u)
if [ "$DOW" -le 5 ] && [ "$HM" -ge 0900 ] && [ "$HM" -lt 1545 ]; then echo "ABORT: market hours"; exit 1; fi
grep -q "GAMMA CONFIRM v1" "$ROOT/kadima/auto_executor.py" || { echo "ABORT: install gamma confirm first"; exit 1; }
"$PY" "$SRC/tools/install_loss_controls_20261025.py" --root "$ROOT" --check || { echo "ABORT: anchors; nothing changed"; exit 1; }
cp -p "$SRC/tools/install_loss_controls_20261025.py" "$ROOT/tools/"
"$PY" "$SRC/tools/install_loss_controls_20261025.py" --root "$ROOT" || { echo "ABORT: install failed"; exit 1; }
sudo chown --reference="$ROOT/kadima/auto_executor.py" "$ROOT/kadima/daily_limit.py"
"$PY" -m py_compile "$ROOT/kadima/daily_limit.py" "$ROOT/kadima/auto_executor.py" "$ROOT/kadima/sniper.py" || {
  echo "ROLLING BACK: compile"; "$PY" "$ROOT/tools/install_loss_controls_20261025.py" --root "$ROOT" --rollback; exit 1; }
sudo systemctl restart kadima
if ! health; then echo "ROLLING BACK"; "$PY" "$ROOT/tools/install_loss_controls_20261025.py" --root "$ROOT" --rollback; sudo systemctl restart kadima; health; exit 1; fi
echo "DONE: LOSS CONTROLS v1 live"
