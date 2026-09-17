"""Read-only audit summary and chronological price-setup replay. Never trades.

No broker credentials required. Cannot reconstruct executable fills from LTPs.
Run with --help. All database access uses mode=ro, query_only.
"""
import argparse
from collections import Counter
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from kadima.fast_momentum import setup


EVENTS = {'CONFIRMED_EVENT','SHIELD_DISPATCH_RESULT','FAST_DISPATCH_RESULT','FAST_CANDIDATE','FAST_LANE_ERROR',
          'auto_executor_entry_rejected','entry_order_result','ANGELONE_REQUEST',
          'ANGELONE_RESPONSE','ANGELONE_BLOCKED','execution_order_submitted','order_submission_unknown',
          'execution_fill_confirmed','execution_order_unresolved','execution_fill_inconsistent',
          'startup_reconcile_complete','startup_reconcile_broker_unreadable'}


def audit_summary(path):
    counts=Counter();reasons=Counter();recent=[]
    with path.open() as handle:
        for line in handle:
            try:r=json.loads(line)
            except ValueError:continue
            event=r.get('event')
            if event not in EVENTS:continue
            counts[event]+=1
            if isinstance(r.get('reason'),str):reasons[r['reason']]+=1
            # Whitelist only diagnostics; never output credentials/raw HTTP data.
            recent.append({k:r[k] for k in ('ts','event','index','underlying','direction','signal_id','reason','ok','stage','blocked_reasons','error_type') if k in r})
            recent=recent[-20:]
    return dict(event_counts=dict(counts),reason_counts=dict(reasons),recent=recent)


def replay(conn, token, day):
    one=[dict(r) for r in conn.execute('SELECT * FROM candles WHERE token=? AND interval=1 '
        'AND closed=1 AND substr(start_ts,1,10)=? ORDER BY start_ts',(token,day))]
    five=[dict(r) for r in conn.execute('SELECT * FROM candles WHERE token=? AND interval=5 '
        'AND closed=1 AND substr(start_ts,1,10)=? ORDER BY start_ts',(token,day))]
    found=[];reasons=Counter()
    for i,r in enumerate(one):
        now=datetime.fromisoformat(r['start_ts'])+timedelta(minutes=1)
        known5=[b for b in five if datetime.fromisoformat(b['start_ts'])+timedelta(minutes=5)<=now]
        try:
            candidate=setup(one[max(0,i-59):i+1],known5[-30:],r['close'],now)
            found.append(dict(at=now.isoformat(),direction=candidate['direction'],
                              breakout_at=candidate['breakout_at'],spot=r['close'],stop=candidate['spot_stop']))
        except (ValueError,TypeError,KeyError):reasons['not_confirmed_or_missing_data']+=1
    return dict(price_setups=found,checked_bars=len(one),excluded=dict(reasons),
                note='Price-only replay, not executable trades or P&L. No historical bid/ask/fill assumptions. Premium/volume, news and execution gates are not replayed.')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--date',type=date.fromisoformat,required=True)
    p.add_argument('--audit-dir',type=Path,default=Path('/var/lib/kadima/audit'))
    p.add_argument('--db',type=Path,default=Path('/var/lib/kadima/kadima.db'))
    p.add_argument('--token',help='Underlying token from the instrument master; optional price-setup replay')
    a=p.parse_args();out={'date':a.date.isoformat(),'read_only':True}
    path=a.audit_dir/(a.date.isoformat()+'.jsonl')
    out['audit']=audit_summary(path) if path.is_file() else {'status':'audit_file_missing'}
    if a.token:
        with sqlite3.connect(a.db.resolve().as_uri()+'?mode=ro',uri=True) as c:
            c.row_factory=sqlite3.Row;c.execute('PRAGMA query_only=ON')
            out['replay']=replay(c,a.token,a.date.isoformat())
    print(json.dumps(out,indent=2))


if __name__=='__main__':main()
