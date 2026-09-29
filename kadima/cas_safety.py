"""Pure CAS data/price/risk checks. No broker, storage or activation calls.

FULL fields follow SmartAPI Live Market Data documentation. A recent HTTP
response is NOT proof of a recent exchange quote. Monetary reserves are
conservative modelling inputs, not guaranteed execution prices or tariffs.
"""
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_CEILING
import math
from zoneinfo import ZoneInfo

IST = ZoneInfo('Asia/Kolkata')
VERSION = 'cas-safety-v1'
QUOTE_MAX_AGE = 15.0
HISTORY_MAX_GAP = 45.0
MIN_TRIGGER_SPAN = 10.0
COST_RESERVE = .01
TICK = Decimal('0.1')  # Existing transport precision; use only compatible contracts.


def number(v, *, positive=False):
    return type(v) in (int, float) and math.isfinite(v) and (v > 0 if positive else v >= 0)


def numeric(v, *, positive=False):
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        return None
    try:
        n = float(v)
        return n if number(n, positive=positive) else None
    except (ValueError, OverflowError):
        return None


def integer(v, *, positive=False):
    n = numeric(v, positive=positive)
    return int(n) if n is not None and n.is_integer() else None


def stamp(value):
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else None
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo is not None else None
    except ValueError:
        try:
            # SmartAPI explicitly supplies exchange time in this IST format.
            return datetime.strptime(value, '%d-%b-%Y %H:%M:%S').replace(tzinfo=IST)
        except ValueError:
            return None


def fresh(value, now, max_age=QUOTE_MAX_AGE):
    dt = stamp(value)
    return (dt is not None and isinstance(now, datetime) and now.tzinfo is not None
            and dt.astimezone(IST).date() == now.astimezone(IST).date()
            and 0 <= (now-dt).total_seconds() <= max_age)


def grid(value, *, up=False):
    if not number(value, positive=True):
        raise ValueError('cas_invalid_price')
    return float((Decimal(str(value))/TICK).to_integral_value(
        rounding=ROUND_CEILING if up else ROUND_FLOOR)*TICK)


def compatible_tick(raw):
    """Angel index-option master expresses tick_size in paise, like strike."""
    n = numeric(raw, positive=True)
    if n is None:
        return False
    try:
        tick = Decimal(str(raw))/100
        return Decimal('0.01') <= tick <= TICK and TICK % tick == 0
    except InvalidOperation:
        return False


def parse_quote(response, exchange, token, now, *, for_exit=False):
    if not isinstance(response, dict) or response.get('status') is not True:
        return None
    data = response.get('data')
    rows = data.get('fetched') if isinstance(data, dict) else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        return None
    row = rows[0]
    if row.get('exchange') != exchange or str(row.get('symbolToken')) != str(token):
        return None
    if not fresh(row.get('exchFeedTime'), now):
        return None
    depth = row.get('depth')
    if not isinstance(depth, dict):
        return None
    def best(side):
        items = depth.get(side)
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            return None, None
        return numeric(items[0].get('price'), positive=True), integer(items[0].get('quantity'), positive=True)
    bid, bid_qty = best('buy')
    ask, ask_qty = best('sell')
    ltp = numeric(row.get('ltp'), positive=True)
    if bid is None or bid_qty is None:
        return None
    if not for_exit and (ltp is None or ask is None or ask_qty is None or ask < bid
                         or not fresh(row.get('exchTradeTime'), now)):
        return None
    if ask is not None and ask < bid:
        return None
    return dict(ltp=ltp, bid=bid, ask=ask, bid_qty=bid_qty, ask_qty=ask_qty,
                quote_at=stamp(row['exchFeedTime']).isoformat(),
                trade_at=stamp(row.get('exchTradeTime')).isoformat() if stamp(row.get('exchTradeTime')) else None,
                symbol=row.get('tradingSymbol'))


def entry_book_reason(q, now, lot_size, max_spread):
    if (not isinstance(q, dict) or type(lot_size) is not int or lot_size <= 0
            or not number(max_spread, positive=True)):
        return 'cas_quote_schema_invalid'
    if not fresh(q.get('quote_at'), now) or not fresh(q.get('trade_at'), now):
        return 'cas_quote_stale'
    if not all(number(q.get(k), positive=True) for k in ('ltp', 'bid', 'ask')) or q['ask'] < q['bid']:
        return 'invalid_or_missing_top_of_book'
    if any(type(q.get(k)) is not int or q[k] < lot_size for k in ('bid_qty', 'ask_qty')):
        return 'cas_depth_below_one_lot'
    if (q['ask']-q['bid'])/((q['ask']+q['bid'])/2)*100 > max_spread:
        return 'spread_too_wide'
    return None


def budget(funds, maximum, daily_limit, realized, buffer_pct):
    if (not all(number(x) for x in (funds, maximum, daily_limit, buffer_pct))
            or buffer_pct >= 100 or type(realized) not in (int, float) or not math.isfinite(realized)):
        return 0.0
    # No entry stop: reserve the whole premium plus costs, not a guessed stop loss.
    return max(0.0, min(maximum, funds*(1-buffer_pct/100), daily_limit+min(0.0, realized)))


def affordable(budget_inr, cap, lot, freeze, bid_qty, ask_qty):
    if (not number(budget_inr) or not number(cap, positive=True)
            or any(type(x) is not int or x <= 0 for x in (lot, freeze, bid_qty, ask_qty))):
        return 0, 0
    per_lot = Decimal(str(cap))*lot*(1+Decimal(str(COST_RESERVE)))
    lots = min(int(Decimal(str(budget_inr))//per_lot), freeze//lot, bid_qty//lot, ask_qty//lot)
    return max(0, lots), max(0, lots)*lot


def contract(chain, quote, now):
    """Exact active expiry-day index option, from the already-loaded master."""
    try:
        rows = [r for r in chain.scrips._options_for(chain.index)
                if str(r.get('token')) == quote.token and r.get('symbol') == quote.symbol]
        if len(rows) != 1:
            return None
        r = rows[0]
        expiry = datetime.strptime(r['expiry'], '%d%b%Y').date()
        lot = integer(r.get('lotsize'), positive=True)
        if (expiry != now.astimezone(IST).date() or r.get('instrumenttype') != 'OPTIDX'
                or r.get('exch_seg') != chain.index.opt_exchange or r.get('name') != chain.index.name
                or r.get('permitted_to_trade') is False or lot is None
                or not compatible_tick(r.get('tick_size'))
                or numeric(r.get('strike'), positive=True) != quote.strike*100
                or not quote.symbol.endswith(quote.opt_type)):
            return None
        return dict(expiry=expiry.isoformat(), lot_size=lot, tick_size=float(r['tick_size'])/100)
    except (AttributeError, ValueError, TypeError, KeyError):
        return None
