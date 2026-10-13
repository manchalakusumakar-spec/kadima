"""Read-only, bounded snapshot for the bottom-of-forecast learning panel.

Call with already scored original-forecast journals and EventJournal.recent().
This does not create an API route or authorize production publication.
"""
from __future__ import annotations

from .core import EvidenceError, instant

MODES={'NOT_INSTALLED','STAGED_DISABLED','CAPTURE_ONLY','SHADOW_RUNNING',
       'WAITING_FOR_LIVE_SESSION','BLOCKED','UNAVAILABLE'}


def panel_snapshot(*, as_of: str, mode: str, event_rows: list,
                   original_scores: list, learning_report=None) -> dict:
    now=instant(as_of)
    if mode not in MODES:
        raise EvidenceError('unknown observer status')
    if len(event_rows)>50 or len(original_scores)>200:
        raise EvidenceError('bounded read-only panel inputs required')
    events=[]
    for x in event_rows:
        if not isinstance(x,dict) or not isinstance(x.get('event'),dict):
            raise EvidenceError('event journal shape required')
        event=x['event']
        if instant(event['trigger_at'])>now:
            continue  # never show future events
        receipt=x.get('receipt')
        if receipt is not None and instant(receipt['recorded_at'])>now:
            receipt=None
        events.append({'event':event,'receipt':receipt})
    events.sort(key=lambda e:e['event']['trigger_at'],reverse=True)
    seen=set();scored=0;success=0
    for score in original_scores:
        if score.get('kind')!='ORIGINAL':
            raise EvidenceError('learning panel scores must use original forecasts')
        id_=score.get('forecast_id')
        if not isinstance(id_,str) or not id_ or id_ in seen:
            raise EvidenceError('duplicate/missing original score identity')
        seen.add(id_)
        if instant(score['evaluated_at'])>now:
            continue
        rows=score.get('rows')
        if not isinstance(rows,list) or len(rows)>75:
            raise EvidenceError('bounded five-minute score rows required')
        for row in rows:
            if row.get('status')!='EVALUATED':
                continue
            if instant(row['target_at'])>now:
                raise EvidenceError('future actual may not be scored')
            a,p=row.get('actual'),row.get('forecast')
            if type(a) not in (int,float) or type(p) not in (int,float):
                raise EvidenceError('actual and forecast prices required')
            delta=abs(float(a)-float(p))
            if (row.get('absolute_error') is None or
                    abs(row['absolute_error']-delta)>1e-6 or
                    type(row.get('below_5')) is not bool or
                    row['below_5']!=(delta<5)):
                raise EvidenceError('invalid per-point original score')
            scored+=1;success+=int(delta<5)
    learning=learning_report.to_dict() if learning_report is not None else {
        'model_status':'NOT_TRAINED','outcomes':{}}
    return {'mode':mode,'as_of':now.isoformat(),
            'accuracy':{'scored':scored,'below_5_count':success,
                        'below_5_fraction':success/scored if scored else None},
            'events':events[:12],'learning':learning,
            'net_error':None,'net_error_status':'AGGREGATION_NOT_YET_AGREED',
            'source_status':'RESEARCH_READ_ONLY_NOT_LIVE_ENDPOINT'}
