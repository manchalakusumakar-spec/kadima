#!/bin/bash
# KADIMA Sniper mode, 2026-10-24 (trader decisions 01:33 IST): live at 1 lot.
#   Gamma Blast auto entries only 09:45-10:30, with the day trend (no sideways days), never the
#   first signal of an index/side, no chase >15%, side done after a stop, max 2 a day (2nd after
#   a loss on another index/side), 1 lot, skip if stop risk >15% of cash, no-progress exit 20 min.
#   Sniper trades are exempt from the 30-min exhaustion (R3) and band-half pullback waits.
# Run on EC2 OUTSIDE market hours:
#   bash deploy_sniper_20261024.sh              # fetch branch, verify, install, mode=live, restart, health
#   bash deploy_sniper_20261024.sh --shadow     # mode=shadow (log only, within 30 s, no restart)
#   bash deploy_sniper_20261024.sh --off        # mode=off (old gamma flow, within 30 s, no restart)
#   bash deploy_sniper_20261024.sh --rollback   # restore auto_executor/celestial, remove sniper.py, restart
set -u
ROOT=${KADIMA_ROOT:-$HOME/kadima-py}
BRANCH=claude/sniper-mode-20261024
REPO=git@github.com:manchalakusumakar-spec/kadima.git
KEY=${KADIMA_DEPLOY_KEY:-$HOME/.ssh/kadima_deploy}
H_SNIPER=ebf8cb0a0338d8b569b85a8f724c66046b7d30b831f342f5db1414432ca23eff
H_INSTALL=ca1716a9ee4274ec3e1532a77123c2c98edb46f0166d2ff5b16015ed8d6d0bf3
DATA=$(systemctl show kadima -p Environment --value | tr ' ' '\n' | sed -n 's/^KADIMA_DATA_DIR=//p'); DATA=${DATA:-/var/lib/kadima}
CFG=$DATA/sniper.json
PY=$ROOT/venv/bin/python; [ -x "$PY" ] || PY=python3
h() { sha256sum "$1" 2>/dev/null | cut -c1-64; }
setmode() {
  sudo python3 - "$CFG" "$1" "$DATA/kadima.db" <<'PY'
import json, os, sys
path, value, ref = sys.argv[1:4]
try:
    cfg = json.load(open(path)) if os.path.exists(path) else {}
except ValueError:
    cfg = {}
cfg['mode'] = value
open(path, 'w').write(json.dumps(cfg, indent=1))
st = os.stat(ref) if os.path.exists(ref) else None
if st: os.chown(path, st.st_uid, st.st_gid)
print('sniper.json', json.dumps(cfg))
PY
}
health() {
  sleep 25
  systemctl is-active --quiet kadima || { echo "HEALTH FAIL: kadima not active"; return 1; }
  if journalctl -u kadima --since "-1 min" --no-pager | grep -q "Traceback"; then echo "HEALTH FAIL: traceback"; return 1; fi
  echo "health ok"; return 0
}
case "${1:-}" in
  --shadow) setmode shadow; echo "Sniper SHADOW (log only) within 30 s"; exit;;
  --off)    setmode off;    echo "Sniper OFF within 30 s"; exit;;
  --rollback)
    setmode off
    [ -f "$ROOT/tools/install_sniper_20261024.py" ] && "$PY" "$ROOT/tools/install_sniper_20261024.py" --root "$ROOT" --rollback
    sudo systemctl restart kadima && health; exit;;
esac
echo "== 1. gates"
HM=$(TZ=Asia/Kolkata date +%H%M); DOW=$(TZ=Asia/Kolkata date +%u)
if [ "$DOW" -le 5 ] && [ "$HM" -ge 0900 ] && [ "$HM" -lt 1545 ]; then echo "ABORT: market hours (IST $HM); run after 15:45 or before 09:00"; exit 1; fi
echo "== 2. fetch + verify"
SRC=$(mktemp -d)
GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" \
  git clone -q --depth 1 -b "$BRANCH" "$REPO" "$SRC" || { echo "ABORT: git fetch failed"; exit 1; }
[ "$(h $SRC/kadima/sniper.py)" = "$H_SNIPER" ] || { echo "ABORT: sniper.py hash mismatch"; exit 1; }
[ "$(h $SRC/tools/install_sniper_20261024.py)" = "$H_INSTALL" ] || { echo "ABORT: installer hash mismatch"; exit 1; }
"$PY" "$SRC/tools/install_sniper_20261024.py" --root "$ROOT" --check || { echo "ABORT: live files do not match anchors; nothing changed"; exit 1; }
echo "== 3. install"
mkdir -p "$ROOT/tools" && cp -p "$SRC/tools/install_sniper_20261024.py" "$ROOT/tools/"
"$PY" "$SRC/tools/install_sniper_20261024.py" --root "$ROOT" || { echo "ABORT: install failed"; exit 1; }
sudo chown --reference="$ROOT/kadima/auto_executor.py" "$ROOT/kadima/sniper.py"
"$PY" -m py_compile "$ROOT/kadima/sniper.py" "$ROOT/kadima/auto_executor.py" "$ROOT/kadima/celestial.py" || {
  echo "ROLLING BACK: compile"; "$PY" "$ROOT/tools/install_sniper_20261024.py" --root "$ROOT" --rollback; exit 1; }
echo "== 4. mode live"; setmode live
echo "== 5. restart"; sudo systemctl restart kadima
if ! health; then
  echo "ROLLING BACK"; setmode off; "$PY" "$ROOT/tools/install_sniper_20261024.py" --root "$ROOT" --rollback
  sudo systemctl restart kadima; health; exit 1
fi
rm -rf "$SRC"
echo "DONE: Sniper LIVE (1 lot). Watch: grep '\"event\":\"sniper_' $DATA/audit/\$(date +%F).jsonl"
