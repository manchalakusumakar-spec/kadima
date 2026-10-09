"""Bounded NIFTY constituent observations; no broker calls or trading authority."""
from collections import deque
from datetime import date, datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
from threading import Lock

IST = timezone(timedelta(hours=5, minutes=30))


def number(value):
    if isinstance(value, bool):
        raise ValueError('boolean numeric input')
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('nonfinite input')
    return value


class ConstituentRadar:
    """At most 62 five-second price samples per constituent, with one lock."""
    def __init__(self, manifest=None, rows=(), today=None):
        self.lock = Lock()
        self.members = {}
        self.states = {}
        self.reason = 'disabled'
        self.valid_from = self.valid_until = None
        self.source = None
        self.session = None
        if manifest is None:
            return
        today = today or datetime.now(IST).date()
        try:
            self.valid_from = date.fromisoformat(manifest['valid_from'])
            self.valid_until = date.fromisoformat(manifest['valid_until'])
            if not self.valid_from <= today <= self.valid_until:
                raise ValueError('weights outside validity period')
            if (self.valid_until - self.valid_from).days > 31:
                raise ValueError('weight validity exceeds 31 days')
            self.source = manifest['source']
            if not isinstance(self.source, str) or not self.source.strip():
                raise ValueError('missing weight source')
            members = manifest['members']
            if len(members) != 50:
                raise ValueError('exactly 50 constituents required')
            symbols = set()
            total = 0
            for m in members:
                symbol = m['symbol']
                if symbol in symbols:
                    raise ValueError('duplicate constituent')
                symbols.add(symbol)
                weight = number(m['weight'])
                if not 0 < weight < 1:
                    raise ValueError('weights must be fractions')
                matches = [r for r in rows if r.get('exch_seg') == 'NSE'
                           and r.get('symbol') == symbol + '-EQ' and r.get('token')]
                if len(matches) != 1:
                    raise ValueError('missing or ambiguous NSE equity token: ' + symbol)
                token = str(matches[0]['token'])
                if token in self.members:
                    raise ValueError('duplicate token')
                self.members[token] = dict(symbol=symbol, weight=weight, sector=str(m['sector']))
                total += weight
            if abs(total - 1) > 0.0001:
                raise ValueError('weights must sum to one')
            self.reason = None
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            self.members.clear()
            self.reason = 'invalid_manifest: ' + str(exc)

    @classmethod
    def from_environment(cls, rows):
        if os.getenv('KADIMA_NIFTY_RADAR_ENABLED', '0') != '1':
            return cls()
        try:
            path = Path(os.environ['KADIMA_NIFTY_RADAR_WEIGHTS'])
            if path.stat().st_size > 32768:
                raise ValueError('manifest too large')
            return cls(json.loads(path.read_text()), rows)
        except (OSError, KeyError, ValueError) as exc:
            obj = cls()
            obj.reason = 'manifest_unavailable: ' + type(exc).__name__
            return obj

    @property
    def tokens(self):
        return list(self.members)

    def on_tick(self, tick):
        token = str(tick.get('token', ''))
        if token not in self.members or tick.get('exchange_type') != 1:
            return
        try:
            received = tick['ts'].astimezone(IST)
            event = tick['exchange_ts'].astimezone(IST)
            if tick['ts'].tzinfo is None or tick['exchange_ts'].tzinfo is None:
                return
            price = number(tick['ltp'])
            volume = number(tick['volume'])
            if price <= 0 or volume < 0 or not 0 <= (received-event).total_seconds() <= 5:
                return
            if not self.valid_from <= received.date() <= self.valid_until:
                return
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            return
        ts = event.timestamp()
        with self.lock:
            if self.session is not None and received.date() < self.session:
                return
            if self.session != received.date():
                self.states.clear()
                self.session = received.date()
            s = self.states.get(token)
            if s and (ts <= s['ts'] or volume < s['volume']):
                return
            if s is None:
                s = dict(samples=deque(maxlen=62), pv=0., dv=0., volume=volume, ts=ts)
                self.states[token] = s
            delta = volume-s['volume']
            s['pv'] += price*delta
            s['dv'] += delta
            s.update(ts=ts, price=price, volume=volume)
            bucket = int(ts//5)
            sample = (bucket, ts, price)
            if s['samples'] and s['samples'][-1][0] == bucket:
                s['samples'][-1] = sample
            else:
                s['samples'].append(sample)

    def snapshot(self, now=None):
        now = now or datetime.now(IST)
        now = now.astimezone(IST)
        out = dict(as_of=now.isoformat(), mode='observation_only', status='unavailable',
                   reason=self.reason, source=self.source, probability=None,
                   configured_count=len(self.members), fresh_weight=0., horizons={})
        if self.reason:
            return out
        if not self.valid_from <= now.date() <= self.valid_until:
            out['reason'] = 'expired_weights'
            return out
        with self.lock:
            live = [(self.members[t], s) for t, s in self.states.items()
                    if self.session == now.date() and 0 <= now.timestamp()-s['ts'] <= 5]
            out['fresh_weight'] = sum(m['weight'] for m,s in live)
            out['fresh_count'] = len(live)
            out['status'] = 'ready' if len(live) == 50 else 'degraded'
            out['reason'] = None if len(live) == 50 else 'incomplete_fresh_coverage'
            out['observed_vwap_above_weight'] = sum(m['weight'] for m,s in live
                if s['dv'] > 0 and s['price'] > s['pv']/s['dv'])
            out['observed_vwap_coverage_weight'] = sum(m['weight'] for m,s in live if s['dv'] > 0)
            for minutes in (1,3,5):
                contributions = []
                sectors = {}
                for m,s in live:
                    target = now.timestamp()-minutes*60
                    prior = next((p for p in reversed(s['samples']) if p[1] <= target), None)
                    if prior is None or target-prior[1] > 10:
                        continue
                    value = m['weight']*(s['price']/prior[2]-1)*10000
                    contributions.append(dict(symbol=m['symbol'], weight=m['weight'], contribution_bps=value))
                    sectors[m['sector']] = sectors.get(m['sector'],0)+value
                complete = len(contributions) == 50
                total = sum(c['contribution_bps'] for c in contributions)
                ordered = sorted(contributions,key=lambda c:c['contribution_bps'])
                out['horizons'][str(minutes)] = dict(ready=complete,
                    coverage_weight=sum(c['weight'] for c in contributions),
                    weighted_return_bps=total if complete else None,
                    direction=('bullish' if total>0 else 'bearish' if total<0 else 'flat') if complete else None,
                    positive_weight=sum(c['weight'] for c in contributions if c['contribution_bps']>0),
                    negative_weight=sum(c['weight'] for c in contributions if c['contribution_bps']<0),
                    sector_contribution_bps=sectors,
                    top_positive=list(reversed([c for c in ordered if c['contribution_bps']>0][-5:])),
                    top_negative=[c for c in ordered if c['contribution_bps']<0][:5])
        h1, h3 = out['horizons']['1'], out['horizons']['3']
        out['acceleration_bps_per_minute'] = (h1['weighted_return_bps'] - h3['weighted_return_bps']/3
            if h1['ready'] and h3['ready'] else None)
        out['limitations'] = ['Observed weighted returns are not predictions.',
            'VWAP is tick-delta approximation since connection, not full-session exchange VWAP.',
            'Relative volume baseline and calibrated forecast probability unavailable.']
        return out
