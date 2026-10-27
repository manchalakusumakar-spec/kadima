import os, sys, json, tempfile, types
from datetime import datetime, timedelta, timezone
sys.path.insert(0, sys.argv[1])
os.environ['KADIMA_DATA_DIR'] = tempfile.mkdtemp()
from kadima import config
config.DATA_DIR = os.environ['KADIMA_DATA_DIR']
from kadima import focus_list as F, move_capture as MC, stock_blast as SB
IST = timezone(timedelta(hours=5, minutes=30))
fails=[]
def check(n,c):
    print(('ok   ' if c else 'FAIL ')+n); (None if c else fails.append(n))
F.DATA_DIR = config.DATA_DIR
check('no file -> empty', F.symbols()==frozenset())
json.dump(dict(day=datetime.now(IST).strftime('%Y-%m-%d'), symbols=['lt','MARUTI'], trigger_score=60, max_trades=5),
          open(os.path.join(config.DATA_DIR,'focus_symbols.json'),'w'))
F._cache.update(at=-1e9, value=None)
check('focus loaded', F.is_focus('LT') and F.is_focus('maruti') and not F.is_focus('TCS'))
check('trigger 60', F.trigger_score(65)==60)
check('max trades 5', F.max_trades(3)==5)
check('stock_blast cap 5 for focus, 3 otherwise', SB._trade_cap('LT')==5 and SB._trade_cap('TCS')==3)
json.dump(dict(day='2020-01-01', symbols=['LT']), open(os.path.join(config.DATA_DIR,'focus_symbols.json'),'w'))
F._cache.update(at=-1e9, value=None)
check('other day -> empty', not F.is_focus('LT'))
json.dump(dict(day='', symbols=['LT']), open(os.path.join(config.DATA_DIR,'focus_symbols.json'),'w'))
F._cache.update(at=-1e9, value=None)
# detector eviction
d = MC.MoveCaptureDetector(max_hot=2)
now = datetime.now(IST)
for n, s in [('A',90),('B',85),('LT',61)]:
    d.hot[n] = dict(underlying=n, score=s, observed_at=now.isoformat())
d._trim(now)
check('focus LT kept when hot list over cap', set(d.hot)=={'A','B','LT'})
d.hot['C'] = dict(underlying='C', score=99, observed_at=now.isoformat())
d._trim(now)
check('non-focus still capped at 2, LT kept', set(d.hot)=={'A','C','LT'})
print(len(fails),'failure(s)'); sys.exit(1 if fails else 0)
