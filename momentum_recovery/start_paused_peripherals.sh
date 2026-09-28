#!/usr/bin/env bash
# Start reviewed observer services beside the already-verified paused engine.
set -euo pipefail
umask 077
kadima_peripheral_commit=${1:?A full reviewed maintenance commit is required}
if [[ ! $kadima_peripheral_commit =~ ^[0-9a-f]{40}$ ]]; then
  printf '%s\n' 'BLOCKED: expected a full maintenance commit' >&2
  exit 1
fi
kadima_history=/home/ubuntu/kadima-py
kadima_learning=8837cd6dc04e16123e94d8b78a3a716f83ed022c
/usr/bin/sudo -n /usr/bin/true
for kadima_pin in "$kadima_peripheral_commit" "$kadima_learning"; do
  /usr/bin/git --no-replace-objects -C "$kadima_history" fetch --no-tags origin "$kadima_pin"
  [[ $(/usr/bin/git --no-replace-objects -C "$kadima_history" rev-parse --verify 'FETCH_HEAD^{commit}') == "$kadima_pin" ]]
done
kadima_peripheral_stage=$(/usr/bin/mktemp -d /tmp/kadima-paused-peripherals.XXXXXX)
/usr/bin/git --no-replace-objects -C "$kadima_history" archive --format=tar "$kadima_peripheral_commit" > "$kadima_peripheral_stage/release.tar"
/usr/bin/mkdir "$kadima_peripheral_stage/release"
/usr/bin/tar --extract --file="$kadima_peripheral_stage/release.tar" --directory="$kadima_peripheral_stage/release" --no-same-owner
/usr/bin/sudo -n /usr/bin/python3 -I -B "$kadima_peripheral_stage/release/momentum_recovery/start_paused_peripherals.py" \
  --history "$kadima_history" \
  --evidence /tmp/kadima-release.ykY99v/reviewed-bankex-95.json \
  --reviewed-session-restart \
  --apply
