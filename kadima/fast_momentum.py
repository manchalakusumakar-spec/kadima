"""Experimental price-led index lane; observation by default, no broker calls.

All thresholds are testable hypotheses, not calibrated probabilities. No OI
direction is inferred from premium prices. Existing quote polls supply volume.
"""
from collections import deque
from copy import copy
from datetime import datetime, timedelta
import json
import math
import os
import statistics
import time

from . import celestial, storage, trade_policy
from .greeks import bs_price
from .utils import now_ist
from .config import IST, GAMMA_INDICES
from .index_event_gate import index_event_block


def live_requested():
    return os.environ.get('KADIMA_FAST_MOMENTUM_LIVE', '0') == '1'


def live_blocks(name, direction, now, score=0):
    cfg = celestial.read_config()
    reasons = []
    if not live_requested(): reasons.append('fast_observation_only')
    if not celestial.enabled(): reasons.append('rollout_disabled')
    if not cfg['auto_trade_enabled']: reasons.append('auto_paused')
    if cfg['kill_latched']: reasons.append('kill_latched')
    if not celestial.finite(score,cfg['score_threshold'],100): reasons.append('below_threshold')
    h = celestial.health('news') or {}
    impact = celestial.news_impact(name, direction, now)
    if name in GAMMA_INDICES:
        block = index_event_block(name, now)
        if block and block != 'news_risk_block': reasons.append(block)
    elif not h.get('ok') or not celestial.fresh(h.get('ts'), now, seconds=300) or not impact['news_available']:
        reasons.append('fresh_news_coverage_required')
    if impact['news_blocked']: reasons.append('news_risk_block')
    return reasons


def bars(rows, interval, now, minimum):
    """Closed, contiguous, same-session OHLC only. Never fill a missing bar."""
    out = []
    for source in rows:
        r = dict(source)
        t = datetime.fromisoformat(r['start_ts'])
        if t.date() != now.date(): continue
        if (t.tzinfo is None or r.get('closed', 1) not in (1, True)
                or not all(trade_policy.positive(r[k]) for k in ('open','high','low','close'))
                or not r['low'] <= min(r['open'],r['close']) <= max(r['open'],r['close']) <= r['high']
                or t + timedelta(minutes=interval) > now):
            raise ValueError('invalid_or_unclosed_bars')
        r['at'] = t
        out.append(r)
    if len(out) < minimum: raise ValueError('candle_warmup')
    if any((b['at']-a['at']).total_seconds() != interval*60 for a,b in zip(out,out[1:])):
        raise ValueError('candle_gap')
    if not 0 <= (now-out[-1]['at']).total_seconds()-interval*60 <= interval*60+15:
        raise ValueError('stale_candles')
    return out


def setup(rows1, rows5, spot, now):
    one = bars(rows1, 1, now, 7)[-7:]
    five = bars(rows5, 5, now, 3)[-3:]
    base, breakout, hold = one[:-2], one[-2], one[-1]
    high, low = max(r['high'] for r in base), min(r['low'] for r in base)
    width = statistics.mean(r['high']-r['low'] for r in base)
    if width <= 0: raise ValueError('flat_underlying')
    if breakout['close'] > high and hold['close'] > high and hold['low'] >= high-width*.25:
        direction, level, stop = 'CE', high, min(breakout['low'],hold['low'])
    elif breakout['close'] < low and hold['close'] < low and hold['high'] <= low+width*.25:
        direction, level, stop = 'PE', low, max(breakout['high'],hold['high'])
    else: raise ValueError('breakout_hold_not_confirmed')
    sign = 1 if direction == 'CE' else -1
    if not trade_policy.positive(spot) or not 0 < sign*(spot-level) <= width*1.5:
        raise ValueError('breakout_invalidated_or_extended')
    if sign*(five[-1]['close']-five[0]['close']) <= 0:
        raise ValueError('five_minute_context_opposes')
    if sign*(spot-stop) <= 0: raise ValueError('invalid_structure')
    return dict(direction=direction,spot_stop=stop,breakout_at=breakout['at'].isoformat(),
                bars=one,level=level)


