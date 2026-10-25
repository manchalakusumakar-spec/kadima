#!/bin/bash
# GIFT NIFTY v1 (2026-10-25). Display-only GIFT Nifty beside crude oil. Run outside market hours.
#   bash deploy_gift_nifty_20261025.sh             # install, start worker, restart engine, health
#   bash deploy_gift_nifty_20261025.sh --rollback  # stop worker, restore files, restart engine
set -u
ROOT=${KADIMA_ROOT:-$HOME/kadima-py}; PY=$ROOT/venv/bin/python; HTML=/var/www/html/dashboard.html
SRC=$(cd "$(dirname "$0")/.." && pwd); T=$SRC/tools/install_gift_nifty_20261025.py
UNIT=/etc/systemd/system/kadima-gift-nifty.service; OUT=$ROOT/state/gift_nifty.json
health() { sleep 25; systemctl is-active --quiet kadima || { echo "HEALTH FAIL: not active"; return 1; }
  journalctl -u kadima --since "-1 min" --no-pager | grep -q Traceback && { echo "HEALTH FAIL: traceback"; return 1; }; echo "health ok"; }
rollback() { sudo systemctl disable --now kadima-gift-nifty 2>/dev/null; sudo rm -f $UNIT; sudo systemctl daemon-reload
  sudo "$PY" "$T" --root "$ROOT" --html "$HTML" --rollback; sudo systemctl restart kadima; health; }
[ "${1:-}" = "--rollback" ] && { rollback; exit; }
HM=$(TZ=Asia/Kolkata date +%H%M); DOW=$(TZ=Asia/Kolkata date +%u)
if [ "$DOW" -le 5 ] && [ "$HM" -ge 0900 ] && [ "$HM" -lt 1545 ]; then echo "ABORT: market hours"; exit 1; fi
sudo "$PY" "$T" --root "$ROOT" --html "$HTML" --check || { echo "ABORT: checks; nothing changed"; exit 1; }
cp -p "$T" "$ROOT/tools/"
sudo "$PY" "$T" --root "$ROOT" --html "$HTML" || { echo "ABORT: install failed"; exit 1; }
sudo chown --reference="$ROOT/kadima/control_api.py" "$ROOT/kadima/gift_nifty_worker.py"
"$PY" -m py_compile "$ROOT/kadima/control_api.py" "$ROOT/kadima/gift_nifty_worker.py" || { echo "ROLLING BACK: compile"; rollback; exit 1; }
sudo tee $UNIT >/dev/null <<EOF
[Unit]
Description=KADIMA GIFT Nifty market context (display only)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=$ROOT
ExecStart=/usr/bin/python3 $ROOT/kadima/gift_nifty_worker.py
Restart=on-failure
RestartSec=30
NoNewPrivileges=yes
PrivateTmp=yes
MemoryMax=64M
CPUQuota=5%
TasksMax=8

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now kadima-gift-nifty
sudo systemctl restart kadima
if ! health; then echo "ROLLING BACK"; rollback; exit 1; fi
for i in 1 2 3 4 5 6; do [ -s "$OUT" ] && break; sleep 5; done
systemctl is-active kadima-gift-nifty && cat "$OUT"; echo
echo "DONE: GIFT NIFTY v1"
