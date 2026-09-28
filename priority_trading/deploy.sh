#!/usr/bin/env bash
# Pinned additive deployment after the successful ordinary automation rollout.
set -euo pipefail
umask 077
kadima_priority_commit=${1:?Full reviewed commit required}
kadima_priority_cap=${2:?Explicit MCX capital ceiling required}
[[ $kadima_priority_commit =~ ^[0-9a-f]{40}$ ]] || exit 1
[[ $kadima_priority_cap =~ ^[0-9]+$ ]] || exit 1
(( kadima_priority_cap > 0 && kadima_priority_cap <= 15000 )) || exit 1
kadima_priority_history=/home/ubuntu/kadima-py
kadima_priority_reference=8837cd6dc04e16123e94d8b78a3a716f83ed022c
/usr/bin/sudo -n /usr/bin/true
for kadima_priority_pin in "$kadima_priority_commit" "$kadima_priority_reference"; do
  /usr/bin/git --no-replace-objects -C "$kadima_priority_history" fetch --no-tags origin "$kadima_priority_pin"
  [[ $(/usr/bin/git --no-replace-objects -C "$kadima_priority_history" rev-parse --verify 'FETCH_HEAD^{commit}') == "$kadima_priority_pin" ]]
done
kadima_priority_stage=$(/usr/bin/mktemp -d /tmp/kadima-priority.XXXXXX)
/usr/bin/git --no-replace-objects -C "$kadima_priority_history" archive --format=tar "$kadima_priority_commit" > "$kadima_priority_stage/release.tar"
/usr/bin/mkdir "$kadima_priority_stage/release"
/usr/bin/tar --extract --file="$kadima_priority_stage/release.tar" --directory="$kadima_priority_stage/release" --no-same-owner
/usr/bin/sudo -n /usr/bin/python3 -I -B "$kadima_priority_stage/release/priority_trading/normalize_source_modes.py" --apply
/usr/bin/sudo -n /usr/bin/python3 -I -B "$kadima_priority_stage/release/priority_trading/install.py" \
  --history "$kadima_priority_history" \
  --evidence /tmp/kadima-release.ykY99v/reviewed-bankex-95.json \
  --mcx-capital "$kadima_priority_cap" --apply
