"""Offline NIFTY path candidate. No KADIMA imports, network, or order operations.

The adapter must supply verified, decision-time observations. This module checks
provenance contracts; it cannot establish that an upstream field is truthful.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date, datetime, time, timedelta
import hashlib
import json
import math
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

import numpy as np

IST = ZoneInfo('Asia/Kolkata')
VERSION = 'nifty-precision-lab-0.1'
HORIZONS = tuple(range(5, 61, 5))
TARGET_POINTS = 5.0


class EvidenceError(ValueError):
    """An observation, its identity, or its timing is invalid."""


class InsufficientEvidence(EvidenceError):
    """Do not manufacture a fitted forecast from inadequate evidence."""


def number(value: Any, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise EvidenceError(f'{name}: finite number required')
    if positive and value <= 0:
        raise EvidenceError(f'{name}: positive number required')
    return float(value)


def instant(value: str | datetime) -> datetime:
    try:
        d = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(d, datetime) or d.tzinfo is None or d.utcoffset() is None:
            raise ValueError('timezone missing')
        return d.astimezone(IST)
    except (TypeError, ValueError) as exc:
        raise EvidenceError('timezone-aware ISO timestamp required') from exc


def text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f'{name}: non-empty string required')
    return value


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    max_age_seconds: int
    required: bool = False


# Engineering defaults for research, not user-approved trading thresholds.
# Ratios are decimals; returns/gaps are basis points; RSI is (RSI - 50)/50.
FEATURES = (
    FeatureSpec('ret_1m_bps', 90),
    FeatureSpec('ret_5m_bps', 330, True),
    FeatureSpec('ret_15m_bps', 330),
    FeatureSpec('ema20_gap_bps', 330, True),
    FeatureSpec('ema50_gap_bps', 330, True),
    FeatureSpec('rsi_centered', 330, True),
    FeatureSpec('macd_hist_bps', 330),
    FeatureSpec('vwap_proxy_gap_bps', 330),
    FeatureSpec('range_position', 330),
    FeatureSpec('realized_vol_5m_bps', 330),
    FeatureSpec('gap_bps', 23400),
    FeatureSpec('weighted_ret_5m_bps', 30),
    FeatureSpec('weighted_up_share', 30),
    FeatureSpec('constituent_weight_coverage', 30),
    FeatureSpec('banknifty_ret_5m_bps', 330),
    FeatureSpec('sensex_ret_5m_bps', 330),
    FeatureSpec('pcr_change_5m', 90),
    FeatureSpec('put_wall_gap_bps', 90),
    FeatureSpec('call_wall_gap_bps', 90),
    FeatureSpec('atm_iv_pct', 90),
    FeatureSpec('iv_skew', 90),
    FeatureSpec('oi_gamma_proxy', 90),
)


def policy_dict() -> dict:
    return {'version': VERSION, 'features': [asdict(f) for f in FEATURES],
            'horizons': list(HORIZONS), 'target_points': TARGET_POINTS,
            'anchor_max_age_seconds': 30, 'same_session_features': True}


POLICY_ID = digest(policy_dict())


def validate_snapshot(s: Mapping) -> datetime:
    text(s.get('id'), 'snapshot id')
    if s.get('instrument') != 'NIFTY':
        raise EvidenceError('This candidate is NIFTY-only')
    issued = instant(s.get('issued_at'))
    if not time(9, 15) <= issued.time() < time(15, 30):
        raise EvidenceError('issue time outside regular session')
    if s.get('session_date') != issued.date().isoformat():
        raise EvidenceError('session_date must match issue time in IST')
    text(s.get('source_id'), 'source_id')
    text(s.get('config_id'), 'config_id')
    number(s.get('anchor'), 'anchor', positive=True)
    anchor_at = instant(s.get('anchor_observed_at'))
    anchor_available = instant(s.get('anchor_available_at'))
    if not anchor_at <= anchor_available <= issued:
        raise EvidenceError('anchor observation/availability is not causal')
    if anchor_at.date() != issued.date() or (issued - anchor_at).total_seconds() > 30:
        raise EvidenceError('anchor is stale')
    origin = instant(s.get('origin_at'))
    if (origin.second or origin.microsecond or origin.minute % 5 or
            origin.date() != issued.date() or not origin <= issued < origin + timedelta(minutes=5)):
        raise EvidenceError('origin must be a five-minute boundary before actual publication')
    bar_end = instant(s.get('closed_bar_end'))
    if (s.get('bar_complete') is not True or bar_end > origin or
            (issued - bar_end).total_seconds() > 330 or bar_end.date() != issued.date()):
        raise EvidenceError('complete, current-session closed bar required')
    if bar_end.second or bar_end.microsecond or bar_end.minute % 5:
        raise EvidenceError('closed_bar_end must be on a five-minute boundary')
    if not isinstance(s.get('features'), Mapping):
        raise EvidenceError('features mapping required')
    unknown = set(s['features']) - {f.name for f in FEATURES}
    if unknown:
        raise EvidenceError(f'unknown features: {sorted(unknown)}')
    return issued


def encode(s: Mapping) -> tuple[np.ndarray, dict[str, str]]:
    """Each unavailable value gets zero PLUS a separate presence-mask feature.

    Zero without that mask is never treated as an observed neutral market.
    Optional unavailable observations remain visible in the quality report.
    """
    issued = validate_snapshot(s)
    values, mask, quality = [], [], {}
    for spec in FEATURES:
        f = s['features'].get(spec.name)
        state, value = 'MISSING', 0.0
        if f is not None:
            if not isinstance(f, Mapping):
                raise EvidenceError(f'{spec.name}: observation mapping required')
            state = f.get('status', 'MISSING')
            if state == 'AVAILABLE':
                observed = instant(f.get('observed_at'))
                available = instant(f.get('available_at'))
                text(f.get('source'), f'{spec.name}.source')
                raw = number(f.get('value'), spec.name)
                if observed > available:
                    raise EvidenceError(f'{spec.name}: observed after available time')
                if available > issued or observed > issued:
                    state = 'FUTURE'
                elif observed.date() != issued.date():
                    state = 'WRONG_SESSION'
                elif (issued - observed).total_seconds() > spec.max_age_seconds:
                    state = 'STALE'
                else:
                    if spec.name in {'weighted_up_share', 'constituent_weight_coverage', 'range_position'} and not 0 <= raw <= 1:
                        raise EvidenceError(f'{spec.name}: fraction must be in [0,1]')
                    if spec.name == 'rsi_centered' and not -1 <= raw <= 1:
                        raise EvidenceError('rsi_centered must be in [-1,1]')
                    if spec.name in {'realized_vol_5m_bps', 'atm_iv_pct'} and raw < 0:
                        raise EvidenceError(f'{spec.name}: negative value')
                    value = raw
            elif state not in {'MISSING', 'STALE', 'CONFLICTING', 'NOT_APPLICABLE'}:
                raise EvidenceError(f'{spec.name}: unrecognized status')
        if spec.required and state != 'AVAILABLE':
            raise InsufficientEvidence(f'{spec.name}: {state}')
        values.append(value)
        mask.append(float(state == 'AVAILABLE'))
        quality[spec.name] = state
    # Time of day is causal and supports differing opening/midday/closing paths.
    minute = issued.hour * 60 + issued.minute - 555
    values += [minute / 375.0]
    return np.array(values + mask, dtype=float), quality


def aggregate_constituents(manifest: Mapping, quotes: Iterable[Mapping],
                           issued_at: str) -> dict:
    """Five-minute observed contributions, NOT a forward prediction.

    Expects all 50 official fractional weights in a dated manifest. Does not
    silently reweight available names to 100%. Each return compares prices at
    the same requested interval, with at most 30s quote age at either endpoint.
    """
    issued = instant(issued_at)
    version = text(manifest.get('version'), 'weight version')
    text(manifest.get('source'), 'weight source')
    try:
        effective = date.fromisoformat(manifest['effective_date'])
        valid_through = date.fromisoformat(manifest['valid_through'])
    except (KeyError, ValueError, TypeError) as exc:
        raise EvidenceError('explicit manifest validity dates required') from exc
    if not effective <= issued.date() <= valid_through:
        raise EvidenceError('manifest is not valid for issue date')
    if instant(manifest.get('available_at')) > issued:
        raise EvidenceError('future weight manifest')
    weights = manifest.get('weights')
    if not isinstance(weights, Mapping) or len(weights) != 50:
        raise EvidenceError('exactly 50 unique weights required')
    for symbol, weight in weights.items():
        text(symbol, 'symbol')
        number(weight, 'weight', positive=True)
    if abs(math.fsum(weights.values()) - 1.0) > 1e-6:
        raise EvidenceError('weights must be fractions summing to 1')
    seen, used, excluded = set(), [], {}
    for q in quotes:
        symbol = q.get('symbol')
        if symbol not in weights or symbol in seen:
            raise EvidenceError('unknown or duplicate constituent')
        seen.add(symbol)
        now = instant(q.get('observed_at'))
        before = instant(q.get('previous_observed_at'))
        available = instant(q.get('available_at'))
        previous_available = instant(q.get('previous_available_at'))
        current_price = number(q.get('price'), 'price', positive=True)
        previous_price = number(q.get('previous_price'), 'previous_price', positive=True)
        text(q.get('source'), 'constituent source')
        prior_target = issued - timedelta(minutes=5)
        if (now > available or before > previous_available or available > issued or
                previous_available > issued or now > issued or before > prior_target):
            excluded[symbol] = 'FUTURE_OR_NONCAUSAL'
        elif now.date() != issued.date() or before.date() != issued.date():
            excluded[symbol] = 'WRONG_SESSION'
        elif ((issued - now).total_seconds() > 30 or
              (prior_target - before).total_seconds() > 30):
            excluded[symbol] = 'STALE_OR_MISALIGNED'
        else:
            ret = (current_price / previous_price - 1) * 10000
            used.append((symbol, weights[symbol], ret))
    excluded.update({s: 'MISSING' for s in weights if s not in seen})
    return {'weight_version': version, 'configured_count': 50, 'fresh_count': len(used),
            'weighted_ret_5m_bps': math.fsum(w * r for _, w, r in used) if used else None,
            'weighted_up_share': math.fsum(w for _, w, r in used if r > 0) if used else None,
            'constituent_weight_coverage': math.fsum(w for _, w, _ in used),
            'excluded': excluded,
            'interpretation': 'observed partial-universe contribution, not a lead signal'}


def valid_actual(a: Mapping, target: datetime, *, as_of: datetime | None = None) -> float | None:
    text(a.get('source'), 'actual source')
    if instant(a.get('target_at')) != target or instant(a.get('observed_at')) != target:
        raise EvidenceError('actual must match the exact target close, not a later spot')
    available = instant(a.get('available_at'))
    if available < target:
        raise EvidenceError('actual cannot be available before target')
    if a.get('complete') is not True or (as_of is not None and available > as_of):
        return None
    return number(a.get('price'), 'actual price', positive=True)


@dataclass(frozen=True)
class PathModel:
    model_id: str
    policy_id: str
    trained_before: str
    feature_mean: tuple
    feature_scale: tuple
    coefficients: dict
    training_counts: dict
    training_sessions: dict
    ridge_lambda: float
    training_digest: str
    evidence_label: str = 'OFFLINE_CANDIDATE_NOT_LIVE_VALIDATED'

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping) -> 'PathModel':
        return cls(**dict(d))


def fit(records: Iterable[Mapping], train_before: str, *, ridge_lambda: float = 10.0,
        min_samples: int = 100, min_sessions: int = 10) -> PathModel:
    """One direct ridge model per horizon; no endpoint interpolation.

    Whole-session cutoff applies BEFORE scaling, fitting, and target use.
    Defaults are evidence floors for a research fit, not promotion guarantees.
    """
    cutoff = datetime.combine(date.fromisoformat(train_before), time(), IST)
    number(ridge_lambda, 'ridge_lambda', positive=True)
    if (isinstance(min_samples, bool) or isinstance(min_sessions, bool) or
            not isinstance(min_samples, int) or not isinstance(min_sessions, int) or
            min_samples < 2 or min_sessions < 2):
        raise EvidenceError('sample/session minimums must be integers >=2')
    rows, seen = [], set()
    for r in records:
        s = r['snapshot']
        # Future/validation records must not affect any fitted transform.
        issued = instant(s.get('issued_at'))
        if issued >= cutoff:
            continue
        if s['id'] in seen:
            raise EvidenceError('duplicate training snapshot id')
        seen.add(s['id'])
        x, _ = encode(s)
        targets = r.get('actuals', {})
        if not isinstance(targets, Mapping):
            raise EvidenceError('actuals mapping required')
        if set(targets) - {str(h) for h in HORIZONS}:
            raise EvidenceError('unknown target horizon')
        ys = {}
        for h in HORIZONS:
            a = targets.get(str(h))
            target = instant(s['origin_at']) + timedelta(minutes=h)
            if target.date() != issued.date() or target.time() > time(15, 30) or a is None:
                continue
            p = valid_actual(a, target, as_of=cutoff - timedelta(microseconds=1))
            if p is not None:
                ys[h] = (p / s['anchor'] - 1) * 10000
        if ys:
            rows.append((s, x, ys))
    rows.sort(key=lambda r: (instant(r[0]['issued_at']), r[0]['id']))
    if len(rows) < min_samples:
        raise InsufficientEvidence('insufficient pre-cutoff training records')
    matrix = np.array([r[1] for r in rows])
    mean = matrix.mean(axis=0)
    scale = matrix.std(axis=0)
    scale[scale < 1e-8] = 1.0
    design = np.column_stack([np.ones(len(rows)), (matrix - mean) / scale])
    coefficients, counts, sessions = {}, {}, {}
    for h in HORIZONS:
        ix = [i for i, r in enumerate(rows) if h in r[2]]
        session_count = len({rows[i][0]['session_date'] for i in ix})
        counts[str(h)], sessions[str(h)] = len(ix), session_count
        if len(ix) < min_samples or session_count < min_sessions:
            continue
        x = design[ix]
        y = np.array([rows[i][2][h] for i in ix])
        penalty = np.eye(x.shape[1]) * ridge_lambda
        penalty[0, 0] = 0.0
        beta = np.linalg.solve(x.T @ x + penalty, x.T @ y)
        if not np.isfinite(beta).all():
            raise EvidenceError('nonfinite fitted coefficients')
        coefficients[str(h)] = beta.tolist()
    if not coefficients:
        raise InsufficientEvidence('no horizon has enough distinct-session evidence')
    # Hash all included source snapshots + usable targets, rather than filenames.
    training_digest = digest([{'snapshot': s, 'targets_bps': y} for s, _, y in rows])
    identity = {'version': VERSION, 'policy': POLICY_ID, 'cutoff': train_before,
                'training_digest': training_digest, 'lambda': ridge_lambda,
                'coefficients': coefficients, 'mean': mean.tolist(), 'scale': scale.tolist()}
    return PathModel(digest(identity), POLICY_ID, train_before, tuple(mean), tuple(scale),
                     coefficients, counts, sessions, ridge_lambda, training_digest)


def predict(model: PathModel, s: Mapping, *, parent_id: str | None = None) -> dict:
    x, quality = encode(s)
    issued = instant(s['issued_at'])
    cutoff = datetime.combine(date.fromisoformat(model.trained_before), time(), IST)
    if issued < cutoff:
        raise EvidenceError('model was fitted using later-session data')
    if model.policy_id != POLICY_ID:
        raise EvidenceError('feature policy mismatch')
    mean, scale = np.array(model.feature_mean), np.array(model.feature_scale)
    if (mean.shape != x.shape or scale.shape != x.shape or not np.isfinite(mean).all()
            or not np.isfinite(scale).all() or (scale <= 0).any()):
        raise EvidenceError('invalid fitted transform')
    design = np.concatenate(([1.0], (x - mean) / scale))
    points, unavailable = [], []
    for h in HORIZONS:
        target = instant(s['origin_at']) + timedelta(minutes=h)
        if target.date() != issued.date() or target.time() > time(15, 30):
            continue
        beta = model.coefficients.get(str(h))
        if beta is None:
            unavailable.append(h)
            continue
        b = np.asarray(beta)
        if b.shape != design.shape or not np.isfinite(b).all():
            raise EvidenceError('invalid coefficient vector')
        forecast = s['anchor'] * (1 + float(design @ b) / 10000)
        number(forecast, 'predicted price', positive=True)
        points.append({'horizon_minutes': h, 'target_at': target.isoformat(),
                       'lead_seconds_at_issue': (target - issued).total_seconds(),
                       'forecast': forecast, 'target_lower': forecast - TARGET_POINTS,
                       'target_upper': forecast + TARGET_POINTS})
    if not points:
        raise InsufficientEvidence('no fitted horizon remains before close')
    origin = {'snapshot_id': s['id'], 'snapshot_sha256': digest(s),
              'issued_at': issued.isoformat(), 'origin_at': instant(s['origin_at']).isoformat(),
              'model_id': model.model_id,
              'parent_id': parent_id}
    return {'id': digest(origin), **origin, 'instrument': 'NIFTY',
            'anchor': s['anchor'], 'source_id': s['source_id'], 'config_id': s['config_id'],
            'policy_id': model.policy_id, 'status': model.evidence_label,
            'kind': 'REVISION' if parent_id else 'ORIGINAL',
            'evidence_type': 'OFFLINE_REPLAY_NOT_A_LIVE_PUBLICATION',
            'feature_quality': quality, 'unavailable_horizons': unavailable,
            'band_label': '±5 point target only; not a confidence interval',
            'points': points}


def score(forecast: Mapping, actuals: Mapping, *, as_of: str) -> dict:
    issued, now = instant(forecast['issued_at']), instant(as_of)
    origin = instant(forecast['origin_at'])
    rows, evaluated = [], []
    seen = set()
    for p in forecast['points']:
        h = p['horizon_minutes']
        if h not in HORIZONS or h in seen:
            raise EvidenceError('invalid/duplicate horizon')
        seen.add(h)
        target = instant(p['target_at'])
        if target != origin + timedelta(minutes=h) or target <= issued:
            raise EvidenceError('target does not match original issue horizon')
        f = number(p['forecast'], 'forecast', positive=True)
        a = actuals.get(str(h))
        actual = valid_actual(a, target, as_of=now) if a is not None and target <= now else None
        row = {'horizon_minutes': h, 'target_at': target.isoformat(), 'forecast': f,
               'actual': actual, 'deviation': None, 'absolute_error': None,
               'below_5': None, 'status': 'PENDING' if target > now else 'MISSING_OR_INCOMPLETE'}
        if actual is not None:
            error = actual - f
            row.update(deviation=error, absolute_error=abs(error),
                       below_5=abs(error) < TARGET_POINTS, status='EVALUATED')
            evaluated.append(row)
        rows.append(row)
    errors = [r['absolute_error'] for r in evaluated]
    naive = [abs(r['actual'] - forecast['anchor']) for r in evaluated]
    return {'forecast_id': forecast['id'], 'kind': forecast['kind'], 'evaluated_at': now.isoformat(),
            'scheduled_points': len(rows), 'evaluated_points': len(errors),
            'unscored_points': len(rows) - len(errors),
            'below_5_count': sum(r['below_5'] for r in evaluated),
            'below_5_fraction': sum(r['below_5'] for r in evaluated) / len(errors) if errors else None,
            'path_mae_points': float(np.mean(errors)) if errors else None,
            'p90_absolute_error': float(np.quantile(errors, .9)) if errors else None,
            'worst_error_points': max(errors) if errors else None,
            'same_anchor_naive_mae': float(np.mean(naive)) if naive else None,
            'net_error': None, 'net_error_status': 'AGGREGATION_NOT_YET_AGREED',
            'target_pass_rule': 'absolute error strictly <5 before display rounding', 'rows': rows}
