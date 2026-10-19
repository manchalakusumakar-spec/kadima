#!/bin/bash
# usage: variant_test.sh <repo> <scratch>  -- installs exit->trend->protection on each live snapshot
set -u; cd "$1"; SP="$2"; FAIL=0
FILES="trade_policy celestial celestial_exit auto_executor celestial_adoption"
for B in origin/codex/ec2-release-source-20261014 origin/codex/verify-sebi-cas-circular-before-cutoff-change origin/live/2026-10-11; do
  T=$(mktemp -d -p "$SP"); mkdir -p $T/kadima $T/data; export KADIMA_DATA_DIR=$T/data
  for f in $FILES; do git show $B:kadima/$f.py > $T/kadima/$f.py; done
  echo "=== $B"
  for i in exit_fix trend_rules protection; do python3 tools/install_${i}_20261019.py $T > $T/log_$i 2>&1 || { echo "FAIL install $i: $(tail -2 $T/log_$i)"; FAIL=1; continue 2; }; done
  cp -r $T/kadima $T/once
  for i in exit_fix trend_rules protection; do python3 tools/install_${i}_20261019.py $T > $T/log2_$i 2>&1 || { echo "FAIL rerun $i: $(tail -2 $T/log2_$i)"; FAIL=1; }; done
  diff -rq -x '*.bak-*' -x __pycache__ $T/once $T/kadima >/dev/null && echo "idempotent" || { echo "FAIL not idempotent"; FAIL=1; }
  for f in $T/kadima/*.py; do python3 -m py_compile $f || { echo "FAIL compile $f"; FAIL=1; }; done
  grep -q "trend_rules.managed_stop" $T/kadima/auto_executor.py && grep -q "protection.watch_rejections" $T/kadima/auto_executor.py && grep -q "entry_basis(row)" $T/kadima/celestial_adoption.py && echo "hooks present" || { echo "FAIL hooks missing"; FAIL=1; }
  python3 tools/install_protection_20261019.py $T --live | grep modes
  for i in protection trend_rules exit_fix; do python3 tools/install_${i}_20261019.py $T --rollback >/dev/null; done
  for f in $FILES; do git show $B:kadima/$f.py | cmp -s - $T/kadima/$f.py || { echo "FAIL rollback $f"; FAIL=1; }; done
  [ -e $T/kadima/protection.py ] || [ -e $T/kadima/trend_rules.py ] && { echo "FAIL new files left"; FAIL=1; }
  echo "rollback ok"
done
exit $FAIL
