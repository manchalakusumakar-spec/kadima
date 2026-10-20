#!/bin/bash
# usage: test_install_forecast_milestones_20261020.sh <repo>  -- install over both predecessors, re-run, rollback, refuse drift
set -u; cd "$1"; FAIL=0; P=${PYTHON:-python3}; I=tools/install_forecast_milestones_20261020.py
BASE=c702310cfcc7f98a61af334a6b428f08215d770e4a314d82e79a3eb0dee1e870
SCORING=f4fb1b07ce4026557ca5a121f6ab3d745e1a4c76c7e6f7cc5806450bae936cd9
for pair in "origin/claude/trend-band-rules-20261019:$BASE" "ef129a0c787e0f3686aa9f754b922186fd2d0b4a:$SCORING"; do
  REF=${pair%%:*}; WANT=${pair##*:}; T=$(mktemp -d); mkdir -p $T/kadima
  git show $REF:kadima/hourly_forecast.py > $T/kadima/hourly_forecast.py
  [ "$(sha256sum < $T/kadima/hourly_forecast.py | cut -c1-64)" = "$WANT" ] || { echo "FAIL predecessor snapshot hash $REF"; exit 1; }
  $P $I $T --check | grep -q "state ready" || { echo "FAIL check ready ($REF)"; FAIL=1; }
  $P $I $T >/dev/null || { echo "FAIL install ($REF)"; FAIL=1; }
  cmp -s kadima/hourly_forecast.py $T/kadima/hourly_forecast.py && echo "installed = release over ${WANT:0:12}" || { echo "FAIL content"; FAIL=1; }
  $P $I $T | grep -q "already installed" && echo "idempotent" || { echo "FAIL rerun"; FAIL=1; }
  $P $I $T --rollback >/dev/null; [ "$(sha256sum < $T/kadima/hourly_forecast.py | cut -c1-64)" = "$WANT" ] && echo "rollback ok" || { echo "FAIL rollback"; FAIL=1; }
  echo "# drift" >> $T/kadima/hourly_forecast.py
  $P $I $T >/dev/null 2>&1 && { echo "FAIL wrote over unknown file"; FAIL=1; } || echo "refuses unknown live file"
  tail -c 8 $T/kadima/hourly_forecast.py | grep -q drift || { echo "FAIL unknown file modified"; FAIL=1; }
done
exit $FAIL
