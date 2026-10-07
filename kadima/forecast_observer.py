"""Bounded, independent forecasting thread. Reads market state; never trades."""
from dataclasses import asdict
from datetime import datetime, timedelta
import json
from pathlib import Path
import threading
import time
import urllib.error
from . import config, storage, indicators
from .checklist_forecast import (Journal, fresh, make_report, number, read_json,
                                 request_forecast, timestamp, write_private)
from .utils import now_ist, in_market_hours

SETTINGS = Path('/etc/kadima/forecaster.json')
STATE = Path.home() / 'kadima-py/state'
REPORT = STATE / 'kadima_forecast_observations.json'
STATUS = STATE / 'kadima_forecast_status.json'
JOURNAL = STATE / 'forecast_journal.sqlite'


def read_report():
    result = {'mode': 'observation', 'status': 'not_started'}
    try:
        result = read_json(STATUS)
    except (OSError, ValueError):
        pass
    try:
        report = read_json(REPORT)
        result['latest'] = report
        result['forecast_expired'] = timestamp(report['expires_at']) < now_ist()
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return result


def clean(value, depth=0):
    """Bound only explicitly selected market data, never arbitrary environments."""
    if depth > 7:
        return None
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value[:600]
    if type(value) in (int, float):
        return value if number(value) else None
    if isinstance(value, dict):
        return {str(k)[:80]: clean(v, depth+1) for k,v in list(value.items())[:60]}
    if isinstance(value, (list, tuple)):
        return [clean(v, depth+1) for v in value[:60]]
    return None


def supplemental(now):
    context = {}
    availability = {
        'global_overnight_GIFT_US_Asia_DXY_FX_yields_oil': 'missing: no connected timestamped provider',
        'heavyweights_and_sector_breadth': 'missing: no weighted constituent feed',
        'IFI_and_central_bank_events': 'only supplied dated headlines/calendar; no dedicated IFI feed',
        'institutional_intraday_flows': 'missing: daily FII/DII is not intraday flow',
        'orderflow_icebergs_absorption_stop_clusters': 'missing: not observable from these feeds',
        'probability_calibration': 'unavailable: scores/heat are not win probabilities'}
    for name in ('macro_snapshot', 'universe_snapshot'):
        try:
            raw = read_json(STATE/(name+'.json'))
            if not isinstance(raw, dict):
                raise ValueError()
            if name == 'macro_snapshot':
                news = [dict(title=n.get('title'),source=n.get('source'),published_at=n.get('published_at'))
                        for n in (raw.get('news') or [])[:10] if isinstance(n,dict) and fresh(n.get('published_at'),now,86400)]
                vix = raw.get('india_vix') or {}
                context['macro'] = clean(dict(snapshot_at=raw.get('ts'),
                    india_vix=vix if fresh(vix.get('last_updated'),now,180) else None,
                    news=news, economic_calendar=raw.get('economic_calendar') if fresh(raw.get('ts'),now,3600) else None))
                availability['macro_news'] = 'dated supplied headlines only' if news else 'missing/stale (24h maximum)'
                availability['india_vix'] = 'available' if context['macro']['india_vix'] else 'missing/stale (180s maximum)'
            else:
                recent = fresh(raw.get('ts'),now,120)
                context['universe'] = clean(dict(snapshot_at=raw.get('ts'),coverage=raw.get('coverage'),
                    tracked=[r for r in (raw.get('tracked') or [])[:20] if isinstance(r,dict) and fresh(r.get('last_updated'),now,120)] if recent else []))
                availability['stock_gamma_shortlist'] = 'available subset; not full-universe forecasting' if context['universe']['tracked'] else 'missing/stale (120s maximum)'
        except (OSError, ValueError, TypeError, AttributeError):
            availability[name] = 'missing/invalid'
    return context, availability


