"""Read-only bridge for existing KADIMA caches; deliberately NOT auto-installed.

No KADIMA import, broker polling, subscriptions, shared indicator mutations, or
model fitting. Caller supplies a bounded closed-bar reader and a dated manifest.
Native source shape reviewed at 15a05c2 (IndexState/storage) and b290213 (radar).
Receipt-time index ticks remain labelled as such; no exchange time is fabricated.
"""
from __future__ import annotations
from collections.abc import Mapping
from datetime import datetime, timedelta
import math
from .core import (EvidenceError, InsufficientEvidence, aggregate_constituents,
                   digest, encode, instant, number, text)

ADAPTER_VERSION = 'kadima-cache-bridge-0.1'
MAX_BARS = 180
# New explicit research definitions, NOT recovered fitted winning parameters.
DEFINITIONS = {'ema': 'SMA seed then recursive 2/(period+1), periods 20/50',
               'rsi': 'Wilder14, SMA seed; flat=50',
               'macd': 'EMA12/26 difference, EMA9 signal, SMA seeds',
               'returns': 'closed 5-minute close-to-close, basis points',
               'history': 'last <=180 complete bars; overnight warmup declared'}
ENRICHMENT = {'vwap_proxy_gap_bps', 'pcr_change_5m', 'put_wall_gap_bps',
              'call_wall_gap_bps', 'atm_iv_pct', 'iv_skew', 'oi_gamma_proxy'}


def ema(values, period):
    if len(values) < period:
        raise InsufficientEvidence('EMA warmup incomplete')
    value = sum(values[:period]) / period
    result = [value]
    for price in values[period:]:
        value += 2 / (period + 1) * (price - value)
        result.append(value)
    return result


def rsi(values):
    if len(values) < 15:
        raise InsufficientEvidence('RSI warmup incomplete')
    changes = [b-a for a, b in zip(values, values[1:])]
    gain = sum(max(d, 0) for d in changes[:14]) / 14
    loss = sum(max(-d, 0) for d in changes[:14]) / 14
    for d in changes[14:]:
        gain = (gain * 13 + max(d, 0)) / 14
        loss = (loss * 13 + max(-d, 0)) / 14
    return 50.0 if gain == loss == 0 else 100.0 if loss == 0 else 100 - 100/(1+gain/loss)


def bars_before(rows, origin, captured):
    """Normalize native candle rows; never interpolate gaps or include forming bars."""
    if len(rows) > MAX_BARS:
        raise EvidenceError('bar reader exceeded its bounded contract')
    result = []
    seen = set()
    for raw in rows:
        row = dict(raw)
        if row.get('interval') != 5 or row.get('closed') not in (True, 1):
            continue
        start = instant(row['start_ts'])
        end = start + timedelta(minutes=5)
        if end > origin:
            continue
        if start.second or start.microsecond or start.minute % 5 or not 555 <= start.hour*60+start.minute < 930:
            raise EvidenceError('invalid exchange-session candle boundary')
        if start in seen:
            raise EvidenceError('duplicate candle boundary')
        seen.add(start)
        prices = {k: number(row[k], k, positive=True) for k in ('open', 'high', 'low', 'close')}
        if not prices['low'] <= min(prices['open'], prices['close']) <= max(prices['open'], prices['close']) <= prices['high']:
            raise EvidenceError('invalid OHLC')
        result.append(dict(start=start, end=end, **prices))
    result.sort(key=lambda r: r['start'])
    if not result or result[-1]['end'] != origin or result[-1]['start'].date() != captured.date():
        raise InsufficientEvidence('latest completed current-session bar is missing')
    for a, b in zip(result, result[1:]):
        if a['start'].date() == b['start'].date() and a['end'] != b['start']:
            raise InsufficientEvidence('intraday candle gap; no interpolation')
        if a['start'].date() != b['start'].date() and (a['end'].hour, a['end'].minute, b['start'].hour, b['start'].minute) != (15, 30, 9, 15):
            raise InsufficientEvidence('incomplete session boundary in warmup')
    return result


def observation(value, observed, available, source):
    return {'value': value, 'status': 'AVAILABLE', 'observed_at': observed.isoformat(),
            'available_at': available.isoformat(), 'source': source}


