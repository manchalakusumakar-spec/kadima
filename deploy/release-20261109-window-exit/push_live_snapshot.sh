#!/bin/bash
# Push a read-only copy of the LIVE KADIMA code on this EC2 box to GitHub
# (kadima-audit, new branch live-snapshot-<IST date-time>, files under live/).
# Changes nothing in ~/kadima-py, the engine or its data. Safe during market hours.
#   bash push_live_snapshot.sh
# What is copied: source/text files tracked or untracked in ~/kadima-py (py, html, js, css, md,
# yaml/yml, toml, txt, sh, service, timer), minus venv/.git/caches/data/logs/backups.
# Plus MANIFEST.txt: sha256 of every copied file, git HEAD, git status, kadima.service state,
# and the strategy switch files (scalp_v2.json etc.) from the data dir.
# It refuses to push if any copied file looks like it holds a credential.
set -euo pipefail
ROOT=${KADIMA_ROOT:-$HOME/kadima-py}
DATA=${KADIMA_DATA_DIR:-/var/lib/kadima}
AUDIT=${KADIMA_AUDIT_REPO:-$HOME/kadima-audit}
STAMP=$(TZ=Asia/Kolkata date +%Y%m%d-%H%M%S)
trap 'echo "FAILED at line $LINENO; nothing pushed"' ERR
BR=live-snapshot-$STAMP
[ -d "$ROOT/kadima" ] || { echo "ABORT: $ROOT/kadima not found"; exit 1; }
[ -d "$AUDIT/.git" ] || { echo "ABORT: $AUDIT is not the kadima-audit clone"; exit 1; }
WT=$(mktemp -d "$HOME/live-snapshot-XXXX")
trap 'git -C "$AUDIT" worktree remove --force "$WT" >/dev/null 2>&1 || true; rm -rf "$WT"' EXIT
git -C "$AUDIT" fetch -q origin main
git -C "$AUDIT" worktree add -q --detach "$WT" origin/main
git -C "$WT" checkout -q --orphan "$BR"
git -C "$WT" rm -rq --cached . >/dev/null 2>&1 || true
find "$WT" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} + || true
mkdir -p "$WT/live"
cd "$ROOT"
{ find . \( -path ./venv -o -path ./.git -o -name __pycache__ -o -path ./data -o -path ./logs \
          -o -path ./backups -o -name node_modules -o -name '*.bak*' -o -name '.env*' \) -prune -o \
     -type f \( -name '*.py' -o -name '*.html' -o -name '*.js' -o -name '*.css' -o -name '*.md' \
          -o -name '*.yaml' -o -name '*.yml' -o -name '*.toml' -o -name '*.txt' -o -name '*.sh' \
          -o -name '*.service' -o -name '*.timer' \) -size -3M -readable -print 2>/dev/null || true; } \
  | sed 's#^\./##' | sort > "$WT/live/FILES.txt"
SKIPPED=0
while read -r f; do mkdir -p "$WT/live/$(dirname "$f")"; cp -p "$f" "$WT/live/$f" 2>/dev/null || { echo "skipped (unreadable): $f"; SKIPPED=$((SKIPPED+1)); }; done < "$WT/live/FILES.txt"
[ -s "$WT/live/FILES.txt" ] || { echo "ABORT: no files found"; exit 1; }
# credential guard: values after these names, private keys, Angel/Telegram tokens
if grep -rIlE "(ANGEL_(API_KEY|CLIENT_ID|PASSWORD|TOTP_SECRET)|TELEGRAM_BOT_TOKEN|KADIMA_API_TOKEN|AWS_SECRET_ACCESS_KEY)[\"']?[[:space:]]*[=:][[:space:]]*[\"']?[A-Za-z0-9/+_-]{8,}|BEGIN [A-Z ]*PRIVATE KEY|[0-9]{9,10}:AA[A-Za-z0-9_-]{30,}" "$WT/live" > "$WT/flagged.txt"; then
  echo "ABORT: these files look like they contain a credential; nothing pushed:"; cat "$WT/flagged.txt"; exit 1
fi
{
  echo "KADIMA live snapshot $STAMP IST from $(hostname)"
  echo "== git HEAD"; git -C "$ROOT" rev-parse HEAD 2>&1 || true
  echo "== git status --short (uncommitted live changes)"; git -C "$ROOT" status --short 2>&1 | head -400 || true
  echo "== kadima.service"; systemctl show kadima -p ActiveState -p SubState -p ActiveEnterTimestamp 2>&1 || true
  echo "== strategy switch files in $DATA"
  for j in scalp_v2.json scalp_runner.json scalp_capital.json scalp_max_lots.json window_exit.json celestial.json \
           runner_hold.json capital_builder.json gamma_confirm.json gamma_priority.json sniper.json trend_rules.json best_entry.json move_capture.json stock_blast.json; do
    [ -f "$DATA/$j" ] && [ "$(stat -c %s "$DATA/$j")" -lt 4096 ] && echo "$j: $(tr -d '\n' < "$DATA/$j")"
    true
  done
  echo "== other top-level json names in $DATA (contents not copied)"; ls "$DATA"/*.json 2>/dev/null | xargs -n1 basename || true
  echo "== sha256 of every copied file"; (cd "$WT/live" && while read -r f; do [ -f "$f" ] && sha256sum "$f"; done < FILES.txt; true)
} > "$WT/live/MANIFEST.txt"
if grep -IqE "(ANGEL_|TELEGRAM_BOT_TOKEN|KADIMA_API_TOKEN|AWS_SECRET|token|secret|password|totp)" "$WT/live/MANIFEST.txt"; then
  sed -i -E '/^[a-z_]+\.json: .*(token|secret|password|totp)/Id' "$WT/live/MANIFEST.txt"; echo "note: a switch file mentioning a credential word was left out of MANIFEST"
fi
cd "$WT"
git add -A live
git -c user.name=KADIMA-EC2 -c user.email=kadima-ec2@noreply commit -qm "live snapshot $STAMP IST"
git push -q origin "$BR"
echo "PUSHED: kadima-audit branch $BR ($(wc -l < live/FILES.txt) files, $SKIPPED unreadable skipped)."
grep -E "kadima/(scalp_v2|auto_executor|window_exit|execution_ledger|storage|fast_momentum)\.py$" live/MANIFEST.txt || true
