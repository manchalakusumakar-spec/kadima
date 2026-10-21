#!/bin/bash
# KADIMA trend rule 2 LIVE (gamma only with the day trend), 2026-10-21. Rules 3-4 and every
# other protection switch stay as they are. Run on EC2 outside market hours.
#   bash deploy_trend_rule2_live_20261021.sh             # backup, install, rule2=live, restart, health
#   bash deploy_trend_rule2_live_20261021.sh --shadow    # rule2 back to shadow (no restart needed)
#   bash deploy_trend_rule2_live_20261021.sh --rollback  # restore previous trend_rules.py, rule2 removed, restart
set -u
ROOT=${KADIMA_ROOT:-$HOME/kadima-py}
F=$ROOT/kadima/trend_rules.py
BASE=a33bb6dca54086144dee08209e6c7f8ac2ea539c48e42498ec0505060eb6fb94
NEW=8481cb3ab89bbabb4d75942cc4641a517fd6f7f404375b5e9c82ad8e459c3456
BAK=$F.bak-rule2-20261021
DATA=$(systemctl show kadima -p Environment --value | tr ' ' '\n' | sed -n 's/^KADIMA_DATA_DIR=//p'); DATA=${DATA:-/var/lib/kadima}
CFG=$DATA/trend_rules.json
h() { sha256sum "$1" | cut -c1-64; }
setmode() {
  sudo python3 - "$CFG" "$1" <<'PY'
import json, os, sys
path, value = sys.argv[1], sys.argv[2]
try:
    cfg = json.load(open(path)) if os.path.exists(path) else {}
except ValueError:
    cfg = {}
if value == 'remove':
    cfg.pop('rule2', None)
else:
    cfg['rule2'] = value
cfg.setdefault('mode', 'shadow')
st = os.stat(path) if os.path.exists(path) else None
open(path, 'w').write(json.dumps(cfg, indent=1))
if st: os.chown(path, st.st_uid, st.st_gid)
print('modes', json.dumps(cfg))
PY
}
health() {
  sleep 25
  systemctl is-active --quiet kadima || { echo "HEALTH FAIL: kadima not active"; return 1; }
  if journalctl -u kadima --since "-1 min" --no-pager | grep -q "Traceback"; then echo "HEALTH FAIL: traceback"; return 1; fi
  echo "health ok"; return 0
}
if [ "${1:-}" = "--shadow" ]; then setmode shadow; echo "rule2 shadow (re-read within 30 s)"; exit; fi
if [ "${1:-}" = "--rollback" ]; then
  [ "$(h $BAK)" = "$BASE" ] || { echo "ABORT: backup missing or wrong hash"; exit 1; }
  cp -p "$BAK" "$F" && setmode remove && sudo systemctl restart kadima && health; echo "live hash $(h $F | cut -c1-12) (base a33bb6dca540)"; exit
fi
echo "== 1. gates"
CUR=$(h $F); echo "live ${CUR:0:12} base ${BASE:0:12} release ${NEW:0:12}"
HM=$(TZ=Asia/Kolkata date +%H%M); DOW=$(TZ=Asia/Kolkata date +%u)
if [ "$DOW" -le 5 ] && [ "$HM" -ge 0900 ] && [ "$HM" -lt 1545 ]; then echo "ABORT: market hours (IST $HM); run after 15:45"; exit 1; fi
if [ "$CUR" != "$NEW" ]; then
  [ "$CUR" = "$BASE" ] || { echo "ABORT: live trend_rules.py is not the 19-Oct release; nothing changed"; exit 1; }
  echo "== 2. backup"; cp -p "$F" "$BAK"; echo "backup $(h $BAK | cut -c1-12) -> $BAK"
  echo "== 3. install"
  echo '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' | base64 -d | gunzip > "$F.new-rule2"
  [ "$(h $F.new-rule2)" = "$NEW" ] || { rm -f "$F.new-rule2"; echo "ABORT: payload hash mismatch"; exit 1; }
  $ROOT/venv/bin/python -m py_compile "$F.new-rule2" || { rm -f "$F.new-rule2"; echo "ABORT: does not compile"; exit 1; }
  mv "$F.new-rule2" "$F"
else
  echo "code already installed"
fi
echo "== 4. switch rule2 live"; setmode live
echo "== 5. restart"; sudo systemctl restart kadima
if ! health; then
  echo "ROLLING BACK"; cp -p "$BAK" "$F"; setmode remove; sudo systemctl restart kadima; health; exit 1
fi
echo "DONE: rule2 live, trend_rules ${NEW:0:12}"
