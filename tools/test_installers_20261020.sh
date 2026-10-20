#!/bin/bash
# usage: test_installers_20261020.sh <repo> <scratch> -- 19-Oct installers, then scalp v2, on live snapshots
set -u; cd "$1"; SP="$2"; FAIL=0
FILES="trade_policy celestial celestial_exit auto_executor celestial_adoption engine market_session_gate cas_executor trade_audit fast_momentum"
for B in origin/codex/ec2-release-source-20261014 origin/live/2026-10-11; do
  T=$(mktemp -d -p "$SP"); mkdir -p $T/kadima $T/data $T/ext; export KADIMA_DATA_DIR=$T/data
  for f in $FILES; do git show $B:kadima/$f.py > $T/kadima/$f.py; done
  git show origin/claude/forecast-v21:server/opt/kadima-session-auto/runner.py > $T/ext/runner.py
  git show origin/codex/trade-audit-dashboard:dashboard_live/dashboard.html > $T/ext/dashboard.html
  cp $T/ext/runner.py $T/ext/runner.orig; cp $T/ext/dashboard.html $T/ext/dashboard.orig
  echo "=== $B"
  for i in exit_fix trend_rules protection; do python3 tools/install_${i}_20261019.py $T > $T/log_$i 2>&1 || { echo "FAIL prereq $i: $(tail -2 $T/log_$i)"; FAIL=1; continue 2; }; done
  for f in $FILES; do cp $T/kadima/$f.py $T/kadima/$f.pre; done
  EXT="--session-runner $T/ext/runner.py --dashboard $T/ext/dashboard.html"
  python3 tools/install_scalp_v2_20261020.py $T $EXT > $T/log_v2 2>&1 || { echo "FAIL install: $(tail -3 $T/log_v2)"; FAIL=1; continue; }
  cp -r $T/kadima $T/once; cp $T/ext/runner.py $T/runner.once
  python3 tools/install_scalp_v2_20261020.py $T $EXT > $T/log_v2b 2>&1 || { echo "FAIL rerun: $(tail -2 $T/log_v2b)"; FAIL=1; }
  diff -rq -x '*.bak-*' -x __pycache__ $T/once $T/kadima >/dev/null && cmp -s $T/runner.once $T/ext/runner.py && echo "idempotent" || { echo "FAIL not idempotent"; FAIL=1; }
  for f in $T/kadima/*.py $T/ext/runner.py; do python3 -m py_compile $f || { echo "FAIL compile $f"; FAIL=1; }; done
  grep -q "scalp_v2.merge(st, self.fast_lane.scan(st), self.fast_lane)" $T/kadima/engine.py && grep -q "scalp_v2.on_tick(name" $T/kadima/engine.py \
   && grep -q "scalp_v2.wait(OI_POLL_SEC" $T/kadima/engine.py && [ "$(grep -c 'underlying=index_name' $T/kadima/auto_executor.py)" = 4 ] \
   && grep -q "THRESHOLDS = (60," $T/kadima/celestial.py && grep -q "'Blocked by'" $T/kadima/trade_audit.py \
   && grep -q "index=gate.calendar_reason" $T/ext/runner.py && grep -q "<option>60</option>" $T/ext/dashboard.html \
   && grep -q "trend_rules.managed_stop" $T/kadima/auto_executor.py && echo "hooks present" || { echo "FAIL hooks missing"; FAIL=1; }
  python3 tools/install_scalp_v2_20261020.py $T --live | grep modes
  python3 tools/install_scalp_v2_20261020.py $T --rollback $EXT >/dev/null
  for f in $FILES; do cmp -s $T/kadima/$f.pre $T/kadima/$f.py || { echo "FAIL rollback $f"; FAIL=1; }; done
  cmp -s $T/ext/runner.orig $T/ext/runner.py && cmp -s $T/ext/dashboard.orig $T/ext/dashboard.html || { echo "FAIL rollback external"; FAIL=1; }
  [ -e $T/kadima/scalp_v2.py ] && { echo "FAIL new file left"; FAIL=1; }
  echo "rollback ok"
done
exit $FAIL
