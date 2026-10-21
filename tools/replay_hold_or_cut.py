#!/usr/bin/env python3
"""Replay one day's real trades under kadima/hold_or_cut.py. READ-ONLY.

Opens kadima.db and forecast.db with mode=ro, writes nothing (audit and
Celestial are stubbed), places no orders. For every position entered that day
it walks the completed 1-minute index closes from entry to 15:20 IST, prices
the option from the stored option-chain snapshots, and compares:
  actual  - what KADIMA did (positions table)
  policy  - hold winners / cut losers: index-based cut or hold; a winner
            ignores the actual exit unless it was a hard cap, loss limit or EOD

Usage (on EC2, ideally after 15:30 IST - it is read-only but uses some RAM):
  cd ~/kadima-py && venv/bin/python tools/replay_hold_or_cut.py 2026-10-21
"""
import sqlite3
import sys
import types
from datetime import date, datetime, time, timedelta
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kadima                                     # noqa: E402
from kadima import config                         # noqa: E402
from kadima.config import IST                     # noqa: E402

# Nothing in a replay may write: stub the audit log and Celestial health.
_stub = types.SimpleNamespace(log=lambda *a, **k: None, health=lambda *a, **k: None)
sys.modules['kadima.celestial'] = _stub
kadima.celestial = _stub
from kadima import trend_rules as T              # noqa: E402
from kadima import hold_or_cut as H              # noqa: E402
T.audit = _stub

HARD_REASONS = ('daily_loss_limit_breached', 'eod_force_close', 'owned_eod_watchdog')
CUTOFF = time(15, 20)
ATR_BARS = 14


def ro(path):
    c = sqlite3.connect('file:%s?mode=ro' % path, uri=True)
    c.row_factory = sqlite3.Row
    return c


def ts(value):
    t = datetime.fromisoformat(str(value))
    return t if t.tzinfo else t.replace(tzinfo=IST)


class Data:
    def __init__(self, day):
        self.day = day
        self.db = ro(config.DB_PATH)
        fdb = Path(config.DATA_DIR) / 'forecast.db'
        self.fdb = ro(fdb) if fdb.exists() else None
        self._five = {}

    def five(self, token):
        """Closed 5-min candles, 30 days back: engine candles over broker backfill."""
        if token not in self._five:
            since = (datetime.combine(self.day, time(0), IST) - timedelta(days=30)).isoformat()
            merged = {}
            if self.fdb is not None:
                try:
                    for r in self.fdb.execute('SELECT start_ts,open,high,low,close FROM history_candles '
                                              'WHERE token=? AND start_ts>=?', (token, since)):
                        merged[r[0]] = r
                except sqlite3.Error:
                    pass
            for r in self.db.execute('SELECT start_ts,open,high,low,close FROM candles WHERE token=? '
                                     'AND interval=5 AND closed=1 AND start_ts>=?', (token, since)):
                merged[r[0]] = r
            self._five[token] = [(ts(k), *merged[k][1:]) for k in sorted(merged)]
        return self._five[token]

    def rows_before(self, token, until):
        return [r for r in self.five(token) if r[0] + timedelta(minutes=5) <= until]

    def ones(self, token):
        lo = datetime.combine(self.day, time(9, 15), IST).isoformat()
        hi = datetime.combine(self.day, time(15, 31), IST).isoformat()
        out = [(ts(r[0]) + timedelta(minutes=1), r[1]) for r in self.db.execute(
            'SELECT start_ts, close FROM candles WHERE token=? AND interval=1 AND start_ts>=? '
            'AND start_ts<? ORDER BY start_ts', (token, lo, hi))]
        if not out:   # fall back to 5-min closes
            out = [(r[0] + timedelta(minutes=5), r[4]) for r in self.five(token)
                   if r[0].date() == self.day]
        return out

    def premiums(self, token, symbol, start):
        q = ('SELECT ts, ltp FROM oi_snapshots WHERE {} AND ts>=? AND ltp>0 ORDER BY ts')
        rows = self.db.execute(q.format('token=?'), (str(token), start.isoformat())).fetchall()
        if not rows and symbol:
            rows = self.db.execute(q.format('symbol=?'), (symbol, start.isoformat())).fetchall()
        if not rows:
            rows = self.db.execute('SELECT ts, ltp FROM ticks WHERE token=? AND ts>=? AND ltp>0 '
                                   'ORDER BY ts', (str(token), start.isoformat())).fetchall()
        return [(ts(r[0]), float(r[1])) for r in rows]

    def chain(self, name):
        """{snapshot ts: [(strike, opt_type, oi)]} for the day, sorted by ts."""
        key = ('chain', name)
        if key not in self._five:
            lo = datetime.combine(self.day, time(9, 0), IST).isoformat()
            hi = datetime.combine(self.day, time(15, 31), IST).isoformat()
            snaps = {}
            for r in self.db.execute('SELECT ts, strike, opt_type, oi FROM oi_snapshots WHERE '
                                     'index_name=? AND ts>=? AND ts<?', (name, lo, hi)):
                snaps.setdefault(r[0], []).append((r[1], r[2], r[3]))
            self._five[key] = sorted((ts(k), v) for k, v in snaps.items())
        return self._five[key]

    def snapshot(self, name, t):
        rows = None
        for st, v in self.chain(name):
            if st > t:
                break
            rows = v
        return rows

    def wall(self, name, t, spot, direction):
        rows = self.snapshot(name, t)
        return H.wall_from(rows, spot, direction) if rows else None

    def pcr(self, name, t):
        rows = self.snapshot(name, t)
        return H.pcr_from(rows) if rows else None


