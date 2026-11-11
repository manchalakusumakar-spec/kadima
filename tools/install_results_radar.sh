#!/bin/bash
# Install RESULTS RADAR v1 on KADIMA-BRIDGE (display only; no engine restart needed).
# Usage (as root): bash install_results_radar.sh <git-ref>
set -euo pipefail
REF="${1:-FETCH_HEAD}"
APP=/home/ubuntu/kadima-py
WEB=/var/www/html
STAMP=$(date +%Y%m%d-%H%M%S)
cd "$APP"
sudo -u ubuntu git show "$REF:tools/results_radar.py" > /tmp/rr_new.py
sudo -u ubuntu git show "$REF:tools/results_radar.js" > /tmp/rr_new.js
/usr/bin/python3 -m py_compile /tmp/rr_new.py
install -o ubuntu -g ubuntu -m 0755 /tmp/rr_new.py "$APP/tools/results_radar.py"
install -o ubuntu -g ubuntu -m 0644 /tmp/rr_new.js "$WEB/nb/results_radar.js"
mkdir -p /var/lib/kadima/results_radar && chown ubuntu:ubuntu /var/lib/kadima/results_radar
# one <script> line in dashboard.html (idempotent, with backup)
if ! grep -q '/nb/results_radar.js' "$WEB/dashboard.html"; then
  cp -p "$WEB/dashboard.html" "$WEB/dashboard.html.bak-resultsradar-$STAMP"
  /usr/bin/python3 - "$WEB/dashboard.html" << 'PY'
import sys
p = sys.argv[1]
s = open(p, encoding='utf-8').read()
tag = '<!-- RESULTS RADAR v1 (2026-11-11): tabs beside Gap-news signals, display only -->\n<script src="/nb/results_radar.js" defer></script>\n'
i = s.rfind('</body>')
s = s[:i] + tag + s[i:] if i >= 0 else s + tag
open(p + '.tmp', 'w', encoding='utf-8').write(s)
PY
  chown --reference="$WEB/dashboard.html" "$WEB/dashboard.html.tmp"; chmod --reference="$WEB/dashboard.html" "$WEB/dashboard.html.tmp"
  mv "$WEB/dashboard.html.tmp" "$WEB/dashboard.html"
fi
# cron (UTC): every 5 min 08:00-19:55 IST on weekdays
CMD='/usr/bin/flock -n /tmp/results_radar.lock /usr/bin/python3 /home/ubuntu/kadima-py/tools/results_radar.py >> /home/ubuntu/kadima-py/logs/results_radar.log 2>&1'
( crontab -u ubuntu -l 2>/dev/null | grep -v 'tools/results_radar.py' ; \
  echo "30-55/5 2 * * 1-5 $CMD # RESULTS RADAR v1"; \
  echo "*/5 3-14 * * 1-5 $CMD # RESULTS RADAR v1" ) | crontab -u ubuntu -
sudo -u ubuntu bash -c "$CMD"
tail -2 "$APP/logs/results_radar.log"
grep -c 'results_radar' "$WEB/dashboard.html"
crontab -u ubuntu -l | grep -c 'RESULTS RADAR'
