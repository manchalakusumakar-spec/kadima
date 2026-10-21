#!/bin/bash
# KADIMA Step 0 (trader-approved 22-Oct 02:01): capital_guard fixes.
#  1. Shared cached Angel One balance read: fresh 5 s, last-good fallback up to 60 s on a failed
#     read (minus cash reserved since), one retry after 1 s when no cache; ends the rate-limit
#     "Access denied because of exceeding access rate" refusals.
#  2. Headroom = buffered available cash (already net of open premium); open positions are no
#     longer subtracted a second time.
# The new file is taken from the git branch the live checkout has fetched and verified by hash.
#   bash $0             # gates, backup, install, restart, health (auto-rollback)
#   bash $0 --rollback  # restore previous capital_guard.py, restart
set -u
ROOT=${KADIMA_ROOT:-$HOME/kadima-py}
F=$ROOT/kadima/capital_guard.py
BASE=5141f361513b668a0382485537be5d2108127a6c9006e5bdf58fcd82d2988dab
NEW=a6b23bcf3ed2bfbe850110ec2f56f685e70e0083e8172172abebed174c4bee56
BAK=$F.bak-step0-20261022
REF=${KADIMA_STEP0_REF:-origin/claude/gamma-red-reduction-20261022}
h() { sha256sum "$1" 2>/dev/null | cut -c1-64; }
health() {
  sleep 25
  systemctl is-active --quiet kadima || { echo "HEALTH FAIL: kadima not active"; return 1; }
  if journalctl -u kadima --since "-1 min" --no-pager | grep -q "Traceback"; then echo "HEALTH FAIL: traceback"; return 1; fi
  echo "health ok"; return 0
}
if [ "${1:-}" = "--rollback" ]; then
  [ "$(h $BAK)" = "$BASE" ] || { echo "ABORT: backup missing or wrong hash"; exit 1; }
  cp -p "$BAK" "$F" && sudo systemctl restart kadima && health; echo "live $(h $F | cut -c1-12)"; exit
fi
echo "== 1. gates"
HM=$(TZ=Asia/Kolkata date +%H%M); DOW=$(TZ=Asia/Kolkata date +%u)
if [ "$DOW" -le 5 ] && [ "$HM" -ge 0900 ] && [ "$HM" -lt 1545 ]; then echo "ABORT: market hours (IST $HM)"; exit 1; fi
CUR=$(h $F)
if [ "$CUR" = "$NEW" ]; then echo "already installed"; exit 0; fi
[ "$CUR" = "$BASE" ] || { echo "ABORT: live capital_guard.py ${CUR:0:12} is not the expected base; nothing changed"; exit 1; }
echo "== 2. stage"
git -c safe.directory=$ROOT -C "$ROOT" show "$REF:kadima/capital_guard.py" > "$F.new-step0" || { echo "ABORT: cannot read $REF"; exit 1; }
[ "$(h $F.new-step0)" = "$NEW" ] || { rm -f "$F.new-step0"; echo "ABORT: staged hash mismatch"; exit 1; }
"$ROOT/venv/bin/python" -m py_compile "$F.new-step0" || { rm -f "$F.new-step0"; echo "ABORT: does not compile"; exit 1; }
echo "== 3. install"; cp -p "$F" "$BAK"; mv "$F.new-step0" "$F"; echo "installed ${NEW:0:12}, backup $BAK"
echo "== 4. restart"; sudo systemctl restart kadima
if ! health; then echo "ROLLING BACK"; cp -p "$BAK" "$F"; sudo systemctl restart kadima; health; exit 1; fi
echo "DONE: Step 0 capital_guard ${NEW:0:12}"
