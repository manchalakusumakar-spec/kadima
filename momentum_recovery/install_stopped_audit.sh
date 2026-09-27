#!/usr/bin/env bash
# Source-only PR20/PR24 maintenance; every KADIMA service remains stopped.
set -euo pipefail
umask 077
kadima_audit_release=${1:?A full reviewed maintenance commit is required}
if [[ ! $kadima_audit_release =~ ^[0-9a-f]{40}$ ]]; then
  printf '%s\n' 'BLOCKED: expected a full 40-character maintenance commit' >&2
  exit 1
fi
kadima_history=/home/ubuntu/kadima-py
kadima_learning=8837cd6dc04e16123e94d8b78a3a716f83ed022c
/usr/bin/sudo -n /usr/bin/true
for kadima_pin in "$kadima_audit_release" "$kadima_learning"; do
  /usr/bin/git --no-replace-objects -C "$kadima_history" fetch --no-tags origin "$kadima_pin"
  [[ $(/usr/bin/git --no-replace-objects -C "$kadima_history" rev-parse --verify 'FETCH_HEAD^{commit}') == "$kadima_pin" ]]
done
kadima_audit_stage=$(/usr/bin/mktemp -d /tmp/kadima-audit-bootstrap.XXXXXX)
/usr/bin/git --no-replace-objects -C "$kadima_history" archive --format=tar "$kadima_audit_release" > "$kadima_audit_stage/release.tar"
/usr/bin/mkdir "$kadima_audit_stage/release"
/usr/bin/tar --extract --file="$kadima_audit_stage/release.tar" --directory="$kadima_audit_stage/release" --no-same-owner
kadima_audit_result=0
/usr/bin/sudo -n /usr/bin/python3 -I -B "$kadima_audit_stage/release/momentum_recovery/install_stopped_audit.py" \
  --history "$kadima_history" --apply --manual-holding-external || kadima_audit_result=$?
if (( kadima_audit_result != 0 )); then
  kadima_audit_status=/var/lib/kadima-stopped-audit-20260928/status.json
  if /usr/bin/sudo -n /usr/bin/test -f "$kadima_audit_status" && /usr/bin/sudo -n /usr/bin/test ! -L "$kadima_audit_status"; then
    /usr/bin/sudo -n /usr/bin/cat "$kadima_audit_status"
  fi
fi
exit "$kadima_audit_result"
