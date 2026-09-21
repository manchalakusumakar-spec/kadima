"""All active NFO stock roots, nearest expiry ATM +/- one strike, in FULL batches.

No top-mover promotion cutoff. Polling shares SESSION's cross-process budget.
One spot plus six option tokens per stock permits seven stocks per 50-token
request. This covers the specified entry ladder, not every listed expiry/strike.
Actual freshness and missing mappings remain observable; no coverage promise.
"""
import math
import threading
import time
from dataclasses import asdict
from datetime import datetime

from . import audit, celestial, gamma_blast_scanner as gamma, stock_handoff
from .celestial_scanner import Radar
from .celestial_universe import active_contracts
from .config import Index, IST, MARKET_CLOSE
from .option_chain import OptionChain
from .scrip_master import ScripMaster
from .session import SESSION
from .utils import in_market_hours, now_ist
from .universe_display import selected_contract, write_snapshot
from .stock_fast import Recorder as StockFastRecorder


def stock_roots(master, on):
    contracts = active_contracts(master.rows, on)
    # Stock-fast is an NSE/NFO stock-options strategy. BFO stock-option
    # listings are a separate venue/scope and must not be counted as unresolved
    # NFO coverage; doing so makes an otherwise healthy NSE F&O universe fail.
    roots = sorted({(r['exch_seg'], r['name']) for r in contracts.values()
                    if r['instrumenttype'] == 'OPTSTK' and r['exch_seg'] == 'NFO'})
    spots = {}
    for r in master.rows:
        if (r.get('exch_seg') == 'NSE' and r.get('instrumenttype', '') == ''
                and r.get('symbol', '').endswith('-EQ') and r.get('token')):
            spots.setdefault(r.get('name'), []).append(str(r['token']))
    resolved, missing = {}, []
    for exchange, name in roots:
        tokens = set(spots.get(name, []))
        step = master.infer_strike_step(name, exchange)
        if len(tokens) != 1 or not step:
            missing.append(dict(name=name, exchange=exchange, reason='spot_or_strike_mapping_unresolved'))
        else:
            resolved[name] = Index(name, 'NSE', next(iter(tokens)), exchange, name, step, 0)
    return resolved, missing


def fetch_batch(keys):
    """No retries, no token-only joins, no partial-success fiction."""
    keys = set(keys)
    if not keys or len(keys) > 50:
        raise ValueError('Quote batch must contain 1–50 exchange/token pairs')
    payload = {}
    for exchange, token in sorted(keys):
        payload.setdefault(exchange, []).append(token)
    SESSION.ensure()
    response = SESSION.api.getMarketData(mode='FULL', exchangeTokens=payload)
    if not isinstance(response, dict) or response.get('status') is not True:
        raise ValueError('Quote batch failed')
    rows = (response.get('data') or {}).get('fetched')
    if not isinstance(rows, list):
        raise ValueError('Quote batch schema invalid')
    found = {}
    for row in rows:
        key = row.get('exchange'), str(row.get('symbolToken'))
        if key not in keys or key in found:
            raise ValueError('Unrequested, duplicate or unidentified quote')
        price = float(row.get('ltp'))
        if not math.isfinite(price) or price <= 0:
            continue
        found[key] = dict(row, _received_at=now_ist().isoformat())
    return found