class PremiumHistory:
    def __init__(self):
        self.windows = {}
        self.poll = None

    def record(self, chain, now):
        poll = chain._last_poll
        if poll == self.poll: return
        self.poll = poll
        retained = {}
        for q in chain.quotes.values():
            key = (chain._expiry,str(q.token),q.symbol)
            if not all(celestial.finite(v, .000001) for v in (q.ltp,q.bid,q.ask)) or q.ask < q.bid or not celestial.finite(q.volume):
                continue
            samples = self.windows.get(key, deque(maxlen=512))
            if samples and (not 0 < (now-samples[-1][0]).total_seconds() <= 90
                            or now.date()!=samples[-1][0].date() or q.volume<samples[-1][2]):
                samples.clear()
            samples.append((now,q.ltp,q.volume))
            while len(samples)>1 and (now-samples[1][0]).total_seconds()>=180:
                samples.popleft()
            retained[key] = samples
        self.windows = retained

    def metrics(self, chain, q, now):
        s = self.windows.get((chain._expiry,str(q.token),q.symbol), [])
        if not s or not 0 <= (now-s[-1][0]).total_seconds() <= 30: return None
        span = (s[-1][0]-s[0][0]).total_seconds()
        if not 180 <= span <= 270: return None
        mid = next((x for x in reversed(s) if (s[-1][0]-x[0]).total_seconds()>=60),None)
        if mid is None or mid[2]<=s[0][2] or mid[0]<=s[0][0]: return None
        recent = (s[-1][2]-mid[2])/(s[-1][0]-mid[0]).total_seconds()
        prior = (mid[2]-s[0][2])/(mid[0]-s[0][0]).total_seconds()
        return dict(premium_move_pct=100*(q.ltp/mid[1]-1),volume_ratio=recent/prior,
                    baseline_at=mid[0].isoformat())


def scenarios(q, spot, rows, days, now):
    """Fixed-IV short-horizon scenarios; ranges are NOT confidence intervals."""
    if not trade_policy.positive(q.iv, days): return []
    changes = [math.log(b['close']/a['close']) for a,b in zip(rows,rows[1:])]
    drift, sigma = statistics.mean(changes), statistics.pstdev(changes)
    close = now.replace(hour=15,minute=30,second=0,microsecond=0)
    baseline = bs_price(spot,q.strike,days,q.iv/100,q.opt_type)
    result = []
    for minutes in (5,10,15):
        target = now+timedelta(minutes=minutes)
        remaining = days-minutes/1440
        if target >= close or remaining <= 0: continue
        prices = [max(.01,q.ltp+bs_price(spot*math.exp(drift*minutes+z*sigma*math.sqrt(minutes)),
                   q.strike,remaining,q.iv/100,q.opt_type)-baseline) for z in (-1,0,1)]
        if not all(math.isfinite(p) for p in prices): return []
        result.append(dict(minutes=minutes,at=target.isoformat(),low=round(min(prices),2),
                           central=round(prices[1],2),high=round(max(prices),2)))
    return result


