"""Drive Engine._on_candle_close (5-min) through a fixed sequence on any source tree and
print every Shield output as JSON. Used to prove SHIELD v9 off == the live snapshot.

    python tools/v9_engine_diff.py --root <tree> [--v9-live] [--with-futures]
No broker, no network, no market hours (dispatch is skipped).
"""
import argparse
import json
import os
import socket
import sys
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace as NS

ap = argparse.ArgumentParser()
ap.add_argument('--root', required=True)
ap.add_argument('--v9-live', action='store_true')
ap.add_argument('--with-futures', action='store_true')
a = ap.parse_args()
os.environ['KADIMA_DATA_DIR'] = tempfile.mkdtemp(prefix='v9diff-')
os.environ['HOME'] = os.environ['KADIMA_DATA_DIR']
for k in ('KADIMA_CELESTIAL_ENABLED', 'KADIMA_SHIELD_V9_LIVE'):
    os.environ.pop(k, None)
if a.v9_live:
    os.environ['KADIMA_SHIELD_V9_LIVE'] = '1'
socket.socket.connect = lambda *x, **k: (_ for _ in ()).throw(RuntimeError('offline'))
sys.path.insert(0, a.root)

from kadima import audit, engine, indicators  # noqa: E402
from kadima import storage  # noqa: E402
storage.recent_candles('0', 5, 1)   # the live database always exists; create it for both trees
from kadima.config import IST  # noqa: E402

EVENTS = []
audit.log = lambda event, **f: EVENTS.append(dict(event=event, **f))
engine.audit.log = audit.log
CLOCK = {'now': datetime(2026, 10, 13, 10, 0, tzinfo=IST)}
engine.now_ist = lambda: CLOCK['now']
engine.in_market_hours = lambda: False          # _evaluate returns before any dispatch
if os.environ.get('V9DIFF_MACRO_CLEAR'):
    import kadima.gap_live as _g; _g._macro_clear = lambda now: True

E = engine.Engine.__new__(engine.Engine)
E.bank_states, E.bank_tokens = {}, {}
E.fut_bars, E.fut_tokens, E.fut_builders, E.v9_oi, E.v9_hist = {}, {}, {}, {}, {}
E.scrips = NS(nearest_expiry=lambda idx: '14OCT2026', rows=[])
idx = engine.INDICES['NIFTY']
E.token_to_index = {idx.token: 'NIFTY'}


class Vel:
    def __init__(self):
        self.items = []

    def alerts(self):
        return list(self.items)


chain = NS(index=idx, quotes={}, velocity=Vel(), _last_poll=0)
st = engine.IndexState(name='NIFTY')
st.chain = chain
E.states = {'NIFTY': st}
if hasattr(engine, 'shield_v9'):
    from kadima import shield_v9
    E.v9_oi['NIFTY'], E.v9_hist['NIFTY'] = shield_v9.OIHistory(), []
    if a.with_futures:
        E.fut_bars['NIFTY'] = shield_v9.FuturesBars(dict(symbol='NIFTY27OCT26FUT', exchange='NFO'))
        E.fut_builders['NIFTY'] = NS(live=lambda i: None, session_vwap=lambda: None)

out = []
for i in range(14):
    now = CLOCK['now'] = datetime(2026, 10, 13, 10, 0, tzinfo=IST) + timedelta(minutes=5 * i)
    up = i % 7 < 5
    spot = 25000 + (8 * i if up else -10 * i)
    st.spot, st.last_updated = float(spot), now
    st.snap = indicators.Snapshot(close=float(spot), vwap=float(spot), ema_fast=25010.0 if up else 24990.0,
                                  ema_slow=25000.0, rsi=62.0 if up else 38.0, vol=0, avg_vol=None,
                                  failed_breakout=(i == 6), failed_breakdown=(i == 3))
    st.technical_at = now - timedelta(seconds=30)
    chain.velocity.items = [dict(token='C1')] if i % 4 == 0 else []
    chain.quotes = {'C1': NS(token='C1', opt_type='CE', strike=25050.0, oi=1000.0 * (1 - 0.2 * (i % 3)), iv=0.14),
                    'P1': NS(token='P1', opt_type='PE', strike=25000.0, oi=1000.0 + 150 * i, iv=0.15)}
    if a.with_futures and E.fut_bars:
        fut = 25030 + (8 * i if up else -10 * i)
        E.fut_bars['NIFTY'].add(now, float(fut), 900 + 100 * i if i in (4, 9, 10, 11) else 300, 25020.0)
    candle = NS(token=idx.token, interval=5, start=now - timedelta(minutes=5), close=float(spot))
    EVENTS.clear()
    E._on_candle_close(candle)
    keep = [e for e in EVENTS if e['event'] in ('SHIELD_SCORE', 'CONFIRMATION_GATE', 'tier_change', 'SHIELD_V9')]
    out.append(dict(bar=i, score=st.score, direction=st.direction, tier=st.tier,
                    events=[{k: v for k, v in e.items() if not (e['event'] == 'SHIELD_V9' and k == 'filters')}
                            for e in keep]))
print(json.dumps(out, default=str, sort_keys=True, indent=1))
