#!/usr/bin/env bash
# Observe the stopped installation and complete broker books; no apply path.
set -euo pipefail
umask 077
kadima_readiness_commit=${1:?A full reviewed maintenance commit is required}
if [[ ! $kadima_readiness_commit =~ ^[0-9a-f]{40}$ ]]; then
  printf '%s\n' 'BLOCKED: expected a full maintenance commit' >&2
  exit 1
fi
kadima_history=/home/ubuntu/kadima-py
kadima_learning=8837cd6dc04e16123e94d8b78a3a716f83ed022c
/usr/bin/sudo -n /usr/bin/true
for kadima_pin in "$kadima_readiness_commit" "$kadima_learning"; do
  /usr/bin/git --no-replace-objects -C "$kadima_history" fetch --no-tags origin "$kadima_pin"
  [[ $(/usr/bin/git --no-replace-objects -C "$kadima_history" rev-parse --verify 'FETCH_HEAD^{commit}') == "$kadima_pin" ]]
done
kadima_readiness_stage=$(/usr/bin/mktemp -d /tmp/kadima-stopped-readiness.XXXXXX)
/usr/bin/git --no-replace-objects -C "$kadima_history" archive --format=tar "$kadima_readiness_commit" > "$kadima_readiness_stage/release.tar"
/usr/bin/mkdir "$kadima_readiness_stage/release"
/usr/bin/tar --extract --file="$kadima_readiness_stage/release.tar" --directory="$kadima_readiness_stage/release" --no-same-owner
/usr/bin/sudo -n /usr/bin/python3 -I -B "$kadima_readiness_stage/release/momentum_recovery/check_stopped_readiness.py" \
  --history "$kadima_history" \
  --evidence /tmp/kadima-release.ykY99v/reviewed-bankex-95.json \
  --broker-check
