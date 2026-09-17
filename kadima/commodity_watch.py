"""Read-only MCX futures/options monitoring, isolated from the order executor."""
from datetime import datetime, time
import json
import math
from pathlib import Path

from .config import IST
from .celestial_scanner import PremiumWindow
from .utils import now_ist

ROOTS = ('CRUDEOIL', 'GOLD', 'NATURALGAS', 'SILVER')
SNAPSHOT_PATH = Path.home() / 'kadima-py/state/macro_snapshot.json'


def monitoring_window(now):
    """User's 23:30 IST cap; this is not an exchange holiday calendar."""
    now = now.astimezone(IST)
    return now.weekday() < 5 and time(9) <= now.time() < time(23, 30)


def number(value):
    try:
        if isinstance(value, bool):
            return None
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def exchange_time(value):
    try:
        result = datetime.strptime(value, '%d-%b-%Y %H:%M:%S').replace(tzinfo=IST)
        return result.isoformat() if result.year >= 2000 else None
    except (TypeError, ValueError):
        return None


def recent(stamp, now):
    try:
        parsed = datetime.fromisoformat(stamp)
        return parsed.tzinfo is not None and 0 <= (now - parsed).total_seconds() <= 90
    except (TypeError, ValueError, OverflowError):
        return False


def catalog(rows, today):
    out = []
    for row in rows:
        if row.get('exch_seg') != 'MCX' or row.get('name') not in ROOTS \
                or row.get('instrumenttype') not in ('FUTCOM', 'OPTFUT') \
                or row.get('permitted_to_trade') is False:
            continue
        try:
            expiry = datetime.strptime(row['expiry'], '%d%b%Y').date()
            token, symbol = str(row['token']), str(row['symbol'])
            if expiry < today or not token.isdigit() or not symbol:
                continue
            kind = row['instrumenttype']
            side = symbol[-2:] if kind == 'OPTFUT' else None
            strike = number(row.get('strike'))
            if kind == 'OPTFUT' and (side not in ('CE', 'PE') or strike is None or strike <= 0):
                continue
            out.append(dict(name=row['name'], instrumenttype=kind, symbol=symbol[:80],
                            token=token, exchange='MCX', expiry=expiry.isoformat(),
                            type=side, strike=strike / 100 if kind == 'OPTFUT' else None,
                            broker_lot_size=number(row.get('lotsize'))))
        except (KeyError, ValueError, TypeError):
            continue
    return out


def select_futures(contracts):
    return [min(group, key=lambda row: (row['expiry'], row['symbol'])) for name in ROOTS
            if (group := [row for row in contracts if row['name'] == name
                          and row['instrumenttype'] == 'FUTCOM'])]


def select_options(contracts, futures):
    """Display reference only; no claim that option/future expiries are mapped."""
    out = []
    for future in futures:
        if not future.get('fresh'):
            continue
        group = [row for row in contracts if row['name'] == future['name']
                 and row['instrumenttype'] == 'OPTFUT']
        if not group:
            continue
        expiry = min(row['expiry'] for row in group)
        for side in ('CE', 'PE'):
            choices = [row for row in group if row['expiry'] == expiry and row['type'] == side]
            if choices:
                out.append(min(choices, key=lambda row: (abs(row['strike'] - future['ltp']),
                                                        row['strike'], row['symbol'])))
    return out


def quote_rows(contracts, response, now):
    if not isinstance(response, dict) or response.get('status') is not True:
        raise ValueError('quote_request_failed')
    data = response.get('data')
    fetched = data.get('fetched') if isinstance(data, dict) else None
    if not isinstance(fetched, list):
        raise ValueError('quote_response_invalid')
    by_token = {str(row.get('symbolToken')): row for row in fetched
                if isinstance(row, dict) and row.get('exchange') == 'MCX'}
    out = []
    for contract in contracts:
        quote = by_token.get(contract['token'], {})
        ltp = number(quote.get('ltp'))
        feed_at, trade_at = exchange_time(quote.get('exchFeedTime')), exchange_time(quote.get('exchTradeTime'))
        depth = quote.get('depth')
        depth = depth if isinstance(depth, dict) else {}
        def best(side):
            levels = depth.get(side)
            return number(levels[0].get('price')) if isinstance(levels, list) and levels \
                and isinstance(levels[0], dict) else None
        bid, ask = best('buy'), best('sell')
        clean = bid is not None and ask is not None and 0 < bid <= ask
        out.append(dict(contract, ltp=ltp if ltp is not None and ltp > 0 else None,
                        volume=number(quote.get('tradeVolume')), oi=number(quote.get('opnInterest')),
                        bid=bid, ask=ask,
                        spread_pct=100 * (ask - bid) / ((ask + bid) / 2) if clean else None,
                        exchange_feed_at=feed_at, exchange_trade_at=trade_at,
                        received_at=now.isoformat() if quote else None,
                        fresh=bool(ltp is not None and ltp > 0 and recent(feed_at, now)
                                   and recent(trade_at, now))))
    return out


