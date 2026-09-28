#!/usr/bin/env bash
# Verify the completed repair, then start the reviewed engine with entries paused.
set -euo pipefail
umask 077
kadima_start_commit=${1:?A full reviewed maintenance commit is required}
if [[ ! $kadima_start_commit =~ ^[0-9a-f]{40}$ ]]; then
  printf '%s\n' 'BLOCKED: expected a full maintenance commit' >&2
  exit 1
fi
kadima_history=/home/ubuntu/kadima-py
kadima_learning=8837cd6dc04e16123e94d8b78a3a716f83ed022c
/usr/bin/sudo -n /usr/bin/true
for kadima_pin in "$kadima_start_commit" "$kadima_learning"; do
  /usr/bin/git --no-replace-objects -C "$kadima_history" fetch --no-tags origin "$kadima_pin"
  [[ $(/usr/bin/git --no-replace-objects -C "$kadima_history" rev-parse --verify 'FETCH_HEAD^{commit}') == "$kadima_pin" ]]
done
kadima_start_stage=$(/usr/bin/mktemp -d /tmp/kadima-paused-start.XXXXXX)
/usr/bin/git --no-replace-objects -C "$kadima_history" archive --format=tar "$kadima_start_commit" > "$kadima_start_stage/release.tar"
/usr/bin/mkdir "$kadima_start_stage/release"
/usr/bin/tar --extract --file="$kadima_start_stage/release.tar" --directory="$kadima_start_stage/release" --no-same-owner
/usr/bin/sudo -n /usr/bin/python3 -I -B "$kadima_start_stage/release/momentum_recovery/start_repaired_paused.py" \
  --history "$kadima_history" \
  --evidence /tmp/kadima-release.ykY99v/reviewed-bankex-95.json \
  --apply
