"""Forecast-only event-driven reforecast requests and immutable update receipts.

No broker imports, subscriptions, order calls, timers, or production writes.
Reforecast is a *request*; no event is evidence of an issued forecast until
there is an independent publication receipt. All thresholds are research
candidates and must be calibrated before an operator enables shadow operation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
import json
import math
from pathlib import Path
import sqlite3
import threading

from .core import EvidenceError, canonical, digest, instant


@dataclass(frozen=True)
class TriggerPolicy:
    # These are *proposed* dimensionless anomaly thresholds, not validated
    # rates or proven Gamma criteria. The engine starts disabled.
    enabled: bool = False
    price_z: float = 2.5
    extreme_price_z: float = 4.0
    volume_ratio: float = 2.0
    extreme_volume_ratio: float = 4.0
    breadth_z: float = 2.0
    oi_velocity_z: float = 2.5
    gamma_pressure_z: float = 2.5
    min_baseline_sessions: int = 10
    min_confirming_ticks: int = 3
    minimum_tick_span_seconds: float = 2.0
    min_seconds_between_shocks: float = 30.0
    max_signal_age_seconds: float = 30.0
    review_lag_seconds: float = 120.0

    def __post_init__(self):
        if self.min_baseline_sessions < 2 or self.min_confirming_ticks < 2:
            raise EvidenceError('anomaly baselines require distinct sessions and corroboration')
        if not all(math.isfinite(x) and x > 0 for x in (
                self.price_z, self.extreme_price_z, self.volume_ratio,
                self.extreme_volume_ratio, self.breadth_z, self.oi_velocity_z,
                self.gamma_pressure_z, self.minimum_tick_span_seconds,
                self.min_seconds_between_shocks, self.max_signal_age_seconds,
                self.review_lag_seconds)):
            raise EvidenceError('invalid research trigger parameters')


def _session(ts: datetime) -> bool:
    return ts.weekday() < 5 and time(9, 15) <= ts.time() < time(15, 30)


def _valid_signal(raw: dict | None, now: datetime, policy: TriggerPolicy) -> dict | None:
    """Read only producer-certified, dimensionless anomalies with causal times.

    This checks a producer contract. It cannot certify the underlying market
    source, which must be independently source- and clock-audited before use.
    """
    if not isinstance(raw, dict) or raw.get('status') != 'AVAILABLE':
        return None
    try:
        observed = instant(raw['observed_at'])
        available = instant(raw['available_at'])
        value = raw['value']
        if type(value) not in (float, int) or not math.isfinite(value):
            return None
        source, baseline = raw['source'], raw['baseline_id']
        sessions = raw['baseline_sessions']
        if (not isinstance(source, str) or not source.strip() or not isinstance(baseline, str)
                or not baseline.strip() or type(sessions) is not int
                or sessions < policy.min_baseline_sessions):
            return None
        if (observed.date() != now.date() or not observed <= available <= now
                or (now-observed).total_seconds() > policy.max_signal_age_seconds):
            return None
        return {'value': float(value), 'source': source, 'baseline_id': baseline,
                'baseline_sessions': sessions, 'observed_at': observed.isoformat(),
                'available_at': available.isoformat()}
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


class EventGate:
    """Serial, bounded trigger evaluator. Only the forecast worker calls it.

    A shock bypasses the 15-minute REVIEW cadence. A short independent shock
    cooldown only limits duplicates; it is NOT a 15-minute gate. Running the
    shock gate never requires calling the Gamma signal/entry code.
    """
    def __init__(self, policy: TriggerPolicy | None = None):
        self.policy = policy or TriggerPolicy()
        self._mutex = threading.Lock()
        self._seen_reviews: set[tuple[str, str]] = set()
        self._last_shock: datetime | None = None

    @staticmethod
    def _check_forecast(forecast_id: str, original_id: str, published_at: str,
                        event_time: datetime, next_target_at: str) -> datetime:
        if (not isinstance(forecast_id, str) or not forecast_id or
                not isinstance(original_id, str) or not original_id):
            raise EvidenceError('immutable forecast identity required')
        publication = instant(published_at)
        target=instant(next_target_at)
        if (publication >= event_time or target <= event_time or
                target.date() != event_time.date() or
                target.time()>time(15,30) or target.minute%5 or target.second or target.microsecond):
            raise EvidenceError('reforecast requires a genuine future five-minute target')
        if publication.date() != event_time.date():
            raise EvidenceError('prior forecast is from another session')
        return target

    def scheduled(self, *, now: str, target_at: str, actual_at: str,
                  actual_available_at: str, actual: float, projected: float,
                  forecast_id: str, original_id: str, published_at: str,
                  next_target_at: str) -> dict:
        """At a 15-min target, review actual vs the *active issued version*.

        Exactly 5 points is a miss. A missing, late, or mismatched actual is
        never silently converted to a zero error or a passing review.
        """
        clock, target, observed, available = map(instant, (
            now, target_at, actual_at, actual_available_at))
        if not self.policy.enabled:
            return {'state':'DISABLED'}
        if (not _session(clock) or target.minute % 15 != 0 or target.second
                or target.microsecond or target.date() != clock.date()):
            return {'state':'NOT_REVIEW_CHECKPOINT'}
        if observed != target or available < target or available > clock:
            return {'state':'MISSING_MATCHED_ACTUAL'}
        if not 0 <= (clock-target).total_seconds() <= self.policy.review_lag_seconds:
            return {'state':'LATE_OR_EARLY_REVIEW'}
        if (type(actual) not in (float, int) or type(projected) not in (float, int)
                or not math.isfinite(actual) or not math.isfinite(projected)
                or actual <= 0 or projected <= 0):
            raise EvidenceError('positive finite index values required')
        next_target=self._check_forecast(forecast_id, original_id, published_at, clock, next_target_at)
        if instant(published_at) >= target:
            raise EvidenceError('forecast was issued at or after scored target')
        key = (forecast_id, target.isoformat())
        with self._mutex:
            if key in self._seen_reviews:
                return {'state':'ALREADY_REVIEWED'}
            self._seen_reviews.add(key)
        deviation = float(actual)-float(projected)
        if abs(deviation) < 5:
            return {'state':'CONTINUE', 'abs_error':abs(deviation)}
        event = dict(kind='SCHEDULED_DEVIATION', trigger_at=clock.isoformat(),
                     matched_target_at=target.isoformat(), previous_forecast_id=forecast_id,
                     original_forecast_id=original_id, previous_published_at=published_at,
                     deviation_points=deviation, abs_error_points=abs(deviation),
                     evidence={'source':'MATCHED_ACTUAL', 'target_at':target.isoformat()},
                     next_target_at=next_target.isoformat(),
                     outcome='REQUESTED_NOT_PUBLISHED')
        return {'state':'REQUEST_REFORECAST', 'event':{'id':digest(event), **event}}

    def shock(self, *, now: str, signals: dict, forecast_id: str,
              original_id: str, published_at: str, next_target_at: str,
              confirmations: dict | None = None) -> dict:
        """Qualified shocks can trigger at any IST second, not quarter-hour.

        Supported producer signals: price_z, volume_ratio, breadth_z,
        oi_velocity_z, gamma_pressure_z. Gamma here is *observation evidence*,
        not a Gamma Blast entry/confirmation tier. At least two independent
        domains are needed unless independently verified consecutive ticks
        corroborate an extreme price shock.
        """
        clock=instant(now)
        if not self.policy.enabled:
            return {'state':'DISABLED'}
        if not _session(clock):
            return {'state':'OUTSIDE_SESSION'}
        next_target=self._check_forecast(forecast_id,original_id,published_at,clock,next_target_at)
        if not isinstance(signals,dict):
            raise EvidenceError('producer signals must be a mapping')
        allowed={'price_z','volume_ratio','breadth_z','oi_velocity_z','gamma_pressure_z'}
        if set(signals)-allowed:
            raise EvidenceError('unknown shock evidence')
        good={key:x for key,val in signals.items()
              if (x:=_valid_signal(val,clock,self.policy)) is not None}
        p=good.get('price_z',{}).get('value')
        v=good.get('volume_ratio',{}).get('value')
        b=good.get('breadth_z',{}).get('value')
        oi=good.get('oi_velocity_z',{}).get('value')
        gamma=good.get('gamma_pressure_z',{}).get('value')
        reasons=[]
        if p is not None and abs(p)>=self.policy.price_z:
            if v is not None and v>=self.policy.volume_ratio:
                reasons.append('PRICE_AND_VOLUME')
            if b is not None and abs(b)>=self.policy.breadth_z and p*b>0:
                reasons.append('PRICE_AND_WEIGHTED_BREADTH')
            if oi is not None and abs(oi)>=self.policy.oi_velocity_z:
                reasons.append('PRICE_AND_OI_VELOCITY')
            if gamma is not None and abs(gamma)>=self.policy.gamma_pressure_z:
                reasons.append('PRICE_AND_GAMMA_PRESSURE')
        if v is not None and v>=self.policy.extreme_volume_ratio and (
                (oi is not None and abs(oi)>=self.policy.oi_velocity_z) or
                (gamma is not None and abs(gamma)>=self.policy.gamma_pressure_z)):
            reasons.append('VOLUME_AND_OPTIONS_PRESSURE')
        proof=confirmations or {}
        if p is not None and abs(p)>=self.policy.extreme_price_z:
            n=proof.get('distinct_ticks',0)
            duration=proof.get('span_seconds',0)
            verified=proof.get('verified_exchange_sequence') is True
            if (type(n) is int and n>=self.policy.min_confirming_ticks and
                    type(duration) in (float,int) and math.isfinite(duration) and
                    duration>=self.policy.minimum_tick_span_seconds and verified):
                reasons.append('EXTREME_PRICE_CONFIRMED_TICKS')
        if not reasons:
            return {'state':'NO_CONFIRMED_SHOCK','valid_evidence':sorted(good)}
        with self._mutex:
            if (self._last_shock is not None and
                    (clock-self._last_shock).total_seconds()<self.policy.min_seconds_between_shocks):
                return {'state':'SHOCK_COOLDOWN', 'valid_evidence':sorted(good)}
            self._last_shock=clock
        event=dict(kind='MARKET_SHOCK', trigger_at=clock.isoformat(),
                   reasons=sorted(set(reasons)), previous_forecast_id=forecast_id,
                   original_forecast_id=original_id, previous_published_at=published_at,
                   deviation_points=None,abs_error_points=None,
                   evidence=good, next_target_at=next_target.isoformat(),
                   outcome='REQUESTED_NOT_PUBLISHED')
        return {'state':'REQUEST_REFORECAST', 'event':{'id':digest(event),**event}}


class EventJournal:
    """Separate append-only research DB. No production forecast or order DB."""
    def __init__(self, path):
        p=Path(path)
        if not p.name.endswith('.reforecast-events.sqlite3') or not p.parent.exists():
            raise EvidenceError('use a pre-existing directory and separate *.reforecast-events.sqlite3 DB')
        self.db=sqlite3.connect(p)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS triggers(id TEXT PRIMARY KEY,trigger_at TEXT NOT NULL,body TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS receipts(event_id TEXT PRIMARY KEY REFERENCES triggers(id),
            recorded_at TEXT NOT NULL,body TEXT NOT NULL);
          CREATE TRIGGER IF NOT EXISTS triggers_no_update BEFORE UPDATE ON triggers
            BEGIN SELECT RAISE(ABORT,'immutable'); END;
          CREATE TRIGGER IF NOT EXISTS triggers_no_delete BEFORE DELETE ON triggers
            BEGIN SELECT RAISE(ABORT,'immutable'); END;
          CREATE TRIGGER IF NOT EXISTS receipts_no_update BEFORE UPDATE ON receipts
            BEGIN SELECT RAISE(ABORT,'immutable'); END;
          CREATE TRIGGER IF NOT EXISTS receipts_no_delete BEFORE DELETE ON receipts
            BEGIN SELECT RAISE(ABORT,'immutable'); END;
        ''')

    def record(self, event: dict) -> bool:
        if not isinstance(event,dict) or event.get('id')!=digest({k:v for k,v in event.items() if k!='id'}):
            raise EvidenceError('event hash mismatch')
        stamp=instant(event['trigger_at'])
        old=self.db.execute('SELECT body FROM triggers WHERE id=?',(event['id'],)).fetchone()
        body=canonical(event)
        if old:
            if old[0]!=body: raise EvidenceError('conflicting event identity')
            return False
        with self.db:
            self.db.execute('INSERT INTO triggers VALUES (?,?,?)',(event['id'],stamp.isoformat(),body))
        return True

    def record_receipt(self, event_id: str, *, recorded_at: str,
                       outcome: str, publication_at: str | None=None,
                       revised_forecast_id: str | None=None,
                       note: str | None=None) -> bool:
        if outcome not in {'PUBLISHED','BLOCKED_DATA','NO_FITTED_MODEL','CANCELLED','SHADOW_ONLY'}:
            raise EvidenceError('unsupported publication outcome')
        stamp=instant(recorded_at)
        orig=self.db.execute('SELECT trigger_at,body FROM triggers WHERE id=?',(event_id,)).fetchone()
        if orig is None or stamp < instant(orig[0]):
            raise EvidenceError('receipt without earlier trigger')
        event=json.loads(orig[1])
        if outcome=='PUBLISHED':
            if not (publication_at and revised_forecast_id and revised_forecast_id!=event['previous_forecast_id']):
                raise EvidenceError('published revision identity required')
            publication=instant(publication_at)
            if not instant(orig[0])<=publication<=stamp or publication>=instant(event['next_target_at']):
                raise EvidenceError('publication must be after trigger, before receipt and before the next target')
        elif publication_at is not None or revised_forecast_id is not None:
            raise EvidenceError('unpublished events cannot claim revision')
        receipt=dict(event_id=event_id,recorded_at=stamp.isoformat(),outcome=outcome,
                     actual_publication_at=instant(publication_at).isoformat() if publication_at else None,
                     revised_forecast_id=revised_forecast_id,
                     note=(note or '')[:240])
        body=canonical(receipt)
        old=self.db.execute('SELECT body FROM receipts WHERE event_id=?',(event_id,)).fetchone()
        if old:
            if old[0]!=body: raise EvidenceError('immutable receipt conflict')
            return False
        with self.db:
            self.db.execute('INSERT INTO receipts VALUES (?,?,?)',(event_id,stamp.isoformat(),body))
        return True

    def recent(self, limit: int=20) -> list[dict]:
        if type(limit) is not int or not 1<=limit<=50:
            raise EvidenceError('bounded recent limit required')
        rows=self.db.execute('SELECT t.body,r.body FROM triggers t LEFT JOIN receipts r '
                             'ON r.event_id=t.id ORDER BY t.trigger_at DESC,t.id DESC LIMIT ?',
                             (limit,)).fetchall()
        return [dict(event=json.loads(t),receipt=json.loads(r) if r else None) for t,r in rows]

    def close(self):
        self.db.close()
