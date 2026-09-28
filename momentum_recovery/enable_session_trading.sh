#!/usr/bin/env bash
# Restore scheduled NSE/BSE trading after the reviewed paused rollout.
set -euo pipefail
umask 077
kadima_activation_commit=${1:?A full reviewed maintenance commit is required}
[[ $kadima_activation_commit =~ ^[0-9a-f]{40}$ ]] || exit 1
kadima_history=/home/ubuntu/kadima-py
kadima_learning=8837cd6dc04e16123e94d8b78a3a716f83ed022c
/usr/bin/sudo -n /usr/bin/true
for kadima_pin in "$kadima_activation_commit" "$kadima_learning"; do
  /usr/bin/git --no-replace-objects -C "$kadima_history" fetch --no-tags origin "$kadima_pin"
  [[ $(/usr/bin/git --no-replace-objects -C "$kadima_history" rev-parse --verify 'FETCH_HEAD^{commit}') == "$kadima_pin" ]]
done
kadima_activation_stage=$(/usr/bin/mktemp -d /tmp/kadima-auto-enable.XXXXXX)
/usr/bin/git --no-replace-objects -C "$kadima_history" archive --format=tar "$kadima_activation_commit" > "$kadima_activation_stage/release.tar"
/usr/bin/mkdir "$kadima_activation_stage/release"
/usr/bin/tar --extract --file="$kadima_activation_stage/release.tar" --directory="$kadima_activation_stage/release" --no-same-owner
/usr/bin/sudo -n /usr/bin/python3 -I -B "$kadima_activation_stage/release/momentum_recovery/enable_session_trading.py" \
  --history "$kadima_history" \
  --evidence /tmp/kadima-release.ykY99v/reviewed-bankex-95.json \
  --apply