class Lane:
    def __init__(self):
        self.histories = {}
        self.rows = {}

    def scan(self, st, now=None, record=True):
        from . import strike_selector
        now = now or now_ist()
        row = dict(underlying=st.name,strategy='fast_momentum',ts=now.isoformat(),
                   stage='BLOCKED',blocked_reasons=[],direction=None,scenarios=[])
        try:
            chain = st.chain
            if not 0 <= time.monotonic()-chain._last_poll <= 30:
                raise ValueError('option_chain_stale')
            if not celestial.fresh(st.last_updated.isoformat() if st.last_updated else None,now):
                raise ValueError('underlying_quote_stale')
            history = self.histories.setdefault(st.name,PremiumHistory())
            if record: history.record(chain,now)
            info = setup(storage.recent_candles(chain.index.token,1,60),
                         storage.recent_candles(chain.index.token,5,30),st.spot,now)
            row.update({k:v for k,v in info.items() if k!='bars'})
            expiry = datetime.strptime(chain._expiry,'%d%b%Y').replace(tzinfo=IST,hour=15,minute=30)
            days = (expiry-now).total_seconds()/86400
            if days <= 0: raise ValueError('expired_contract')
            view = copy(chain)
            view.quotes = {k:copy(q) for k,q in chain.quotes.items() if q.tradeable
                           and all(celestial.finite(v,.000001) for v in (q.ltp,q.bid,q.ask))
                           and q.ask>=q.bid and (q.ask-q.bid)/q.ask<=.015}
            picks = [r for r in strike_selector.rank(view,info['direction'],st.spot,days) if not r.rejected]
            if not picks: raise ValueError('no_liquid_contract')
            q = picks[0].quote
            row.update(symbol=q.symbol,token=q.token,exchange=chain.index.opt_exchange,
                strike=q.strike,expiry=expiry.date().isoformat(),ltp=q.ltp,days=days,
                quote_at=(now-timedelta(seconds=time.monotonic()-chain._last_poll)).isoformat(),
                episode=st.name+':'+info['direction']+':'+info['breakout_at'])
            pace = history.metrics(chain,q,now)
            if pace is None: raise ValueError('premium_volume_warmup_3min')
            row.update(pace)
            if pace['premium_move_pct']<2 or pace['volume_ratio']<1.5:
                raise ValueError('premium_acceleration_unconfirmed')
            row['score'] = round(50+min(25,pace['premium_move_pct']/4*25)
                                 +min(25,(pace['volume_ratio']-1)/2*25),2)
            stop = trade_policy.premium_stop(st.spot,info['spot_stop'],q.strike,q.ltp,days,q.opt_type)
            entry = q.ask
            if not trade_policy.positive(stop) or stop>=entry: raise ValueError('invalid_premium_stop')
            risk = trade_policy.risk_per_unit(entry,stop)
            row.update(entry=entry,stop=stop,t1=round(entry+risk,2),t2=round(entry+2*risk,2),
                       scenarios=scenarios(q,st.spot,info['bars'],days,now),stage='DETECTED')
            if not row['scenarios']: raise ValueError('horizon_unavailable_before_close')
            central = row['scenarios'][-1]['central']
            net_gain = central*(1-trade_policy.EXIT_BUFFER)-entry*(1+trade_policy.COST_RESERVE)
            row['model_net_reward_risk'] = round(net_gain/risk,2)
            if net_gain/risk < 1.5: raise ValueError('scenario_net_reward_risk_below_1_5')
            row['stage'] = 'SETUP_READY'
            from . import config
            from .oi_confirmation import oi_confirmation, scalp_oi_block
            if config.OI_CONFIRM_MODE == 'live' and st.name in GAMMA_INDICES:
                confirmation = oi_confirmation(st.name, now)
                row['oi_confirmation'] = confirmation
                block = scalp_oi_block(row['direction'], now, confirmation)
                if block: row['blocked_reasons'].append(block)
        except (ValueError, TypeError, AttributeError, KeyError, OverflowError) as exc:
            reason = str(exc) if type(exc) is ValueError and str(exc) in {
                'invalid_or_unclosed_bars','candle_warmup','candle_gap','stale_candles',
                'flat_underlying','breakout_hold_not_confirmed','breakout_invalidated_or_extended',
                'five_minute_context_opposes','invalid_structure','option_chain_stale',
                'underlying_quote_stale','expired_contract','no_liquid_contract',
                'premium_volume_warmup_3min','premium_acceleration_unconfirmed','invalid_premium_stop',
                'horizon_unavailable_before_close','scenario_net_reward_risk_below_1_5'} else 'invalid_market_inputs'
            row['blocked_reasons'].append(reason)
        if row['direction']:
            row['blocked_reasons'].extend(live_blocks(st.name,row['direction'],now,row.get('score',0)))
        row['eligible'] = row['stage']=='SETUP_READY' and not row['blocked_reasons']
        if record: self.rows[st.name] = row
        return row


def signal_id(row):
    c = storage.conn()
    c.execute('CREATE TABLE IF NOT EXISTS fast_episodes (episode TEXT PRIMARY KEY, signal_id INTEGER NOT NULL)')
    with c:
        existing = c.execute('SELECT signal_id FROM fast_episodes WHERE episode=?',(row['episode'],)).fetchone()
        if existing: return existing[0]
        cur = c.execute('INSERT INTO signals (ts,index_name,tier,score,direction,payload,kind) VALUES (?,?,?,?,?,?,?)',
            (row['ts'],row['underlying'],'EXPERIMENTAL',row.get('score',0),row['direction'],json.dumps(row),'fast_momentum'))
        sid = cur.lastrowid
        c.execute('INSERT INTO fast_episodes VALUES (?,?)',(row['episode'],sid))
        return sid


def timeout_reason(pos, now):
    # Protection keeps working even if the entry feature is later disabled.
    if pos.kind != 'fast_momentum' or not pos.entry_ts or not pos.entry_price: return None
    if (now-pos.entry_ts).total_seconds()>=300 and (pos.peak_price or pos.entry_price)<pos.entry_price*1.05:
        return 'fast_momentum_no_progress_5min'
    return None
