#!/usr/bin/env bash
# Run from the exact reviewed Git blob, passing that release's full commit SHA.
set -euo pipefail
umask 077
kadima_manual_release=${1:?A full reviewed release commit is required}
if [[ ! $kadima_manual_release =~ ^[0-9a-f]{40}$ ]]; then
  printf '%s\n' 'BLOCKED: expected a full 40-character release commit' >&2
  exit 1
fi
kadima_history=/home/ubuntu/kadima-py
kadima_evidence=/tmp/kadima-release.ykY99v/reviewed-bankex-95.json
/usr/bin/sudo -n /usr/bin/test -f "$kadima_evidence"
/usr/bin/sudo -n /usr/bin/test ! -L "$kadima_evidence"
kadima_stage=$(/usr/bin/mktemp -d /tmp/kadima-after-flat.XXXXXX)
printf 'Staging reviewed sources in %s\n' "$kadima_stage"
for kadima_pin in \
  "$kadima_manual_release" \
  e4277654951164e6b59fcdcec4670a44eafe0cf1 \
  bd294a7048749258fbaa2cc786f20a441a2a8073 \
  d584fe0eba27a8f23d2dfd2bac8fb68cb48501ce
do
  /usr/bin/git --no-replace-objects -C "$kadima_history" fetch --no-tags origin "$kadima_pin"
  [[ $(/usr/bin/git --no-replace-objects -C "$kadima_history" rev-parse --verify 'FETCH_HEAD^{commit}') == "$kadima_pin" ]]
done
/usr/bin/git --no-replace-objects -C "$kadima_history" archive --format=tar "$kadima_manual_release" > "$kadima_stage/release.tar"
/usr/bin/mkdir "$kadima_stage/release"
/usr/bin/tar --extract --file="$kadima_stage/release.tar" --directory="$kadima_stage/release" --no-same-owner
/usr/bin/sudo -n /usr/bin/python3 -I -B "$kadima_stage/release/momentum_recovery/deploy_after_flat.py" \
  --history "$kadima_history" --evidence "$kadima_evidence" --apply