def technical_features(rows, origin, captured):
    bars = bars_before(rows, origin, captured)
    close = [b['close'] for b in bars]
    if len(close) < 50:
        raise InsufficientEvidence('50 complete bars needed for EMA50 warmup')
    last = close[-1]
    ret = lambda n: (last/close[-n-1]-1)*10000
    values = {'ret_5m_bps': ret(1), 'ret_15m_bps': ret(3),
              'ema20_gap_bps': (last/ema(close, 20)[-1]-1)*10000,
              'ema50_gap_bps': (last/ema(close, 50)[-1]-1)*10000,
              'rsi_centered': (rsi(close)-50)/50}
    e12, e26 = ema(close, 12), ema(close, 26)
    macd = [a-b for a, b in zip(e12[-len(e26):], e26)]
    values['macd_hist_bps'] = (macd[-1]-ema(macd, 9)[-1])/last*10000
    returns = [(b/a-1)*10000 for a, b in zip(close[-25:], close[-24:])]
    values['realized_vol_5m_bps'] = math.sqrt(sum(r*r for r in returns)/len(returns))
    session = [b for b in bars if b['start'].date() == captured.date()]
    # A partial day's history must not masquerade as a full-session range.
    if session[0]['start'].hour == 9 and session[0]['start'].minute == 15:
        high, low = max(b['high'] for b in session), min(b['low'] for b in session)
        values['range_position'] = (last-low)/(high-low) if high > low else .5
    return ({k: observation(v, origin, captured, ADAPTER_VERSION) for k, v in values.items()},
            {'definitions': DEFINITIONS, 'bar_count': len(bars),
             'warmup_sessions': sorted({b['start'].date().isoformat() for b in bars})})


def radar_quotes(radar, manifest, cutoff, captured):
    """Copy only bounded primitive radar data under its existing lock.

    available_at is the conservative cache-read time, NOT an invented past
    receipt time. The legacy radar stores exchange event time but not receipts.
    The caller must still verify the actual deployed feed's timestamp decoder.
    """
    if radar is None:
        raise InsufficientEvidence('NIFTY50 radar missing; do not substitute five heavyweights')
    if radar.reason or radar.source != manifest['source']:
        raise InsufficientEvidence('radar unavailable or manifest source mismatch')
    if (radar.valid_from.isoformat() != manifest['effective_date'] or
            radar.valid_until.isoformat() != manifest['valid_through']):
        raise EvidenceError('radar manifest validity mismatch')
    weights = manifest['weights']
    quotes = []
    with radar.lock:
        members = {m['symbol']: m['weight'] for m in radar.members.values()}
        if len(members) != 50 or set(members) != set(weights) or any(abs(members[s]-weights[s]) > 1e-10 for s in weights):
            raise EvidenceError('radar members/weights differ from supplied manifest')
        if radar.session != cutoff.date():
            raise InsufficientEvidence('radar is from another session')
        target = cutoff.timestamp()-300
        for token, member in radar.members.items():
            state = radar.states.get(token)
            if not state:
                continue
            samples = state.get('samples', ())
            if len(samples) > 62:
                raise EvidenceError('unexpected unbounded radar cache')
            prior = next((p for p in reversed(samples) if p[1] <= target), None)
            if prior is None:
                continue
            quotes.append({'symbol': member['symbol'], 'price': state['price'],
                'previous_price': prior[2], 'observed_at': datetime.fromtimestamp(state['ts'], cutoff.tzinfo).isoformat(),
                'previous_observed_at': datetime.fromtimestamp(prior[1], cutoff.tzinfo).isoformat(),
                'available_at': captured.isoformat(), 'previous_available_at': captured.isoformat(),
                'source': 'existing_constituent_radar_exchange_events'})
    return quotes