def collect_asset(name, st, now, mono):
    """Requires a fresh actual underlying quote, and closed-candle technicals."""
    at = st.last_updated.isoformat() if st.last_updated else None
    if not st.is_live_tick or not number(st.spot, True) or not fresh(at,now,30):
        raise ValueError('underlying_missing_or_stale')
    chain = st.chain
    bars = storage.recent_candles(chain.index.token,3,limit=100)
    if not bars:
        raise ValueError('closed_candles_missing')
    closed_at = timestamp(bars[-1]['start_ts']) + timedelta(minutes=3)
    if not fresh(closed_at.isoformat(),now,240):
        raise ValueError('technicals_stale')
    # Never use a previous day's VWAP as today's confirmed alignment.
    vwap = st.snap.vwap if 0 <= mono-getattr(chain,'_alignment_at',0) <= 240 else None
    snap = indicators.compute(bars,session_vwap=vwap)
    sample = getattr(chain,'_forecast_sample',None)
    option_data = None
    if sample and 0 <= mono-sample['mono'] <= 30:
        option_data = dict(as_of=(now-timedelta(seconds=mono-sample['mono'])).isoformat(),
            expiry=str(sample['expiry']), quotes=sample['quotes'])
    # Greeks are often absent in raw quote responses. Derive them explicitly,
    # retaining the source quote timestamp and labeling the model assumption.
    if option_data:
        from .checklist_forecast import enrich_options
        option_data = enrich_options(option_data,st.spot,now,config.MARKET_CLOSE)
    asset = dict(name=name,spot=st.spot,spot_at=at,technicals_at=closed_at.isoformat(),
        technicals=asdict(snap),candles_3m=[{k:r[k] for k in ('start_ts','open','high','low','close','volume')} for r in bars[-30:]],
        shield=dict(tier=st.tier,score=st.score,direction=st.direction),
        gamma=asdict(st.gamma_result) if st.gamma_result else None,
        option_chain=option_data,
        unavailable=['OI velocity history not connected to this forecast snapshot', 'opening range unavailable before completed opening bars'])
    if option_data is None:
        asset['unavailable'].append('option chain missing/stale (30s maximum)')
    asset['unavailable'].extend('technical '+k+' unavailable' for k,v in asdict(snap).items() if v is None)
    return clean(asset), option_data