def atr(rows):
    trs, prev = [], None
    for r in rows[-(ATR_BARS + 1):]:
        trs.append(max(r[2] - r[3], abs(r[2] - prev) if prev else 0, abs(r[3] - prev) if prev else 0))
        prev = r[4]
    return sum(trs[1:]) / len(trs[1:]) if len(trs) > 4 else None


def at_or_before(series, t, max_age=180):
    best = None
    for st, v in series:
        if st > t:
            break
        best = (st, v)
    return best[1] if best and (t - best[0]).total_seconds() <= max_age else None


def replay(data, pos, variant='base'):
    name = pos['index_name']
    idx = config.GAMMA_INDICES.get(name)
    if idx is None:
        return dict(note='index %s not in GAMMA_INDICES' % name)
    entry_t = ts(pos['entry_ts'])
    entry = float(pos['entry_price'])
    spots = data.ones(idx.token)
    prem = data.premiums(pos['token'], pos['symbol'], entry_t - timedelta(minutes=2))
    entry_spot = at_or_before(spots, entry_t, 300)
    if not prem or entry_spot is None:
        return dict(note='missing data: %d premium samples, entry spot %s' % (len(prem), entry_spot))
    actual_t = ts(pos['exit_ts']) if pos['exit_ts'] else None
    actual_reason = pos['exit_reason'] or ''
    T._rows = lambda token, now: data.rows_before(token, now)
    T._inputs_cache.clear(); T._trend.clear()
    for t, spot in spots:            # warm the day trend (and its hysteresis) up to entry
        if t > entry_t:
            break
        T.day_trend(name, idx.token, spot, None, t)
    state = H.new_state(entry_spot)
    held_past_actual = False
    cutoff = datetime.combine(data.day, CUTOFF, IST)
    best_after = None
    for t, spot in spots:
        if t <= entry_t:
            continue
        ltp = at_or_before(prem, t)
        if actual_t and t >= actual_t and ltp is not None:
            best_after = max(best_after or 0, ltp)
        if t >= cutoff:
            return dict(time=t, price=ltp, reason='eod_1520', best_after=best_after)
        if ltp is None:
            continue
        a = T._rows(idx.token, t)
        trend = T.day_trend(name, idx.token, spot, None, t)
        wall = data.wall(name, t, spot, pos['direction']) if variant in ('oi', 'pcr') else None
        pcr = data.pcr(name, t) if variant == 'pcr' else None
        action, reason = H.step(state, pos['direction'], entry, ltp, spot, atr(a), trend,
                                variant=variant, wall=wall, pcr=pcr)
        if action == 'exit':
            return dict(time=t, price=ltp, reason=reason, best_after=best_after, phase=state['phase'])
        if actual_t and t >= actual_t and not held_past_actual:
            if action == 'hold' and actual_reason not in HARD_REASONS:
                held_past_actual = True       # winner: the actual soft/trail exit is vetoed
            else:
                return dict(time=actual_t, price=pos['exit_price'], reason='same_as_actual',
                            best_after=best_after, phase=state['phase'])
    last = prem[-1]
    return dict(time=last[0], price=last[1], reason='end_of_data', best_after=best_after)


def main():
    day = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else datetime.now(IST).date()
    data = Data(day)
    rows = data.db.execute('SELECT * FROM positions WHERE entry_ts LIKE ? ORDER BY entry_ts',
                           (day.isoformat() + '%',)).fetchall()
    print('replay', day, 'positions', len(rows), 'rule', 'cut %.1f/%.1f ATR, prove %.1f ATR, giveback >= %.1f ATR, %d closes'
          % (H.CUT_COUNTER, H.CUT_ANY, H.PROVE, H.GIVEBACK, H.CONFIRM))
    variants = sys.argv[2].split(',') if len(sys.argv) > 2 else list(H.VARIANTS)
    tot_a, tot = 0.0, {v: 0.0 for v in variants}
    for p in rows:
        p = dict(p)
        qty = (p['lots'] or 1) * (p['lot_size'] or 1)
        a_pnl = ((p['exit_price'] or 0) - p['entry_price']) * qty if p['exit_price'] else None
        print('%s %s %s %s qty %d entry %.2f @%s | actual %s @%s %s P&L %s' % (
            p['signal_id'], p['kind'], p['symbol'], p['direction'], qty, p['entry_price'],
            str(p['entry_ts'])[11:19], p['exit_price'], str(p['exit_ts'] or '')[11:19], p['exit_reason'],
            None if a_pnl is None else round(a_pnl)))
        ok = a_pnl is not None
        results = {}
        for v in variants:
            r = replay(data, p, v)
            if 'note' in r:
                print('   %-4s SKIPPED %s' % (v, r['note'])); ok = False; continue
            p_pnl = (r['price'] - p['entry_price']) * qty if r['price'] else None
            ok = ok and p_pnl is not None
            results[v] = p_pnl
            print('   %-4s exit %s @%s %s (%s) P&L %s | best after actual exit %s' % (
                v, None if r['price'] is None else round(r['price'], 2), r['time'].strftime('%H:%M'),
                r['reason'], r.get('phase', '-'), None if p_pnl is None else round(p_pnl),
                None if r.get('best_after') is None else round(r['best_after'], 2)))
        if ok:
            tot_a += a_pnl
            for v in variants:
                tot[v] += results[v]
    print('TOTAL (gross, before charges) actual %.0f | ' % tot_a +
          ' | '.join('%s %.0f (%+.0f)' % (v, tot[v], tot[v] - tot_a) for v in variants))


if __name__ == '__main__':
    main()
