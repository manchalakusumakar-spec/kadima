"""Strict same-snapshot comparisons; never relabel a later revision as original."""
from copy import deepcopy
from .core import EvidenceError, digest, encode, number, score


def baselines(packet, candidate):
    """Return last-price and causal five-minute-return extrapolation references.

    Trend is intentionally simple, not a forecast quality claim. Both use the
    original issue-time anchor; never a later actual or end-of-day observation.
    """
    encode(packet)
    if candidate['snapshot_sha256'] != digest(packet):
        raise EvidenceError('candidate is not bound to this observation packet')
    f = packet['features']['ret_5m_bps']
    ret = number(f['value'], 'past 5-minute return')/10000
    out = {}
    for name in ('last_price', 'causal_trend'):
        x = deepcopy(candidate)
        x['model_id'] = name
        x['id'] = digest({'reference_for': candidate['id'], 'name': name})
        x['status'] = 'REFERENCE_BASELINE_NOT_TRADING_AUTHORITY'
        for p in x['points']:
            move = 0 if name == 'last_price' else ret*p['lead_seconds_at_issue']/300
            value = packet['anchor']*(1+move)
            number(value, 'baseline forecast', positive=True)
            p.update(forecast=value, target_lower=value-5, target_upper=value+5)
        out[name] = x
    return out


def compare(candidate, champion, actuals, *, as_of):
    """Champion must actually have consumed the same immutable packet.

    A legacy forecast without this identity is NOT silently adapted. A host
    integration must journal the champion at issuance with its genuine packet.
    """
    for k in ('instrument', 'snapshot_id', 'snapshot_sha256', 'issued_at', 'origin_at',
              'anchor', 'source_id', 'config_id', 'policy_id'):
        if k not in candidate or k not in champion or candidate[k] != champion[k]:
            raise EvidenceError('not comparable: '+k)
    if candidate['kind'] != 'ORIGINAL' or champion['kind'] != 'ORIGINAL':
        raise EvidenceError('original comparisons must not use revisions')
    targets = lambda f: [(p['horizon_minutes'], p['target_at']) for p in f['points']]
    if targets(candidate) != targets(champion):
        raise EvidenceError('horizon/target mismatch; do not select only easy targets')
    a, b = score(candidate, actuals, as_of=as_of), score(champion, actuals, as_of=as_of)
    return {'candidate': a, 'champion': b, 'same_packet': True,
            'promotion_allowed': False, 'net_error': None,
            'reason': 'comparison is evidence, not automatic model promotion'}
