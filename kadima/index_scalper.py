"""Two-lot index option scalper. Detection is local; execution is opt-in.

Ten points means premium movement from the broker-confirmed average entry.
The exit trigger is not a promised fill price or a net-profit guarantee.
"""
from copy import copy
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_FLOOR
import json
import os
import time

from . import celestial, config, fast_momentum, storage, trade_policy
from .instrument_master import get_lot_size
from .utils import now_ist

KIND = 'index_scalp'
LOTS = 2
TARGET_POINTS = 10.0
MAX_ENTRY_CHASE_POINTS = 0.5
MAX_HOLD_SECONDS = 15 * 60


def live_requested():
    return os.environ.get('KADIMA_INDEX_SCALP_LIVE', '0') == '1'


def live_blocks(name, direction, now, score=0):
    # Share rollout, auto-pause, kill, score and news checks, but do not
    # couple this lane's opt-in to the separate fast-momentum opt-in.
    reasons = [r for r in fast_momentum.live_blocks(name, direction, now, score)
               if r != 'fast_observation_only']
    if not live_requested():
        reasons.insert(0, 'index_scalp_observation_only')
    if name not in getattr(config, 'GAMMA_INDICES', {}):
        reasons.append('index_contract_required')
    return reasons


def entry_limit(ltp, detected_ask):
    if not trade_policy.positive(ltp, detected_ask):
        return None
    cap = Decimal(str(detected_ask)) + Decimal(str(MAX_ENTRY_CHASE_POINTS))
    cap = (cap / Decimal('0.05')).to_integral_value(rounding=ROUND_FLOOR) * Decimal('0.05')
    return float(cap) if Decimal(str(ltp)) <= cap else None


def plan(entry, stop, lot_size):
    if (not trade_policy.positive(entry, stop) or stop >= entry
            or type(lot_size) is not int or lot_size <= 0):
        raise ValueError('invalid_scalp_plan')
    target = float(Decimal(str(entry)) + Decimal('10'))
    net = (target * (1 - trade_policy.EXIT_BUFFER)
           - entry * (1 + trade_policy.COST_RESERVE))
    if net <= 0:
        raise ValueError('scalp_target_consumed_by_cost_reserve')
    return dict(lots=LOTS, lot_size=lot_size, quantity=LOTS * lot_size,
                target_points=TARGET_POINTS, target=target,
                gross_target_inr=TARGET_POINTS * LOTS * lot_size,
                estimated_net_target_inr=round(net * LOTS * lot_size, 2),
                max_hold_seconds=MAX_HOLD_SECONDS)


def exit_reason(pos, ltp, now):
    # Restored positions keep protection even if new entries are paused.
    if (pos.kind != KIND or not trade_policy.positive(pos.entry_price, ltp)
            or pos.remaining_qty <= 0):
        return None
    if Decimal(str(ltp)) >= Decimal(str(pos.entry_price)) + Decimal('10'):
        return 'index_scalp_target_10_points'
    if (pos.entry_ts is not None and pos.entry_ts.tzinfo is not None
            and (now - pos.entry_ts).total_seconds() >= MAX_HOLD_SECONDS):
        return 'index_scalp_timeout_15min'
    return None