def capture(engine, read_bars, manifest, *, source_id, config_id, clock,
            enrichments=None, require_all50=True):
    """Produce a lab packet from existing native engine caches, without mutation.

    read_bars(token, limit, until) must be a bounded READ-ONLY local-cache
    callback returning native candle rows. No broker or model call is allowed.
    enrichments: independently timestamped evidence, never a bare _last_poll.
    No trading score, Gamma gate or auto-trade flag is consulted.
    """
    cutoff = instant(clock())
    origin = cutoff.replace(minute=cutoff.minute-cutoff.minute%5, second=0, microsecond=0)
    text(source_id, 'source_id'); text(config_id, 'config_id')
    st = engine.states['NIFTY']
    if getattr(st, 'is_live_tick', False) is not True:
        raise InsufficientEvidence('anchor is not a live tick')
    anchor, at = number(st.spot, 'spot', positive=True), instant(st.last_updated)
    if at > cutoff:
        raise EvidenceError('anchor changed beyond capture cutoff')
    token = str(st.chain.index.token)
    rows = list(read_bars(token, MAX_BARS, origin))
    other = {}
    for name in ('BANKNIFTY', 'SENSEX'):
        index = engine.states.get(name)
        if index is not None:
            other[name] = list(read_bars(str(index.chain.index.token), MAX_BARS, origin))
    captured = instant(clock())
    if not cutoff <= captured < origin + timedelta(minutes=5) or (captured-cutoff).total_seconds() > 5:
        raise EvidenceError('capture crossed slot or exceeded five-second bound')
    features, metadata = technical_features(rows, origin, captured)
    breadth = None
    try:
        quotes = radar_quotes(getattr(engine, 'constituent_radar', None), manifest, cutoff, captured)
        # Use captured publication reference; quote endpoint tolerance absorbs
        # only the bounded capture delay. Future events are excluded by core.
        breadth = aggregate_constituents(manifest, quotes, captured.isoformat())
        if require_all50 and breadth['fresh_count'] != 50:
            raise InsufficientEvidence('all-50 current and prior endpoints not fresh')
        for key in ('weighted_ret_5m_bps', 'weighted_up_share', 'constituent_weight_coverage'):
            if breadth[key] is not None:
                features[key] = observation(breadth[key], cutoff, captured, 'native_radar:'+manifest['version'])
    except InsufficientEvidence:
        if require_all50:
            raise
    for name, cross in other.items():
        try:
            b = bars_before(cross, origin, captured)
            if len(b) < 2:
                continue
            value = (b[-1]['close']/b[-2]['close']-1)*10000
            features[name.lower()+'_ret_5m_bps'] = observation(value, origin, captured, ADAPTER_VERSION)
        except InsufficientEvidence:
            continue
    for name, value in (enrichments or {}).items():
        if name not in ENRICHMENT or not isinstance(value, Mapping):
            raise EvidenceError('unsupported or malformed enrichment')
        value = dict(value)
        if value.get('status') == 'AVAILABLE':
            if name == 'vwap_proxy_gap_bps':
                text(value.get('volume_instrument'), 'volume-bearing proxy identity')
                text(value.get('mapping'), 'spot/proxy mapping')
            else:
                text(value.get('contract_scope'), 'expiry/contract/strike scope')
        features[name] = value
    # Final timestamp is taken AFTER cache copying and feature calculation.
    # Do not backdate computed-feature availability to the start of the read.
    finished = instant(clock())
    if not captured <= finished < origin + timedelta(minutes=5) or (finished-cutoff).total_seconds() > 5:
        raise EvidenceError('feature calculation exceeded capture deadline')
    for value in features.values():
        if value.get('source') == ADAPTER_VERSION or str(value.get('source', '')).startswith('native_radar:'):
            value['available_at'] = finished.isoformat()
    captured = finished
    raw_id = digest({'source': source_id, 'config': config_id, 'cutoff': cutoff.isoformat(),
                     'captured': captured.isoformat(), 'features': features, 'anchor': anchor,
                     'anchor_at': at.isoformat(), 'manifest': manifest})
    packet = {'id': raw_id, 'instrument': 'NIFTY', 'issued_at': captured.isoformat(),
        'origin_at': origin.isoformat(), 'session_date': captured.date().isoformat(),
        'source_id': source_id, 'config_id': config_id, 'anchor': anchor,
        'anchor_observed_at': at.isoformat(), 'anchor_available_at': captured.isoformat(),
        'closed_bar_end': origin.isoformat(), 'bar_complete': True, 'features': features,
        'capture_metadata': {'adapter': ADAPTER_VERSION, **metadata, 'breadth': breadth,
            'index_timestamp_kind': 'LOCAL_RECEIPT_EXCHANGE_EVENT_TIME_NOT_CAPTURED',
            'available_at_kind': 'CONSERVATIVE_CACHE_CAPTURE_TIME',
            'live_runtime_verified': False, 'trading_authority': False}}
    encode(packet)
    return packet
