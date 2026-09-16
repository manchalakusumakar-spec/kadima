"""Bounded stock worker in the engine process, sharing its one AutoExecutor.

Historical reads occur outside the executor lock/exit-monitor thread. They
never synthesize candles from sparse REST quotes. Broker failures reject entry.
"""
import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from . import audit, config, indicators, storage, stock_handoff, trade_policy
from .option_chain import OptionChain, OptionQuote
from .session import SESSION
from .utils import now_ist, in_market_hours


def parse_candles(rows, interval, now):
    if not isinstance(rows, list) or len(rows) > 5000:
        raise ValueError('Invalid historical data')
    output = []
    previous = None
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            raise ValueError('Incomplete historical candle')
        ts = datetime.fromisoformat(row[0])
        if ts.tzinfo is None:
            raise ValueError('Candle timezone missing')
        ts = ts.astimezone(now.tzinfo)
        o, h, l, close, vol = map(float, row[1:6])
        if not trade_policy.positive(o,h,l,close) or not 0 <= vol < float('inf'):
            raise ValueError('Invalid candle values')
        if not l <= min(o, close) <= max(o, close) <= h or (previous and ts <= previous):
            raise ValueError('Invalid candle ordering/range')
        previous = ts
        if ts + timedelta(minutes=interval) > now:
            continue  # never use the forming candle
        output.append(dict(start_ts=ts.isoformat(),open=o,high=h,low=l,close=close,volume=int(vol),vwap=None))
    if not output:
        raise ValueError('No closed candles')
    age = (now - (datetime.fromisoformat(output[-1]['start_ts']) + timedelta(minutes=interval))).total_seconds()
    if not 0 <= age <= interval * 60 + 60:
        raise ValueError('Stale historical candles')
    today = [r for r in output if datetime.fromisoformat(r['start_ts']).date() == now.date()]
    if any((datetime.fromisoformat(b['start_ts'])-datetime.fromisoformat(a['start_ts'])).total_seconds()
           != interval * 60 for a,b in zip(today,today[1:])):
        raise ValueError('Missing session candles')
    return output[-300:]


