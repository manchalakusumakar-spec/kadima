"""Whole-session walk-forward evaluation; no random split or automatic promotion."""
from collections import defaultdict
import numpy as np
from .core import fit, predict, score, instant, InsufficientEvidence, EvidenceError


def walk_forward(records, *, min_samples=100, min_sessions=10, ridge_lambda=10.0):
    records = list(records)
    groups = defaultdict(list)
    seen = set()
    for r in records:
        s = r['snapshot']
        if s['id'] in seen:
            raise EvidenceError('duplicate dataset snapshot id')
        seen.add(s['id'])
        groups[instant(s['issued_at']).date().isoformat()].append(r)
    reports, skips, models = [], {}, {}
    for day in sorted(groups):
        try:
            m = fit(records, day, min_samples=min_samples, min_sessions=min_sessions,
                    ridge_lambda=ridge_lambda)
        except InsufficientEvidence as exc:
            skips[day] = str(exc)
            continue
        models[day] = m.model_id
        for r in sorted(groups[day], key=lambda r: r['snapshot']['issued_at']):
            f = predict(m, r['snapshot'])
            times = [instant(f['issued_at'])]
            times += [instant(a['available_at']) for a in r.get('actuals', {}).values()]
            times += [instant(p['target_at']) for p in f['points']]
            result = score(f, r.get('actuals', {}), as_of=max(times).isoformat())
            result['session_date'], result['model_id'] = day, m.model_id
            reports.append(result)
    errors = defaultdict(list)
    for r in reports:
        for p in r['rows']:
            if p['absolute_error'] is not None:
                errors[p['horizon_minutes']].append(p)
    by_horizon = {}
    for h, points in sorted(errors.items()):
        ae = [p['absolute_error'] for p in points]
        by_horizon[str(h)] = {'n': len(points), 'below_5_fraction': sum(p['below_5'] for p in points)/len(points),
                             'mae': float(np.mean(ae)), 'p90': float(np.quantile(ae,.9)), 'worst': max(ae)}
    return {'evaluation': 'whole-session walk-forward; research only; no automatic promotion',
            'input_sessions': len(groups), 'evaluated_sessions': len(models),
            'models_by_session': models, 'skipped_sessions': skips,
            'by_horizon': by_horizon, 'forecast_reports': reports,
            'limitation': 'No independent validation of upstream provenance, current champion, costs, or live performance.'}
