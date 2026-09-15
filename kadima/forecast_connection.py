"""Local adapter for an actual forecast producer; never fabricates a projection.

Input path is private server-side state. Missing/invalid output clears the
connection and ordinary trailing remains in force. Feature flag is preserved.
"""
import json
import os
from datetime import datetime
from pathlib import Path
from .forecast_trail import Projection, evaluate
from .utils import now_ist
from . import config, audit

PATH = Path(os.environ.get('KADIMA_FORECAST_PATH',
                          os.path.expanduser('~/kadima-py/state/kadima_forecasts.json')))


def read_projections(path=PATH):
    try:
        with path.open('rb') as f:
            raw = f.read(1_000_001)
        if len(raw) > 1_000_000:
            return [], 'oversize'
        data = json.loads(raw)
        if isinstance(data, dict) and data.get('mode') == 'observation':
            return [], 'observation_only'
        if data.get('schema_version') != 1 or not isinstance(data.get('projections'), list):
            return [], 'invalid_schema'
        if len(data['projections']) > 20:
            return [], 'too_many_projections'
        projections = []
        for row in data['projections']:
            p = dict(row)
            for key in ('generated_at', 'market_data_at', 'target_at'):
                p[key] = datetime.fromisoformat(p[key])
            projections.append(Projection(**p))
        keys = [(p.exchange, p.token, p.signal_id) for p in projections]
        if len(keys) != len(set(keys)):
            return [], 'duplicate_projection'
        return projections, 'read'
    except FileNotFoundError:
        return [], 'producer_output_missing'
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return [], 'invalid_output'


def refresh(executor, path=PATH):
    projections, status = read_projections(path)
    accepted = {}
    now = now_ist()
    previous = getattr(executor, 'forecast_connection_status', {})
    with executor._lock:
        for p in projections:
            pos = executor.state.positions.get(str(p.signal_id))
            if pos is None or pos.status != 'open':
                continue
            try:
                d = evaluate(entry=pos.entry_price, peak=max(pos.peak_price or pos.entry_price, pos.entry_price),
                    previous_stop=pos.effective_stop or 0, ltp=pos.entry_price,
                    quantity=pos.remaining_qty, giveback_fraction=.5,
                    token=pos.token, exchange=pos.exchange, signal_id=pos.signal_id,
                    now=now, projection=p)
                if not d.reason.startswith('ordinary_trail_fallback:'):
                    accepted[p.signal_id] = p
            except (TypeError, ValueError, AttributeError):
                continue
        executor._profit_projections = accepted
        executor.forecast_connection_status = {
            'status': 'connected' if accepted else status if not projections else 'no_valid_matching_forecast',
            'accepted': len(accepted), 'checked_at': now.isoformat(),
            'trailing_enabled': bool(getattr(config, 'FORECAST_TRAIL_ENABLED', False))}
    state = executor.forecast_connection_status
    if any(previous.get(k) != state.get(k) for k in ('status','accepted','trailing_enabled')):
        audit.log('forecast_connection_status', **state)
    return state
