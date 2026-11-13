#!/bin/bash
# Read-only replay of the NIFTY day forecast, old (live) vs new (13-Nov anchor change).
# Run on EC2 from anywhere: COMMIT=<branch commit> bash run_day_anchor_replay_20261113.sh
# Copies live kadima/*.py into /tmp scratch dirs, swaps day_forecast.py for the new arm,
# runs both arms at nice 19 against read-only kadima.db and a scratch copy of forecast.db.
set -eu
ROOT=${KADIMA_ROOT:-$HOME/kadima-py}
COMMIT=${COMMIT:?set COMMIT}
W=$(mktemp -d /tmp/dayreplay-XXXX)
for arm in old new; do mkdir -p $W/$arm; cp -r $ROOT/kadima $W/$arm/kadima; find $W/$arm -name __pycache__ -prune -exec rm -rf {} +; done
git -C $ROOT show $COMMIT:kadima/day_forecast.py > $W/new/kadima/day_forecast.py
git -C $ROOT show $COMMIT:tools/replay_day_anchor_20261113.py > $W/replay.py
echo "live day_forecast $(sha256sum $ROOT/kadima/day_forecast.py | cut -c1-12)  new $(sha256sum $W/new/kadima/day_forecast.py | cut -c1-12)"
for arm in old new; do
  nice -n 19 ionice -c3 timeout ${REPLAY_TIMEOUT:-2400} $ROOT/venv/bin/python -I $W/replay.py --code $W/$arm --arm $arm --scratch $W --out $W/$arm.json > $W/$arm.log 2>&1 || { echo "$arm FAILED"; tail -20 $W/$arm.log; }
done
$ROOT/venv/bin/python -I - $W <<'PY'
import json, sys
w = sys.argv[1]
o = {r['day']: r for r in json.load(open(f'{w}/old.json'))}
n = {r['day']: r for r in json.load(open(f'{w}/new.json'))}
def f(x, nd=0):
    return '-' if x is None else (f'{x:.{nd}f}' if isinstance(x, (int, float)) else str(x))
print('day        | live: dir end | old: dir end mae2 revs | new: dir end mae2 revs fast official')
tot = {'o': [], 'n': [], 'l': []}
for d in sorted(o):
    a, b = o[d], n.get(d, {})
    L = a['live']
    print(f"{d} | {f(L['dir_hit'])} {f(L['end_error_pts'])} | {f(a.get('dir_hit'))} {f(a.get('end_error_pts'))} {f(a.get('mae_common_k2'),1)} {f(a.get('revisions'))} | "
          f"{f(b.get('dir_hit'))} {f(b.get('end_error_pts'))} {f(b.get('mae_common_k2'),1)} {f(b.get('revisions'))} {f((b.get('kinds') or {}).get('fast',0))} {f(b.get('official'))}"
          + (f"  ERR old={a.get('error')} new={b.get('error')}" if a.get('error') or b.get('error') else ''))
    if a.get('end_error_pts') is not None and b.get('end_error_pts') is not None:
        tot['o'].append(a); tot['n'].append(b); tot['l'].append(L)
def agg(rs, key):
    v = [r[key] for r in rs if r.get(key) is not None]
    return v
for lab, rs in (('old', tot['o']), ('new', tot['n'])):
    e = agg(rs, 'end_error_pts'); m = agg(rs, 'mae_common_k2'); h = agg(rs, 'dir_hit'); r = agg(rs, 'revisions')
    print(f"{lab}: days {len(rs)}  mean|end err| {sum(abs(x) for x in e)/len(e):.1f}  mean path MAE(k>=2) {sum(m)/len(m):.1f}  dir hits {sum(h)}/{len(h)}  revisions {sum(r)}")
PY
echo "workdir $W"
