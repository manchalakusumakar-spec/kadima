#!/bin/bash
# KADIMA: disable index scalping (trader decision 2026-10-22 01:50 IST). Gamma, CAS, move
# capture, gap-and-go, stock fast and MCX are untouched (they do not read this switch).
# Adds a scalp_v2.json switch "entries": off|shadow|live. Anything but "live" adds the blocker
# `scalp_entries_off` to every scalp v2 row, so it is never eligible and the executor refuses it
# (auto_executor requires fast.get('eligible')). scalp_v2 stays mode=live, so v1 rows are still
# not dispatched, and scalp signals are still computed and written to the Trade Audit.
#   bash disable_scalping_20261022.sh            # patch (hash-guarded), entries=off, restart, health
#   bash disable_scalping_20261022.sh --enable   # entries=live (re-read within 30 s, no restart)
#   bash disable_scalping_20261022.sh --disable  # entries=off  (re-read within 30 s, no restart)
#   bash disable_scalping_20261022.sh --rollback # restore original scalp_v2.py, drop switch, restart
set -u
ROOT=${KADIMA_ROOT:-$HOME/kadima-py}
F=$ROOT/kadima/scalp_v2.py
BASE=bc0ed64719f9511c64c1bd434d92bae8272fc8026a37ffcc162dd5f8d4251b09
BAK=$F.bak-scalp-off-20261022
DATA=$(systemctl show kadima -p Environment --value | tr ' ' '\n' | sed -n 's/^KADIMA_DATA_DIR=//p'); DATA=${DATA:-/var/lib/kadima}
CFG=$DATA/scalp_v2.json
h() { sha256sum "$1" 2>/dev/null | cut -c1-64; }
entries() {
  sudo python3 - "$CFG" "$1" <<'PY'
import json, os, sys
path, value = sys.argv[1], sys.argv[2]
cfg = json.load(open(path)) if os.path.exists(path) else {}
if value == 'remove':
    cfg.pop('entries', None)
else:
    cfg['entries'] = value
st = os.stat(path) if os.path.exists(path) else None
open(path, 'w').write(json.dumps(cfg))
if st: os.chown(path, st.st_uid, st.st_gid)
print('scalp_v2.json', json.dumps(cfg))
PY
}
health() {
  sleep 25
  systemctl is-active --quiet kadima || { echo "HEALTH FAIL: kadima not active"; return 1; }
  if journalctl -u kadima --since "-1 min" --no-pager | grep -q "Traceback"; then echo "HEALTH FAIL: traceback"; return 1; fi
  echo "health ok"; return 0
}
case "${1:-}" in
  --enable)  entries live; echo "scalping entries ON (within 30 s)"; exit;;
  --disable) entries off;  echo "scalping entries OFF (within 30 s)"; exit;;
  --rollback)
    [ "$(h $BAK)" = "$BASE" ] || { echo "ABORT: backup missing or wrong hash"; exit 1; }
    cp -p "$BAK" "$F" && entries remove && sudo systemctl restart kadima && health; exit;;
esac
echo "== 1. gates"
HM=$(TZ=Asia/Kolkata date +%H%M); DOW=$(TZ=Asia/Kolkata date +%u)
if [ "$DOW" -le 5 ] && [ "$HM" -ge 0900 ] && [ "$HM" -lt 1545 ]; then echo "ABORT: market hours (IST $HM)"; exit 1; fi
CUR=$(h $F)
if grep -q "scalp_entries_off" "$F"; then
  echo "switch already installed (${CUR:0:12})"
else
  [ "$CUR" = "$BASE" ] || { echo "ABORT: live scalp_v2.py ${CUR:0:12} is not the expected base; nothing changed"; exit 1; }
  echo "== 2. patch"
  cp -p "$F" "$BAK"
  python3 - "$F" <<'PY' || { cp -p "$BAK" "$F"; echo "ABORT: patch failed, restored"; exit 1; }
import sys
p = sys.argv[1]; s = open(p).read()
a = "DEFAULTS = {'mode': 'shadow', 'priority_poll': 'shadow', 'partial': 'shadow', 'thesis_exits': 'shadow'}"
b = ("    if mode() != 'live':\n"
     "        row['blocked_reasons'].append('scalp_v2_shadow')\n")
assert s.count(a) == 1 and s.count(b) == 1, (s.count(a), s.count(b))
s = s.replace(a, a[:-1] + ", 'entries': 'live'}  # entries: trader switch 2026-10-22")
s = s.replace(b, b + "    if config()['entries'] != 'live':   # trader 2026-10-22: scalping disabled\n"
                     "        row['blocked_reasons'].append('scalp_entries_off')\n")
open(p, 'w').write(s)
print('patched')
PY
  "$ROOT/venv/bin/python" -m py_compile "$F" || { cp -p "$BAK" "$F"; echo "ABORT: does not compile, restored"; exit 1; }
  echo "patched $(h $F | cut -c1-12), backup $BAK"
fi
echo "== 3. entries off"; entries off
echo "== 4. restart"; sudo systemctl restart kadima
if ! health; then echo "ROLLING BACK"; cp -p "$BAK" "$F"; entries remove; sudo systemctl restart kadima; health; exit 1; fi
grep -n -e "'entries'" -e scalp_entries_off "$F"
echo "DONE: index scalping entries OFF; signals still audited"
