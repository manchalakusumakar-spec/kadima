"""Bounded, forecast-only self-learning from immutable original outcomes.

A model adjusts NEXT forecasts only in an isolated SHADOW view and ONLY after
session-separated validation. Never edits live model coefficients or source.
Training and evaluation are offline and intentionally reject future labels.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime
import math
import statistics

from .core import EvidenceError, digest, instant


@dataclass(frozen=True)
class LearningPolicy:
    min_train_sessions: int = 8
    min_validation_sessions: int = 3
    min_train_points: int = 30
    min_validation_points: int = 12
    max_abs_bias_points: float = 12.0
    shrinkage: float = 0.65
    required_mae_improvement_points: float = 0.5
    max_p90_regression_points: float = 0.0

    def __post_init__(self):
        if (not 2 <= self.min_train_sessions <= 1000 or
                not 2 <= self.min_validation_sessions <= 1000 or
                not 2 <= self.min_train_points <= 100000 or
                not 2 <= self.min_validation_points <= 100000 or
                not 0 < self.shrinkage <= 1 or
                not 0 < self.max_abs_bias_points <= 100 or
                not 0 <= self.required_mae_improvement_points <= 100 or
                not 0 <= self.max_p90_regression_points <= 100):
            raise EvidenceError('invalid research learning settings')


def _p90(xs: list[float]) -> float:
    arr=sorted(xs)
    if not arr:
        raise EvidenceError('empty error population')
    # nearest-rank, deterministic, avoids dependencies and tiny-sample quantile artifacts
    return arr[max(0,math.ceil(.9*len(arr))-1)]


def _validated_labels(records: list[dict], train_before: datetime) -> list[dict]:
    """Only ORIGINAL, issue-time-identified, exact-target completed outcomes.

    A later actual receipt is not valid for a model frozen before that receipt.
    Labels belonging to the cutoff session are excluded, even if completed.
    """
    seen=set()
    out=[]
    for r in records:
        if r.get('kind')!='ORIGINAL':
            continue
        id_=r.get('forecast_id')
        h=r.get('horizon_minutes')
        if (not isinstance(id_,str) or not id_ or type(h) is not int
                or h not in range(5,61,5)):
            raise EvidenceError('original forecast id and valid horizon required')
        issued=instant(r.get('issued_at'))
        target=instant(r.get('target_at'))
        available=instant(r.get('actual_available_at'))
        if issued.date() >= train_before.date() or target.date()!=issued.date():
            continue
        if not issued < target <= available < train_before:
            raise EvidenceError('future/misaligned outcome or unavailable label')
        if abs((target - instant(r.get('origin_at'))).total_seconds() - h*60) > 1e-6:
            raise EvidenceError('incorrect target horizon')
        if target.minute%5!=0 or target.second!=0 or target.microsecond!=0:
            raise EvidenceError('outcome is not an exact five-minute checkpoint')
        if r.get('complete') is not True or r.get('source_verified') is not True:
            continue
        p,a=r.get('forecast'),r.get('actual')
        if (type(p) not in (int,float) or type(a) not in (int,float)
                or not math.isfinite(p) or not math.isfinite(a) or p<=0 or a<=0):
            raise EvidenceError('invalid original forecast or actual')
        key=(id_,h)
        if key in seen:
            raise EvidenceError('duplicate original outcome')
        seen.add(key)
        out.append({'forecast_id':id_,'horizon_minutes':h,'day':issued.date().isoformat(),
                    'residual':float(a-p),'regime':r.get('regime','ALL')})
    return sorted(out,key=lambda r:(r['day'],r['forecast_id'],r['horizon_minutes']))


@dataclass(frozen=True)
class LearningReport:
    id: str
    model_status: str
    train_before: str
    corrections: dict
    outcomes: dict
    train_sessions: int
    validation_sessions: int
    reason: str
    evidence: str = 'HISTORICAL_HOLDOUT_RESEARCH_NOT_LIVE_ACCURACY'

    def to_dict(self):
        return asdict(self)


def learn_next_forecast_bias(records: list[dict], *, train_before: str,
                             policy: LearningPolicy | None=None) -> LearningReport:
    """Split whole sessions in temporal order, learn robust per-horizon bias.

    If no horizon passes, returns an explicit NO_IMPROVEMENT / INSUFFICIENT_DATA
    report. Never mutates records or assumes that a sub-five goal is achieved.
    """
    policy=policy or LearningPolicy()
    cutoff=instant(train_before)
    clean=_validated_labels(records,cutoff)
    days=sorted(set(r['day'] for r in clean))
    needed=policy.min_train_sessions+policy.min_validation_sessions
    if len(days)<needed:
        return LearningReport(digest({'status':'INSUFFICIENT_DATA','cutoff':cutoff.isoformat(),
                                      'days':days}), 'INSUFFICIENT_DATA',cutoff.isoformat(),{},{},
                              max(0,len(days)-policy.min_validation_sessions),
                              min(len(days),policy.min_validation_sessions),
                              'not enough complete historical sessions')
    # Holdout is later than all training sessions; at least validation min days.
    heldout_count=max(policy.min_validation_sessions,math.ceil(len(days)*.25))
    train_days=set(days[:-heldout_count]); val_days=set(days[-heldout_count:])
    groups=defaultdict(lambda:{'train':[],'validation':[]})
    for r in clean:
        groups[str(r['horizon_minutes'])]['train' if r['day'] in train_days else 'validation'].append(r)
    offsets,metrics={},{}
    for h,group in sorted(groups.items(),key=lambda x:int(x[0])):
        tr,val=group['train'],group['validation']
        if (len(tr)<policy.min_train_points or len(val)<policy.min_validation_points
                or len({r['day'] for r in tr})<policy.min_train_sessions
                or len({r['day'] for r in val})<policy.min_validation_sessions):
            metrics[h]={'status':'INSUFFICIENT_HORIZON_EVIDENCE',
                        'training_points':len(tr),'validation_points':len(val)}
            continue
        raw_bias=statistics.median(r['residual'] for r in tr)
        offset=max(-policy.max_abs_bias_points,min(policy.max_abs_bias_points,
                      raw_bias*policy.shrinkage))
        original=[abs(r['residual']) for r in val]
        learned=[abs(r['residual']-offset) for r in val]
        orig_mae=statistics.mean(original);learn_mae=statistics.mean(learned)
        orig_hits=sum(e<5 for e in original);learn_hits=sum(e<5 for e in learned)
        orig_p90=_p90(original);learn_p90=_p90(learned)
        accepted=(orig_mae-learn_mae>=policy.required_mae_improvement_points and
                  learn_hits>=orig_hits and
                  learn_p90<=orig_p90+policy.max_p90_regression_points)
        metrics[h]={'status':'SHADOW_ELIGIBLE' if accepted else 'NO_VALIDATED_IMPROVEMENT',
                   'training_points':len(tr),'validation_points':len(val),
                   'training_sessions':len({r['day'] for r in tr}),
                   'validation_sessions':len({r['day'] for r in val}),
                   'before_mae':round(orig_mae,4),'after_mae':round(learn_mae,4),
                   'before_below_5_fraction':orig_hits/len(val),
                   'after_below_5_fraction':learn_hits/len(val),
                   'before_p90':round(orig_p90,4),'after_p90':round(learn_p90,4),
                   'candidate_bias_points':round(offset,4)}
        if accepted:
            offsets[h]=round(offset,4)
    state='SHADOW_ELIGIBLE' if offsets else 'NO_VALIDATED_IMPROVEMENT'
    evidence={'cutoff':cutoff.isoformat(),'policy':asdict(policy),
              'training_sessions':sorted(train_days), 'validation_sessions':sorted(val_days),
              'labels_sha256':digest(clean), 'metrics':metrics,'offsets':offsets}
    return LearningReport(digest(evidence),state,cutoff.isoformat(),offsets,metrics,
                          len(train_days),len(val_days),
                          'candidate only, requires prospective shadow test and operator review')


def propose_next_shadow_path(original: dict, learning: LearningReport, *, at: str) -> dict:
    """Produce an UNPUBLISHED shadow alternative to a new, future-issued path.

    Neither the original forecast nor any point at/before `at` is changed.
    This is not automatic live parameter promotion or replacement of originals.
    """
    now=instant(at)
    issued=instant(original.get('issued_at'))
    if issued>now or issued.date()!=now.date():
        raise EvidenceError('next forecast issue time must precede preview')
    if learning.model_status!='SHADOW_ELIGIBLE':
        raise EvidenceError('validated shadow-eligible learning report required')
    cutoff=instant(learning.train_before)
    if cutoff > issued:
        raise EvidenceError('learning report cutoff must precede the new forecast')
    if original.get('kind')!='ORIGINAL' or not original.get('id'):
        raise EvidenceError('only a preserved original can be compared')
    points=[]
    for p in original['points']:
        target=instant(p['target_at'])
        if target<=now:
            continue
        h=str(p['horizon_minutes'])
        if h not in learning.corrections:
            continue
        v=p['forecast']
        if type(v) not in (int,float) or not math.isfinite(v) or v<=0:
            raise EvidenceError('invalid prior prediction')
        corrected=v+learning.corrections[h]
        if corrected<=0:
            raise EvidenceError('invalid corrected shadow forecast')
        points.append({'target_at':target.isoformat(),'horizon_minutes':int(h),
                       'forecast':corrected,'original_forecast':v,
                       'correction_points':learning.corrections[h]})
    if not points:
        raise EvidenceError('no future targets with qualified learning offsets')
    identity=dict(original_id=original['id'],learning_report_id=learning.id,
                  shadow_issued_at=now.isoformat(),points=points)
    return {'id':digest(identity),'kind':'SHADOW_CHALLENGER','mode':'SHADOW_NOT_PUBLISHED',
            'original_id':original['id'],'learning_report_id':learning.id,
            'issued_at':now.isoformat(),'points':points,
            'no_trading_authority':True,'active_forecast_replaced':False}