class ForecastObserver:
    def __init__(self, engine, executor):
        self.engine, self.executor = engine, executor
        self._stop = threading.Event()
        self._next = 0.0
        self.journal = None

    def start(self):
        threading.Thread(target=self.run,daemon=True,name='checklist-forecaster').start()

    def stop(self):
        self._stop.set()

    def samples(self):
        now = now_ist()
        result = {}
        for name,st in list(self.engine.states.items()):
            at = st.last_updated.isoformat() if st.last_updated else None
            if st.is_live_tick and number(st.spot,True) and fresh(at,now,30):
                result[name] = {'spot':st.spot,'at':at}
        stock = getattr(self.engine.stock_worker,'forecast_state',None)
        if stock:
            at = stock.last_updated.isoformat()
            if fresh(at,now,30):
                result[stock.name] = {'spot':stock.spot,'at':at}
        return result

    def collect(self, now):
        assets, excluded, contracts = [], {}, []
        context, availability = supplemental(now)
        radar = self.engine.constituent_radar.snapshot(now)
        context["nifty_constituent_radar"] = radar
        availability["nifty_constituent_radar"] = radar["status"]
        states = dict(self.engine.states)
        stock = getattr(self.engine.stock_worker,'forecast_state',None)
        if stock:
            states[stock.name] = stock
        # Copy only identities under the lock; no network/DB/model work there.
        with self.executor._lock:
            held = [dict(name=p.index,token=str(p.token),exchange=p.exchange,symbol=p.symbol)
                    for p in self.executor.state.positions.values() if p.status == 'open']
        self._held_symbols = [p['symbol'] for p in held]
        names = list(config.INDICES)
        for p in held:
            if p['name'] not in names:
                names.append(p['name'])
        for name in names[:5]:
            try:
                st = states[name]
                asset, option = collect_asset(name,st,now,time.monotonic())
                assets.append(asset)
                for p in held:
                    if p['name'] != name or option is None:
                        continue
                    # Exact current ladder identity, never a nearby replacement.
                    q = next((q for q in option['quotes'] if str(q['token'])==p['token'] and q['symbol']==p['symbol']),None)
                    if q is None:
                        continue
                    expiry = datetime.strptime(option['expiry'], '%d%b%Y').date()
                    close_hour, close_minute = config.MARKET_CLOSE
                    expiry_at = datetime.combine(expiry,datetime.min.time(),tzinfo=now.tzinfo).replace(hour=close_hour,minute=close_minute)
                    contracts.append(dict(p,spot=asset['spot'],spot_at=asset['spot_at'],
                        premium=q['ltp'],quote_at=option['as_of'],strike=q['strike'],opt_type=q['opt_type'],expiry_at=expiry_at.isoformat()))
            except (ValueError, KeyError, TypeError, AttributeError, RuntimeError):
                excluded[name] = 'missing/stale or inconsistent source inputs'
        availability['core_index_agreement'] = 'available only for included core indices: '+', '.join(a['name'] for a in assets if a['name'] in config.INDICES)
        return dict(as_of=now.isoformat(),assets=assets,context=context,availability=availability,excluded_assets=excluded),contracts

    def status(self, state):
        metrics = self.journal.metrics() if self.journal else {}
        write_private(STATUS,dict(mode='observation',status=state,checked_at=now_ist().isoformat(),metrics=metrics,
            execution_effect='none',cadence_seconds=300))

    def step(self):
        if self.journal is None:
            self.journal = Journal(JOURNAL)
        if time.monotonic() < self._next:
            return
        self._next = time.monotonic()+300  # bounded even after failure/missing key
        now = now_ist()
        try:
            settings = read_json(SETTINGS,16384)
        except FileNotFoundError:
            self.status('waiting_for_api_key')
            return
        if not isinstance(settings,dict) or settings.get('enabled') is not True:
            self.status('disabled')
            return
        if not settings.get('api_key'):
            self.status('waiting_for_api_key')
            return
        closing = now.replace(hour=config.MARKET_CLOSE[0],minute=config.MARKET_CLOSE[1],second=0,microsecond=0)
        if not in_market_hours() or now.weekday()>=5 or now+timedelta(hours=1)>closing:
            self.status('outside_one_hour_forecast_window')
            return
        inputs, contracts = self.collect(now)
        if not inputs['assets']:
            self.status('waiting_for_fresh_market_inputs')
            return
        if not self.journal.reserve(now,100):
            self.status('daily_request_limit_reached')
            return
        assets = request_forecast(inputs,settings)
        if self._stop.is_set():
            return
        report = make_report(inputs,assets,settings.get('model','gpt-5-mini'),now_ist(),contracts)
        report['held_contracts_without_scenario'] = [symbol for symbol in self._held_symbols if not any(s.get('symbol')==symbol and s['status']=='scenario_only' for s in report['premium_scenarios'])]
        self.journal.record(report,inputs)  # persist BEFORE publication/scoring
        write_private(REPORT,report)
        self.status('forecast_recorded')

    def sample_loop(self):
        while not self._stop.is_set():
            try:
                if self.journal:
                    self.journal.observe(self.samples())
            except Exception:
                pass  # missing outcomes remain visibly unscored
            self._stop.wait(5)

    def run(self):
        threading.Thread(target=self.sample_loop,daemon=True,name='forecast-outcomes').start()
        while not self._stop.is_set():
            try:
                self.step()
            except Exception as exc:
                # Never log remote response bodies, API keys, or model input.
                state = 'provider_http_'+str(exc.code) if isinstance(exc,urllib.error.HTTPError) else 'forecast_cycle_failed_'+type(exc).__name__
                try:
                    self.status(state)
                except Exception:
                    pass
            self._stop.wait(5)

