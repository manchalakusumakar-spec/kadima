#!/usr/bin/env bash
# Operator-authorized source maintenance; leaves the KADIMA engine stopped.
set -euo pipefail
umask 077
kadima_stop_release=${1:?A full reviewed maintenance commit is required}
if [[ ! $kadima_stop_release =~ ^[0-9a-f]{40}$ ]]; then
  printf '%s\n' 'BLOCKED: expected a full 40-character maintenance commit' >&2
  exit 1
fi
kadima_history=/home/ubuntu/kadima-py
kadima_ui_archive=/tmp/kadima-ui-review-bo94rx77/dashboard-source.tar.gz
kadima_momentum=bd294a7048749258fbaa2cc786f20a441a2a8073
/usr/bin/sudo -n /usr/bin/true
for kadima_pin in "$kadima_stop_release" "$kadima_momentum"; do
  /usr/bin/git --no-replace-objects -C "$kadima_history" fetch --no-tags origin "$kadima_pin"
  [[ $(/usr/bin/git --no-replace-objects -C "$kadima_history" rev-parse --verify 'FETCH_HEAD^{commit}') == "$kadima_pin" ]]
done
kadima_stop_stage=$(/usr/bin/mktemp -d /tmp/kadima-stopped-bootstrap.XXXXXX)
/usr/bin/git --no-replace-objects -C "$kadima_history" archive --format=tar "$kadima_stop_release" > "$kadima_stop_stage/release.tar"
/usr/bin/mkdir "$kadima_stop_stage/release"
/usr/bin/tar --extract --file="$kadima_stop_stage/release.tar" --directory="$kadima_stop_stage/release" --no-same-owner
kadima_stop_result=0
/usr/bin/sudo -n /usr/bin/python3 -I -B "$kadima_stop_stage/release/momentum_recovery/install_stopped_source.py" \
  --history "$kadima_history" --apply --manual-holding-external || kadima_stop_result=$?
if (( kadima_stop_result != 0 )); then
  kadima_stop_status=/var/lib/kadima-stopped-source-20260928/status.json
  if /usr/bin/sudo -n /usr/bin/test -f "$kadima_stop_status" && /usr/bin/sudo -n /usr/bin/test ! -L "$kadima_stop_status"; then
    /usr/bin/sudo -n /usr/bin/cat "$kadima_stop_status"
  fi
fi
if [[ -r $kadima_ui_archive && ! -L $kadima_ui_archive ]]; then
  printf '\nDASHBOARD_ARCHIVE_BASE64_BEGIN\n'
  /usr/bin/base64 "$kadima_ui_archive"
  printf 'DASHBOARD_ARCHIVE_BASE64_END\n'
else
  printf '%s\n' 'The previously exported UI archive is unavailable. Report the maintenance status before further changes.'
fi
exit "$kadima_stop_result"