class FullUniverseScanner:
    def __init__(self, master=None):
        self.scrips = master or ScripMaster.load()
        self.indices, self.missing = stock_roots(self.scrips, now_ist().date())
        if len(self.indices) > 512:
            raise ValueError('Universe exceeds validated memory budget; refusing truncation')
        self.chains = {n: OptionChain(i, self.scrips, strikes_each_side=1) for n, i in self.indices.items()}
        self.histories = {n: gamma.GammaHistory() for n in self.indices}
        self.radar = Radar()
        self.fast_recorder = StockFastRecorder()
        self.last = {}
        self.scores = {}
        self.prices = {}
        self.gap_paper = {}
        self.day = now_ist().date()
        self._stop = threading.Event()
        self.report()

    def _fast_invalidate(self, name):
        try:
            self.fast_recorder.invalidate(name)
        except Exception as exc:
            audit.log('STOCK_FAST_RECORDER_ERROR', index=name, error_type=type(exc).__name__)

    def report(self, **extra):
        now = now_ist()
        fresh = sum(celestial.fresh(t, now, 90) for t in self.last.values())
        celestial.health('stock_universe', dict(
            expected=len(self.indices) + len(self.missing), priced=len(self.last),
            option_chains_tracked=len(self.chains), option_chain_limit=None,
            option_chains_fresh=fresh, missing=self.missing,
            ok=not self.missing and bool(self.chains) and fresh == len(self.chains),
            coverage='all_resolved_active_nfo_stock_roots_nearest_expiry_atm_plus_minus_one', **extra))
        try:
            from . import gap_and_go
            if gap_and_go.paper_prefilter_enabled():
                rows = list(self.gap_paper.values())
                celestial.health('gap_and_go_paper', dict(
                    ok=True, mode='PAPER_PREFILTER', live_execution=False,
                    observed=len(rows),
                    gap_detected=sum(r.get('stage') == 'GAP_DETECTED' for r in rows),
                    scan_window='09:15-10:30_IST',
                    source='existing_full_universe_FULL_quotes'))
        except Exception as exc:
            audit.log('GAP_PAPER_HEALTH_ERROR', error_type=type(exc).__name__)
        try:
            write_snapshot(dict(ts=now.isoformat(),tracked=sorted(self.scores.values(),key=lambda r:r['score'],reverse=True),
                prices=self.prices,execution='central_executor_gated',coverage=dict(
                    expected=len(self.indices)+len(self.missing),priced=len(self.prices),
                    missing=sorted({r['name'] for r in self.missing}|(set(self.indices)-set(self.prices))),
                    gamma_limit=None)))
        except (OSError,ValueError,TypeError) as exc:
            audit.log('universe_snapshot_write_failed',error=type(exc).__name__)

    def poll_group(self, names):
        keys = set()
        for name in names:
            chain = self.chains[name]
            keys.add((chain.index.exchange, chain.index.token))
            keys.update((chain.index.opt_exchange, t) for t in chain.quotes)
        rows = fetch_batch(keys)
        # On startup only spot tokens are known. Establish the actual ladders;
        # subsequent batches collect both sides and the underlying together.
        for name in names:
            chain = self.chains[name]
            producer = chain.index.opt_exchange + ':' + name
            self.scores.pop(name,None)
            spot_row = rows.get((chain.index.exchange, chain.index.token))
            if not spot_row:
                celestial.publish(producer, [])
                stock_handoff.invalidate(name)
                self._fast_invalidate(name)
                self.gap_paper.pop(name, None)
                continue
            spot = float(spot_row['ltp'])
            self.prices[name]=dict(ltp=spot,received_at=spot_row['_received_at'])
            # Default-OFF and paper-only. The prefilter never calls an executor;
            # it only publishes a cheap opening-gap observation from this same
            # FULL quote, avoiding a second universe-wide broker request.
            try:
                from . import gap_and_go
                if gap_and_go.paper_prefilter_enabled():
                    self.gap_paper[name] = gap_and_go.observe_full_quote(
                        name, spot_row, now_ist())
            except Exception as exc:
                audit.log('GAP_PAPER_PREFILTER_ERROR', index=name,
                          error_type=type(exc).__name__)
            previous = set(chain.quotes)
            chain.rebuild_ladder(spot)
            fetched = [rows[(chain.index.opt_exchange, t)] for t in chain.quotes
                       if (chain.index.opt_exchange, t) in rows]
            if (previous != set(chain.quotes) or len(chain.quotes) != 6
                    or not chain.accept_full_sample(fetched)):
                celestial.publish(producer, [])
                stock_handoff.invalidate(name)
                self._fast_invalidate(name)
                continue
            expiry = datetime.strptime(chain._expiry, '%d%b%Y').replace(
                tzinfo=IST, hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1])
            days = (expiry - now_ist()).total_seconds() / 86400
            if days <= 0:
                celestial.publish(producer, [])
                stock_handoff.invalidate(name)
                self._fast_invalidate(name)
                continue
            stamp = spot_row['_received_at']
            vwap = float(spot_row.get('avgPrice') or 0)
            vwap = vwap if math.isfinite(vwap) and vwap > 0 else None
            result = gamma.score(name, chain, self.histories[name], spot, days)
            vel = chain.velocity.alerts()
            ce_tokens = {q.token for q in chain.quotes.values() if q.opt_type == 'CE'}
            pe_tokens = {q.token for q in chain.quotes.values() if q.opt_type == 'PE'}
            ce_hits = len({v['token'] for v in vel if v['token'] in ce_tokens})
            pe_hits = len({v['token'] for v in vel if v['token'] in pe_tokens})
            oi_direction = 'CE' if ce_hits > pe_hits else 'PE' if pe_hits > ce_hits else None
            # The stock-fast producer remains independent for detection; Gamma/OI
            # metadata is attached only so Gap live confirmation can fail closed
            # without a second option-chain poll.
            try:
                self.fast_recorder.publish(chain, spot, stamp, vwap,
                    spot_context=dict(
                        day_open=float(spot_row.get('open') or 0),
                        previous_close=float(spot_row.get('close') or 0),
                        cumulative_volume=float(spot_row.get('tradeVolume') or 0)),
                    gamma=dict(score=result.score, tier=result.tier,
                               direction=result.direction, oi_direction=oi_direction))
            except Exception as exc:
                self._fast_invalidate(name)
                audit.log('STOCK_FAST_RECORDER_ERROR', index=name, error_type=type(exc).__name__)
            contract,status=selected_contract(chain,result.direction,spot,days,stamp)
            self.scores[name]=dict(name=name,spot=spot,tier=result.tier,score=result.score,
                direction=result.direction,last_updated=stamp,best_contract=contract,strike_status=status)
            self.last[name] = stamp
            candidates = self.radar.publish_chain(chain, spot, stamp, result, vwap=vwap)
            if candidates:
                stock_handoff.publish(name, result.direction, result.tier, result.score, dict(
                    index=asdict(chain.index), spot=spot, spot_at=stamp, session_vwap=vwap,
                    chain_at=stamp, days_to_expiry=days, score=result.score, ladder_width=1,
                    quotes=[asdict(q) for q in chain.quotes.values()]))
            else:
                stock_handoff.invalidate(name)

    def cycle(self):
        started = time.monotonic()
        names = sorted(self.chains)
        for i in range(0, len(names), 7):
            if self._stop.is_set():
                break
            group = names[i:i + 7]
            try:
                self.poll_group(group)
            except Exception as exc:
                for name in group:
                    self.scores.pop(name,None)
                    celestial.publish(self.indices[name].opt_exchange + ':' + name, [])
                    stock_handoff.invalidate(name)
                    self._fast_invalidate(name)
                audit.log('full_universe_batch_failed', error=type(exc).__name__, count=len(group))
            self.report(cycle_running=True)
        self.report(cycle_seconds=round(time.monotonic() - started, 2), cycle_running=False)

    def run_forever(self):
        from .news_collector import NewsCollector
        self.news = NewsCollector(self.indices)
        self.news.start()
        while not self._stop.is_set():
            if now_ist().date() != self.day:
                # Refresh master/mappings every trading day, retaining no old tokens.
                replacement = FullUniverseScanner()
                self.__dict__.update({k:v for k,v in replacement.__dict__.items() if k != '_stop'})
                self.news.stop()
                self.news = NewsCollector(self.indices)
                self.news.start()
            if in_market_hours():
                started = time.monotonic()
                self.cycle()
                self._stop.wait(max(0.1, 30 - (time.monotonic() - started)))
            else:
                self._stop.wait(5)

    def stop(self):
        self._stop.set()
        if hasattr(self, 'news'):
            self.news.stop()
