#!/bin/bash
# MARKET CONTEXT v1 (2026-10-25): light replacement for kadima-macro-watch (crude, VIX, FII/DII, news)
# and broker credentials moved out of the old unit file into a root-only EnvironmentFile.
#   bash deploy_market_context_20261025.sh             # install + start (outside market hours)
#   bash deploy_market_context_20261025.sh --rollback  # stop new service, restore old unit file
set -u
ROOT=${KADIMA_ROOT:-$HOME/kadima-py}; PY=$ROOT/venv/bin/python
SRC=$(cd "$(dirname "$0")/.." && pwd)
OLD=/etc/systemd/system/kadima-macro-watch.service; NEW=/etc/systemd/system/kadima-market-context.service
ENVF=/etc/kadima/market-context.env; BAK=/root/kadima-macro-watch.service.bak-20261025
SNAP=$ROOT/state/macro_snapshot.json
rollback() { sudo systemctl disable --now kadima-market-context 2>/dev/null; sudo rm -f $NEW
  sudo test -f $BAK && sudo cp -p $BAK $OLD; sudo systemctl daemon-reload; echo "rolled back (old macro service stays disabled)"; }
[ "${1:-}" = "--rollback" ] && { rollback; exit; }
HM=$(TZ=Asia/Kolkata date +%H%M); DOW=$(TZ=Asia/Kolkata date +%u)
if [ "$DOW" -le 5 ] && [ "$HM" -ge 0900 ] && [ "$HM" -lt 1545 ]; then echo "ABORT: market hours"; exit 1; fi
systemctl is-enabled --quiet kadima-macro-watch && { echo "ABORT: old macro service is enabled"; exit 1; }

# 1. credentials -> root-only file (values never printed)
if ! sudo test -f $ENVF; then
  sudo test -f $BAK || sudo install -m 600 -o root -g root $OLD $BAK
  sudo install -d -m 700 -o root -g root /etc/kadima
  sudo sed -n 's/^Environment="\(.*\)"$/\1/p' $BAK | sudo tee $ENVF >/dev/null
  sudo chmod 600 $ENVF; sudo chown root:root $ENVF
fi
echo "env file: $(sudo wc -l < $ENVF) variables, mode $(sudo stat -c %a $ENVF)"
sudo grep -q '^ANGEL_API_KEY=' $ENVF && sudo grep -q '^ANGEL_TOTP_SECRET=' $ENVF || { echo "ABORT: env file incomplete"; exit 1; }
sudo sed -i '/^Environment="/d' $OLD
sudo grep -q '^EnvironmentFile=' $OLD || sudo sed -i "s#^\[Service\]#[Service]\nEnvironmentFile=$ENVF#" $OLD
sudo grep -c '^Environment="' $OLD | sed 's/^/plain-text lines left in old unit: /'

# 2. module + self-test (no broker login)
install -m 644 "$SRC/kadima/market_context.py" "$ROOT/kadima/market_context.py"
"$PY" -m py_compile "$ROOT/kadima/market_context.py" || { echo "ABORT: compile"; rollback; exit 1; }
sudo systemd-run --quiet --wait --pipe --collect --uid=ubuntu --gid=ubuntu -p EnvironmentFile=$ENVF -p WorkingDirectory=$ROOT -p MemoryMax=128M "$PY" -m kadima.market_context --selftest || { echo "ABORT: selftest"; rollback; exit 1; }

# 3. service with a hard memory cap
sudo tee $NEW >/dev/null <<EOF
[Unit]
Description=KADIMA market context: crude, India VIX, FII/DII, news (display only)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=$ROOT
EnvironmentFile=$ENVF
ExecStart=$PY -m kadima.market_context
Restart=on-failure
RestartSec=30
Nice=10
MemoryHigh=96M
MemoryMax=128M
CPUQuota=15%
TasksMax=16
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now kadima-market-context
sleep 45
systemctl is-active kadima-market-context || { journalctl -u kadima-market-context -n 20 --no-pager; rollback; exit 1; }
journalctl -u kadima-market-context --since "-2 min" --no-pager | grep -q Traceback && { echo "TRACEBACK"; journalctl -u kadima-market-context -n 30 --no-pager | cut -c1-200; rollback; exit 1; }
systemctl show kadima-market-context -p MemoryCurrent -p MemoryPeak
"$PY" -c "import json;d=json.load(open('$SNAP'));c=d.get('commodities') or {};print('snapshot', d.get('ts'), d.get('source'), 'vix', d.get('india_vix'), 'fiidii', bool(d.get('fii_dii')), 'news', len(d.get('news') or []), 'crude', c.get('status'), len(c.get('contracts') or []))"
echo "DONE: MARKET CONTEXT v1"
