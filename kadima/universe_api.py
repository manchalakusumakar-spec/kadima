"""Bounded, read-only view of the stock scanner for the public state API.

Freshness measures scanner observation time, not exchange trade freshness.
This module never imports a broker client or evaluates execution eligibility.
"""
from datetime import datetime
import json
import math
from pathlib import Path

from .config import IST
from .utils import in_market_hours, now_ist

SNAPSHOT_PATH = Path.home() / 'kadima-py/state/universe_snapshot.json'
MAX_BYTES = 2 * 1024 * 1024
SNAPSHOT_AGE_SECONDS = 180
SCORE_AGE_SECONDS = 90
CONTRACT_AGE_SECONDS = 30
CONTRACT_FIELDS = {
    'symbol', 'strike', 'type', 'ltp', 'oi', 'volume', 'delta', 'iv',
    'spread_pct', 'score', 'token', 'exchange', 'expiry', 'updated_at',
    'pace_status', 'baseline_at', 'premium_move_pct', 'rejected',
}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _text(value):
    return value[:120] if isinstance(value, str) else None


def _age(stamp, now):
    try:
        parsed = datetime.fromisoformat(stamp)
        if parsed.tzinfo is None:
            return None
        seconds = (now - parsed).total_seconds()
        return round(seconds, 1) if seconds >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _recent(age, maximum):
    return age is not None and age <= maximum


def read_universe(path=None, now=None):
    """Keep index FETCH available even if the independent scanner is unavailable."""
    now = (now or now_ist()).astimezone(IST)
    market_open = in_market_hours(now)
    out = dict(status='unavailable', as_of=None, age_seconds=None, fresh=False,
               market_open=market_open, score_model='stock_gamma',
               coverage={}, tracked_count=0, returned_count=0, top20=[],
               note='Read-only scanner observations; scores are not Shield confirmation '
                    'or order authorization. Check row and contract freshness separately.')
    try:
        with Path(path or SNAPSHOT_PATH).open('rb') as handle:
            raw = handle.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            out['reason'] = 'snapshot_too_large'
            return out
        source = json.loads(raw)
        if not isinstance(source, dict) or not isinstance(source.get('tracked'), list):
            raise ValueError('snapshot_schema')
        prices = source.get('prices', {})
        coverage = source.get('coverage', {})
        if not isinstance(prices, dict) or not isinstance(coverage, dict):
            raise ValueError('snapshot_schema')
    except FileNotFoundError:
        out.update(status='missing', reason='snapshot_missing')
        return out
    except (OSError, ValueError, TypeError, RecursionError):
        out['reason'] = 'snapshot_unreadable'
        return out

    age = _age(source.get('ts'), now)
    snapshot_fresh = market_open and _recent(age, SNAPSHOT_AGE_SECONDS)
    out.update(as_of=_text(source.get('ts')), age_seconds=age)
    seen = set()
    rows = []
    invalid_rows = 0
    for item in source['tracked']:
        if not isinstance(item, dict):
            invalid_rows += 1
            continue
        name, score, spot = item.get('name'), _number(item.get('score')), _number(item.get('spot'))
        if not isinstance(name, str) or not name or len(name) > 80 or name in seen \
                or score is None or not 0 <= score <= 100 or spot is None or spot <= 0:
            invalid_rows += 1
            continue
        seen.add(name)
        row_age = _age(item.get('last_updated'), now)
        fresh = snapshot_fresh and _recent(row_age, SCORE_AGE_SECONDS)
        contract = item.get('best_contract')
        if isinstance(contract, dict):
            contract = {key: (_text(value) if isinstance(value, str) else _number(value))
                        for key, value in contract.items() if key in CONTRACT_FIELDS}
            contract_age = _age(contract.get('updated_at'), now)
            contract.update(age_seconds=contract_age,
                            fresh=fresh and _recent(contract_age, CONTRACT_AGE_SECONDS))
        else:
            contract = None
        rows.append(dict(name=name, spot=spot, score=score, tier=_text(item.get('tier')),
                         direction=item.get('direction') if item.get('direction') in ('CE', 'PE') else None,
                         last_updated=_text(item.get('last_updated')), age_seconds=row_age,
                         fresh=fresh, best_contract=contract,
                         strike_status=_text(item.get('strike_status'))))

    rows.sort(key=lambda row: (not row['fresh'], -row['score'], row['name']))
    priced = [row for row in prices.values() if isinstance(row, dict)
              and _number(row.get('ltp')) is not None and row['ltp'] > 0]
    fresh_prices = sum(snapshot_fresh and _recent(_age(row.get('received_at'), now),
                                                 SCORE_AGE_SECONDS) for row in priced)
    fresh_rows = sum(row['fresh'] for row in rows)
    expected = coverage.get('expected')
    expected = expected if type(expected) is int and expected >= 0 else None
    out['coverage'] = dict(expected=expected, priced=len(priced), fresh_prices=fresh_prices,
                           fresh_scores=fresh_rows, invalid_rows=invalid_rows,
                           gamma_limit=coverage.get('gamma_limit')
                           if type(coverage.get('gamma_limit')) is int else None)
    out.update(tracked_count=len(rows), returned_count=min(20, len(rows)), top20=rows[:20])
    if not market_open:
        out['status'] = 'market_closed'
    elif not _recent(age, SNAPSHOT_AGE_SECONDS):
        out['status'] = 'stale'
    elif not rows:
        out['status'] = 'unavailable' if invalid_rows else 'empty'
    elif fresh_rows == 0:
        out['status'] = 'stale'
    elif invalid_rows or fresh_rows < len(rows) or fresh_prices < len(priced) \
            or expected is None or len(priced) < expected:
        out['status'] = 'partial'
    else:
        out['status'] = 'ready'
    out['fresh'] = out['status'] == 'ready'
    return out