class Lane:
    def __init__(self):
        self.histories = {}
        self.rows = {}

    def scan(self, st, now=None, record=True):
        from . import strike_selector
        now = now or now_ist()
        row = dict(underlying=st.name, strategy=KIND, ts=now.isoformat(),
                   stage='BLOCKED', eligible=False, blocked_reasons=[],
                   direction=None, lots=LOTS, target_points=TARGET_POINTS)
        try:
            if st.name not in getattr(config, 'GAMMA_INDICES', {}):
                raise ValueError('index_contract_required')
            # A full holding window must fit inside the configured forced exit.
            from .auto_executor import EOD_FORCE_CLOSE_HHMM
            close = now.replace(hour=EOD_FORCE_CLOSE_HHMM[0], minute=EOD_FORCE_CLOSE_HHMM[1],
                                second=0, microsecond=0)
            if now + timedelta(seconds=MAX_HOLD_SECONDS) >= close:
                raise ValueError('scalp_entry_window_closed')
            chain = st.chain
            clock = time.monotonic()
            if not 0 <= clock - chain._last_poll <= 30:
                raise ValueError('option_chain_stale')
            if not celestial.fresh(st.last_updated.isoformat() if st.last_updated else None, now):
                raise ValueError('underlying_quote_stale')
            if not 0 <= clock - getattr(chain, '_alignment_at', 0) <= 240:
                raise ValueError('alignment_snapshot_stale')
            history = self.histories.setdefault(st.name, fast_momentum.PremiumHistory())
            if record:
                history.record(chain, now)
            info = fast_momentum.setup(storage.recent_candles(chain.index.token, 1, 60),
                                       storage.recent_candles(chain.index.token, 5, 30), st.spot, now)
            row.update({k: v for k, v in info.items() if k != 'bars'})
            direction = info['direction']
            if not trade_policy.eligible('gamma_blast', 'CONFIRMED', 100, direction, st.snap, st.spot):
                raise ValueError('vwap_ema_rsi_not_aligned')
            expiry = datetime.strptime(chain._expiry, '%d%b%Y').replace(
                tzinfo=config.IST, hour=15, minute=30)
            days = (expiry - now).total_seconds() / 86400
            if days <= 0:
                raise ValueError('expired_contract')
            view = copy(chain)
            sign = 1 if direction == 'CE' else -1
            # ATM or one step ITM, resolved from this chain's live master.
            view.quotes = {k: copy(q) for k, q in chain.quotes.items()
                           if q.opt_type == direction and q.tradeable
                           and trade_policy.positive(q.ltp, q.bid, q.ask)
                           and q.ask >= q.bid
                           and 0 <= sign * (chain.atm - q.strike) <= chain.index.strike_step}
            picks = [p for p in strike_selector.rank(view, direction, st.spot, days) if not p.rejected]
            if not picks:
                raise ValueError('no_liquid_atm_or_itm_contract')
            q = picks[0].quote
            pace = history.metrics(chain, q, now)
            if pace is None:
                raise ValueError('premium_volume_warmup_3min')
            if pace['premium_move_pct'] < 2 or pace['volume_ratio'] < 1.5:
                raise ValueError('premium_acceleration_unconfirmed')
            stop = trade_policy.premium_stop(st.spot, info['spot_stop'], q.strike, q.ltp, days, direction)
            details = plan(q.ask, stop, get_lot_size(q.symbol, q.token))
            score = round(50 + min(25, pace['premium_move_pct'] / 4 * 25)
                          + min(25, (pace['volume_ratio'] - 1) / 2 * 25), 2)
            row.update(details, **pace, score=score, symbol=q.symbol, token=q.token,
                       exchange=chain.index.opt_exchange, strike=q.strike, expiry=expiry.date().isoformat(),
                       ltp=q.ltp, entry=q.ask, stop=stop, days=days,
                       quote_at=(now - timedelta(seconds=clock - chain._last_poll)).isoformat(),
                       episode=':'.join((KIND, st.name, direction, chain._expiry, info['breakout_at'])),
                       stage='SETUP_READY')
        except (ValueError, TypeError, AttributeError, KeyError, OverflowError) as exc:
            # Internal exceptions do not expose raw input or secrets in status.
            known = {'index_contract_required', 'scalp_entry_window_closed', 'option_chain_stale',
                     'underlying_quote_stale', 'alignment_snapshot_stale', 'vwap_ema_rsi_not_aligned',
                     'expired_contract', 'no_liquid_atm_or_itm_contract', 'premium_volume_warmup_3min',
                     'premium_acceleration_unconfirmed', 'invalid_scalp_plan',
                     'scalp_target_consumed_by_cost_reserve', 'invalid_or_unclosed_bars', 'candle_warmup',
                     'candle_gap', 'stale_candles', 'flat_underlying', 'breakout_hold_not_confirmed',
                     'breakout_invalidated_or_extended', 'five_minute_context_opposes', 'invalid_structure'}
            row['blocked_reasons'].append(str(exc) if type(exc) is ValueError and str(exc) in known
                                          else 'invalid_market_inputs')
        row['blocked_reasons'].extend(live_blocks(st.name, row['direction'], now, row.get('score', 0)))
        row['eligible'] = row['stage'] == 'SETUP_READY' and not row['blocked_reasons']
        if record:
            self.rows[st.name] = row
        return row


def signal_id(row):
    c = storage.conn()
    c.execute('CREATE TABLE IF NOT EXISTS index_scalp_episodes '
              '(episode TEXT PRIMARY KEY, signal_id INTEGER NOT NULL)')
    with c:
        previous = c.execute('SELECT signal_id FROM index_scalp_episodes WHERE episode=?',
                             (row['episode'],)).fetchone()
        if previous:
            return previous[0]
        cur = c.execute('INSERT INTO signals (ts,index_name,tier,score,direction,payload,kind) '
                        'VALUES (?,?,?,?,?,?,?)', (row['ts'], row['underlying'], 'EXPERIMENTAL',
                         row.get('score', 0), row['direction'], json.dumps(row), KIND))
        c.execute('INSERT INTO index_scalp_episodes VALUES (?,?)', (row['episode'], cur.lastrowid))
        return cur.lastrowid


def status(rows):
    return dict(mode='live_requested' if live_requested() else 'observation',
                lots=LOTS, target_points=TARGET_POINTS, target_basis='actual_average_fill_premium',
                max_hold_seconds=MAX_HOLD_SECONDS, candidates=list(rows.values())[:20],
                note='10-point exit trigger before fees/slippage; fills and profits are not guaranteed.')
