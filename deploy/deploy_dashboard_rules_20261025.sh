#!/bin/bash
# DASHBOARD RULES v1 (2026-10-25): rules strip + CONFIRMED highlight in Celestial. Run as root (sudo) outside market hours.
#   sudo bash deploy_dashboard_rules_20261025.sh            # install, restart engine (API thread), health, auto-rollback
#   sudo bash deploy_dashboard_rules_20261025.sh --rollback
set -u
ROOT=/home/ubuntu/kadima-py; HTML=/var/www/html/dashboard.html; PY=$ROOT/venv/bin/python; SRC=$(cd "$(dirname "$0")/.." && pwd)
health() { sleep 25; systemctl is-active --quiet kadima || { echo "HEALTH FAIL"; return 1; }
  journalctl -u kadima --since "-1 min" --no-pager | grep -q Traceback && { echo "HEALTH FAIL: traceback"; return 1; }
  curl -sf --max-time 5 localhost:8081/health >/dev/null || { echo "HEALTH FAIL: control api"; return 1; }; echo "health ok"; }
if [ "${1:-}" = "--rollback" ]; then "$PY" "$SRC/tools/install_dashboard_rules_20261025.py" --root "$ROOT" --html "$HTML" --rollback; systemctl restart kadima; health; exit; fi
HM=$(TZ=Asia/Kolkata date +%H%M); DOW=$(TZ=Asia/Kolkata date +%u)
if [ "$DOW" -le 5 ] && [ "$HM" -ge 0900 ] && [ "$HM" -lt 1545 ]; then echo "ABORT: market hours"; exit 1; fi
"$PY" "$SRC/tools/install_dashboard_rules_20261025.py" --root "$ROOT" --html "$HTML" --check || { echo "ABORT: anchors; nothing changed"; exit 1; }
"$PY" "$SRC/tools/install_dashboard_rules_20261025.py" --root "$ROOT" --html "$HTML" || { echo "ABORT: install failed"; exit 1; }
chown ubuntu:ubuntu "$ROOT/kadima/control_api.py"
"$PY" -m py_compile "$ROOT/kadima/control_api.py" || { echo "ROLLING BACK: compile"; "$PY" "$SRC/tools/install_dashboard_rules_20261025.py" --root "$ROOT" --html "$HTML" --rollback; exit 1; }
systemctl restart kadima
if ! health; then echo "ROLLING BACK"; "$PY" "$SRC/tools/install_dashboard_rules_20261025.py" --root "$ROOT" --html "$HTML" --rollback; systemctl restart kadima; health; exit 1; fi
echo "DONE: DASHBOARD RULES v1 live (Ctrl+F5 the dashboard)"
