#!/usr/bin/env bash
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then
  echo 'Run with sudo bash live_inputs_update/install.sh' >&2
  exit 1
fi
kadima_update_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
test -f /var/www/html/dashboard.html
test -f /opt/kadima-dashboard/gateway.py
id ubuntu >/dev/null
# Complete compatibility checks before changing installed files or services.
PYTHONDONTWRITEBYTECODE=1 python3 "$kadima_update_dir/upgrade.py" --html /var/www/html/dashboard.html --gateway /opt/kadima-dashboard/gateway.py
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s "$kadima_update_dir" -p 'test_*.py'
install -d -m 0755 /opt/kadima-live-inputs
install -m 0644 "$kadima_update_dir/news_worker.py" /opt/kadima-live-inputs/news_worker.py
install -m 0644 "$kadima_update_dir/kadima-live-news.service" /etc/systemd/system/kadima-live-news.service
python3 "$kadima_update_dir/upgrade.py" --html /var/www/html/dashboard.html --gateway /opt/kadima-dashboard/gateway.py --apply
systemctl daemon-reload
systemctl enable --now kadima-live-news.service
systemctl restart kadima-dashboard.service
systemctl is-active kadima-live-news.service kadima-dashboard.service
echo 'Installed. Sign in to the dashboard again and verify source times and forecast availability.'
echo 'Trading services and execution settings were not changed.'
