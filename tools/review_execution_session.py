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


EVENTS = {'CONFIRMED_EVENT','SHIELD_DISPATCH_RESULT','FAST_DISPATCH_RESULT','FAST_CANDIDATE','FAST_LANE_ERROR',
          'auto_executor_entry_rejected','entry_order_result','ANGELONE_REQUEST',
          'ANGELONE_RESPONSE','ANGELONE_BLOCKED','execution_order_submitted','order_submission_unknown',
          'execution_fill_confirmed','execution_order_unresolved','execution_fill_inconsistent',
          'startup_reconcile_complete','startup_reconcile_broker_unreadable'}
EVENTS |= {'SHIELD_SCORE','CONFIRMATION_GATE','SHIELD_DISPATCH_BLOCKED',
           'gamma_tier_change','gamma_signal_emitted','tier_change','shield_invalidated',
           'startup_reconcile_order_book_unreadable','reconcile_external_pending_orders',
           'reconcile_position_quantity_mismatch','position_monitor_loop_error',
           'oi_loop_error','candle_callback_error','engine_starting','engine_stopped'}

CHECKS = {'score','direction','vwap','ema','rsi','closed_bar','technical_fresh',
          'spot_fresh','extra_5m_hold','oi','no_invalidation','bank_confirmation','evidence'}


def audit_summary(path):
    counts=Counter();reasons=Counter();recent=[];failures=Counter();indices={}
    lines=0;malformed=0;first=None;last=None
    with path.open() as handle:
        for line in handle:
            lines+=1
            try:r=json.loads(line)
            except ValueError:
                malformed+=1
                continue
            if not isinstance(r,dict):
                malformed+=1
                continue
            if isinstance(r.get('ts'),str):
                first=first or r['ts'];last=r['ts']
            event=r.get('event')
            if event not in EVENTS:continue
            counts[event]+=1
            if isinstance(r.get('reason'),str):reasons[r['reason']]+=1
            name=r.get('index') or r.get('underlying')
            if isinstance(name,str):
                entry=indices.setdefault(name,dict(events=Counter(),failed_checks=Counter(),max_shield_score=None))
                entry['events'][event]+=1
                score=r.get('score')
                if event=='SHIELD_SCORE' and type(score) in (int,float) and 0<=score<=100:
                    entry['max_shield_score']=max(entry['max_shield_score'] or 0,score)
                checks=r.get('checks')
                if event=='CONFIRMATION_GATE' and isinstance(checks,dict):
                    for check in CHECKS:
                        if checks.get(check) is False:
                            entry['failed_checks'][check]+=1;failures[check]+=1
            # Fast-lane blockers are logged as a list, not a reason string.
            blocked=r.get('blocked_reasons')
            if isinstance(blocked,list):
                reasons.update(reason for reason in blocked if isinstance(reason,str))
            # Whitelist only diagnostics; never output credentials/raw HTTP data.
            recent.append({k:r[k] for k in ('ts','event','index','underlying','direction','signal_id','reason','ok','stage','blocked_reasons','error_type') if k in r})
            recent=recent[-20:]
    return dict(event_counts=dict(counts),reason_counts=dict(reasons),recent=recent,
                failed_confirmation_checks=dict(failures),by_underlying=indices,
                coverage=dict(lines=lines,malformed=malformed,first_ts=first,last_ts=last),
                note='Counts cover only retained audit records. Zero events do not prove no market opportunities. Multiple checks may fail on the same evaluation.')


def replay(conn, token, day):
    from kadima.fast_momentum import setup
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


def database_summary(path, day):
    """Read saved settings and counts; do not import/start the trading engine."""
    if not path.is_file():
        return {'status':'database_missing'}
    out={'note':'Saved settings are current, not historical service environment. Order counts are local ledger records, not broker-book verification.'}
    try:
        with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2) as c:
            c.execute('PRAGMA query_only=ON')
            queries={
                'signals':('SELECT COUNT(*) FROM signals WHERE substr(ts,1,10)=?',(day,)),
                'orders_by_side_and_state':('SELECT side,state,dry_run,COUNT(*),SUM(filled) FROM execution_orders WHERE substr(created_at,1,10)=? GROUP BY side,state,dry_run',(day,)),
                'unresolved_orders':("SELECT COUNT(*) FROM execution_orders WHERE state!='terminal'",()),
            }
            for name,(sql,args) in queries.items():
                try:out[name]=c.execute(sql,args).fetchall()
                except sqlite3.DatabaseError:out[name]={'status':'table_or_columns_unavailable'}
            try:
                row=c.execute('SELECT payload FROM celestial_config WHERE id=1').fetchone()
                cfg=json.loads(row[0]) if row else None
                out['saved_controls']={k:cfg[k] for k in ('auto_trade_enabled','score_threshold','kill_latched','revision') if k in cfg} if isinstance(cfg,dict) else {'status':'not_saved'}
            except (sqlite3.DatabaseError,ValueError,TypeError):
                out['saved_controls']={'status':'unavailable'}
    except sqlite3.DatabaseError:
        out['status']='database_unreadable'
    return out


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--date',type=date.fromisoformat,required=True)
    p.add_argument('--audit-dir',type=Path,default=Path('/var/lib/kadima/audit'))
    p.add_argument('--db',type=Path,default=Path('/var/lib/kadima/kadima.db'))
    p.add_argument('--token',help='Underlying token from the instrument master; optional price-setup replay')
    a=p.parse_args();out={'date':a.date.isoformat(),'read_only':True}
    path=a.audit_dir/(a.date.isoformat()+'.jsonl')
    out['audit']=audit_summary(path) if path.is_file() else {'status':'audit_file_missing'}
    out['database']=database_summary(a.db,a.date.isoformat())
    if a.token:
        with sqlite3.connect(a.db.resolve().as_uri()+'?mode=ro',uri=True) as c:
            c.row_factory=sqlite3.Row;c.execute('PRAGMA query_only=ON')
            out['replay']=replay(c,a.token,a.date.isoformat())
    print(json.dumps(out,indent=2))


if __name__=='__main__':main()