class StockExecutionWorker:
    def __init__(self, executor, scrips):
        self.executor, self.scrips = executor, scrips
        self._stop = threading.Event()
        self._last_history = 0.0
        self._attempted = {}  # at most one latest snapshot per underlying
        self._cache = {}      # only most recently prepared underlying
        self.forecast_state = None

    def history(self, index):
        now = now_ist()
        cache = self._cache.get(index.name)
        if cache and 0 <= time.monotonic() - cache[0] <= 30:
            return cache[1]
        result = {}
        for interval, label in ((3,'THREE_MINUTE'), (5,'FIVE_MINUTE')):
            if self._stop.wait(max(0, 1.1 - (time.monotonic() - self._last_history))):
                raise ValueError('Worker stopping')
            self._last_history = time.monotonic()
            SESSION.ensure()
            response = SESSION.api.getCandleData(dict(exchange=index.exchange,symboltoken=index.token,
                interval=label,fromdate=(now-timedelta(days=7)).strftime('%Y-%m-%d 09:15'),
                todate=now.strftime('%Y-%m-%d %H:%M')))
            if not isinstance(response,dict) or response.get('status') is not True:
                raise ValueError('Historical request failed')
            bars = parse_candles(response.get('data'), interval, now)
            for bar in bars:
                storage.upsert_candle(index.token,interval,bar['start_ts'],bar['open'],bar['high'],
                    bar['low'],bar['close'],bar['volume'],None,True)
            result[interval] = bars
        self._cache = {index.name: (time.monotonic(),result)}
        return result

    def restore_chain(self, data):
        now = now_ist()
        if not all(stock_handoff.fresh(data.get(k), now=now) for k in ('chain_at','spot_at')):
            raise ValueError('Stale stock snapshot')
        index = config.Index(**data['index'])
        if index.name in config.GAMMA_INDICES:
            raise ValueError('Index is not a stock')
        width = data.get('ladder_width')
        if width is not None and (type(width) is not int or width != 1):
            raise ValueError('Unsupported stock ladder width')
        chain = OptionChain(index,self.scrips, strikes_each_side=width)
        chain.rebuild_ladder(data['spot'])
        expected = set(chain.quotes)
        quotes = [OptionQuote(**q) for q in data['quotes']]
        if not expected or {q.token for q in quotes} != expected or len(quotes) != len(expected):
            raise ValueError('Incomplete or obsolete stock ladder')
        for q in quotes:
            reference = chain.quotes[q.token]
            if (q.symbol,q.strike,q.opt_type) != (reference.symbol,reference.strike,reference.opt_type):
                raise ValueError('Contract metadata mismatch')
        chain.quotes = {q.token:q for q in quotes}
        clock = time.monotonic()
        chain._last_poll = clock - (now-datetime.fromisoformat(data['chain_at'])).total_seconds()
        chain._spot_received_at = clock - (now-datetime.fromisoformat(data['spot_at'])).total_seconds()
        return chain

    def process(self, row):
        data = row['data']
        if self._attempted.get(row['name']) == row['updated_at']:
            return
        self._attempted[row['name']] = row['updated_at']
        if len(self._attempted) > 300:
            self._attempted = {row['name']: row['updated_at']}
        if row['name'] != data['index']['name'] or not stock_handoff.fresh(row['updated_at']):
            raise ValueError('Signal identity or freshness mismatch')
        chain = self.restore_chain(data)
        bars = self.history(chain.index)
        if not trade_policy.positive(data.get('session_vwap')):
            raise ValueError('Broker session VWAP missing')
        snap = indicators.compute(bars[3],session_vwap=data['session_vwap'],
            ema_fast_p=config.EMA_FAST,ema_slow_p=config.EMA_SLOW,
            ma_long_p=config.MA_LONG,rsi_p=config.RSI_PERIOD)
        age = (now_ist() - (datetime.fromisoformat(bars[3][-1]['start_ts'])+timedelta(minutes=3))).total_seconds()
        chain._alignment_at = time.monotonic() - age
        if not stock_handoff.still_current(row['name'],row['signal_id'],row['updated_at']):
            return
        # Rechecked inside the serialized executor immediately before order submission.
        chain._entry_guard = lambda: stock_handoff.still_current(
            row['name'],row['signal_id'],row['updated_at'])
        from . import celestial
        if celestial.enabled():
            self.executor.prepare_celestial_context(row['name'],chain,data['spot'],snap,data['days_to_expiry'])
            return
        self.executor.evaluate_and_enter(row['name'],row['direction'],chain,data['spot'],snap,
            data['days_to_expiry'],'gamma_blast',row['signal_id'],'CONFIRMED',data['score'])

    def update_held(self):
        with self.executor._lock:
            held = [p for p in self.executor.state.positions.values()
                    if p.status == 'open' and p.index not in config.GAMMA_INDICES]
        if not held:
            return
        from .instrument_master import stock_universe
        from .auto_executor import _poll_ltp
        by_name = {s['name']:s for s in stock_universe()}
        for pos in held:
            stock = by_name.get(pos.index)
            if stock is None:
                continue
            step = self.scrips.infer_strike_step(pos.index,stock['opt_exchange'])
            if not step:
                continue
            index = config.Index(name=pos.index,exchange=stock['spot_exchange'],token=stock['spot_token'],
                opt_exchange=pos.exchange,opt_symbol_root=pos.index,strike_step=step,weight=0)
            prices = _poll_ltp(index.exchange,[index.token])
            spot = prices.get(index.token)
            spot_at = now_ist()
            if spot is None:
                continue
            bars = self.history(index)
            chain = OptionChain(index,self.scrips)
            chain.rebuild_ladder(spot)
            if chain.poll():
                self.executor.update_structure(pos.index,chain,spot)
                # One held-stock snapshot, published only after a complete poll.
                # No additional broker request; VWAP remains explicitly missing.
                snap = indicators.compute(bars[3],session_vwap=None)
                snap.vwap = None
                self.forecast_state = SimpleNamespace(name=pos.index,spot=spot,last_updated=spot_at,
                    is_live_tick=True,chain=chain,snap=snap,tier='HELD',score=0,
                    direction=pos.direction,gamma_result=None)

    def run(self):
        while not self._stop.is_set():
            if in_market_hours():
                try:
                    with self.executor._lock:
                        busy = bool(self.executor.state.positions)
                    if busy:
                        self.update_held()
                    elif not self.executor._reconcile_blocked:
                        for row in stock_handoff.candidates():
                            try:
                                self.process(row)
                            except Exception as exc:
                                audit.log('stock_entry_preparation_rejected',name=row['name'],error=type(exc).__name__)
                            if self.executor.state.positions:
                                break
                except Exception as exc:
                    audit.log('stock_execution_worker_error',error=type(exc).__name__)
            self._stop.wait(10)

    def start(self):
        threading.Thread(target=self.run,daemon=True,name='stock-entry-worker').start()

    def stop(self):
        self._stop.set()
