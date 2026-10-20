#!/bin/bash
# usage: test_install_nifty_breadth_shadow_20261020.sh <repo>  -- install / re-run / rollback / refuse mismatch
set -u; cd "$1"; FAIL=0; P=${PYTHON:-python3}; I=tools/install_nifty_breadth_shadow_20261020.py
BASE=16b92751a7a1dfc024ac09fdeaa4fb7759943b587452e98912968e64022598e8
T=$(mktemp -d); mkdir -p $T/kadima
git show origin/claude/forecast-milestones-chart-20261020:kadima/hourly_forecast.py > $T/kadima/hourly_forecast.py
[ "$(sha256sum < $T/kadima/hourly_forecast.py | cut -c1-64)" = "$BASE" ] || { echo "FAIL base snapshot hash"; exit 1; }
$P $I $T --check | grep -q "state ready" || { echo "FAIL check ready"; FAIL=1; }
$P $I $T >/dev/null || { echo "FAIL install"; FAIL=1; }
cmp -s kadima/hourly_forecast.py $T/kadima/hourly_forecast.py && cmp -s kadima/nifty_breadth.py $T/kadima/nifty_breadth.py && echo "installed = release" || { echo "FAIL content"; FAIL=1; }
$P $I $T | grep -q "already installed" && echo "idempotent" || { echo "FAIL rerun"; FAIL=1; }
$P $I $T --rollback >/dev/null
[ "$(sha256sum < $T/kadima/hourly_forecast.py | cut -c1-64)" = "$BASE" ] && [ ! -e $T/kadima/nifty_breadth.py ] && echo "rollback ok" || { echo "FAIL rollback"; FAIL=1; }
echo "# drift" >> $T/kadima/hourly_forecast.py
$P $I $T >/dev/null 2>&1 && { echo "FAIL wrote over unknown file"; FAIL=1; } || echo "refuses unknown live file"
exit $FAIL
