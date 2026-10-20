#!/bin/bash
# usage: test_install_forecast_scoring_20261020.sh <repo>  -- install / re-run / rollback / refuse on mismatch
set -u; cd "$1"; FAIL=0; P=${PYTHON:-python3}; I=tools/install_forecast_scoring_20261020.py
BASE=c702310cfcc7f98a61af334a6b428f08215d770e4a314d82e79a3eb0dee1e870
T=$(mktemp -d); mkdir -p $T/kadima
git show origin/claude/trend-band-rules-20261019:kadima/hourly_forecast.py > $T/kadima/hourly_forecast.py
[ "$(sha256sum < $T/kadima/hourly_forecast.py | cut -c1-64)" = "$BASE" ] || { echo "FAIL base snapshot hash"; exit 1; }
$P $I $T --check | grep -q "state ready" || { echo "FAIL check ready"; FAIL=1; }
$P $I $T >/dev/null || { echo "FAIL install"; FAIL=1; }
cmp -s kadima/hourly_forecast.py $T/kadima/hourly_forecast.py && echo "installed = release" || { echo "FAIL content"; FAIL=1; }
$P $I $T | grep -q "already installed" && echo "idempotent" || { echo "FAIL rerun"; FAIL=1; }
$P $I $T --rollback >/dev/null; [ "$(sha256sum < $T/kadima/hourly_forecast.py | cut -c1-64)" = "$BASE" ] && echo "rollback ok" || { echo "FAIL rollback"; FAIL=1; }
echo "# drift" >> $T/kadima/hourly_forecast.py
$P $I $T >/dev/null 2>&1 && { echo "FAIL wrote over unknown file"; FAIL=1; } || echo "refuses unknown live file"
tail -c 8 $T/kadima/hourly_forecast.py | grep -q drift || { echo "FAIL unknown file modified"; FAIL=1; }
exit $FAIL