class CommodityWatch:
    def __init__(self):
        self.rows = []
        self.checked_at = None
        self.ok = False
        self.error = None
        self.windows = {}

    def poll(self, master_rows, fetch, now=None):
        now = now or now_ist()
        if not monitoring_window(now):
            return
        self.checked_at = now.isoformat()
        self.ok = False
        self.error = None
        try:
            contracts = catalog(master_rows, now.date())
            futures = select_futures(contracts)
            if not futures:
                raise ValueError('contracts_unavailable')
            rows = quote_rows(futures, fetch([row['token'] for row in futures]), now)
            options = select_options(contracts, rows)
            if options:
                rows += quote_rows(options, fetch([row['token'] for row in options]), now)
            retained = {}
            for row in rows:
                if row['instrumenttype'] != 'OPTFUT':
                    continue
                key = row['exchange'], row['token'], row['symbol'], row['expiry']
                window = self.windows.get(key, PremiumWindow())
                valid = row['fresh'] and all(number(row.get(field)) is not None and row[field] >= 0
                                           for field in ('volume', 'oi'))
                if valid:
                    metrics = window.add(now, row['ltp'], row['volume'], row['oi'])
                    retained[key] = window
                else:
                    metrics = None
                    window.samples.clear()
                row['gamma_watch'] = dict(status='warming_up' if valid else 'data_unavailable',
                                         eligible_for_execution=False, gamma_score=None)
                if metrics:
                    spread = row['spread_pct']
                    candidate = (metrics['premium_move_pct'] >= 5 and metrics['volume_spike']
                                 and metrics['oi_change'] != 0 and row['oi'] > 0
                                 and spread is not None and spread <= 1.5)
                    row['gamma_watch'].update(metrics, status='premium_blast_watch' if candidate else 'watching',
                                              oi_activity=metrics['oi_change'] != 0,
                                              directional_oi_confirmed=False)
            self.windows = retained
            self.rows = rows
            self.ok = True
        except Exception as exc:
            # Preserve old observations with their old source times, never credentials.
            self.error = type(exc).__name__
            self.windows.clear()

    def snapshot(self, now=None):
        now = now or now_ist()
        window = monitoring_window(now)
        rows = [dict(row, fresh=bool(row.get('fresh') is True and window and self.ok and recent(self.checked_at, now)
                                    and recent(row.get('exchange_feed_at'), now)
                                    and recent(row.get('exchange_trade_at'), now))) for row in self.rows]
        for row in rows:
            if not row['fresh'] and row.get('gamma_watch'):
                row['gamma_watch'] = dict(row['gamma_watch'], status='stale')
        return dict(status=('outside_monitoring_window' if not window else
                            'unavailable' if not self.ok else
                            'observing' if rows and all(row['fresh'] for row in rows) else 'partial'),
                    checked_at=self.checked_at, monitoring_window_open=window,
                    monitor_until='23:30 Asia/Kolkata', execution_enabled=False,
                    context_monitoring='24/7',
                    roots=list(ROOTS), contracts=rows, error_type=self.error,
                    gamma_note='MCX Gamma Blast watch screens measured five-minute premium gain >=5%, '
                               'volume rate >=1.5x, changing OI and spread <=1.5%. '
                               'It is an activity watchlist, not a computed option gamma or confirmed trade. '
                               'Directional chain OI, futures-option mapping and execution remain unverified.',
                    note='Futures and option quote references only. Option strikes are nearest '
                         'to the displayed future, not verified underlying-expiry mappings. '
                         'Exchange holidays, order entitlement, margin and P&L units are not verified. '
                         'No MCX orders are submitted by this monitor.')


def read_snapshot(path=None, now=None):
    """Expose MCX observations through FETCH without making broker requests."""
    now = now or now_ist()
    unavailable = dict(status='unavailable', contracts=[], execution_enabled=False,
                       context_monitoring='24/7', monitor_until='23:30 Asia/Kolkata')
    try:
        with Path(path or SNAPSHOT_PATH).open('rb') as handle:
            raw = handle.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            return unavailable
        data = json.loads(raw)
        data = data.get('commodities') if isinstance(data, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get('contracts'), list):
            return unavailable
        monitor = CommodityWatch()
        monitor.checked_at = data.get('checked_at')
        monitor.ok = data.get('status') in ('observing', 'partial')
        monitor.rows = [row for row in data['contracts'][:12] if isinstance(row, dict)]
        result = monitor.snapshot(now)
        if monitoring_window(now) and not recent(monitor.checked_at, now):
            result['status'] = 'stale'
        # JSON must remain serializable even if a damaged cache contains NaN.
        json.dumps(result, allow_nan=False)
        return result
    except (OSError, ValueError, TypeError, RecursionError):
        return unavailable
