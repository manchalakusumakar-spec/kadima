#!/usr/bin/env python3
"""What-if replay: STOCK BLAST v1 + v1.1 rules applied to a past day's Move Capture triggers.

Read-only estimate, not a backtest-grade simulation. Uses:
  - MOVE_CAPTURE_TRIGGER events from DATA_DIR/audit/<day>.jsonl (time, stock, score, direction, spot)
  - minute option prices KADIMA recorded that day (price_history.db)
  - real lot sizes (instrument_master)
Assumptions (stated in the output):
  - entry at the trigger minute's first price + 2% (spread/slippage); a strike needs a recorded bar
    in the trigger minute and >= 70% minute coverage while held, else it is skipped as 'no data'
  - spread / OI / volume of that minute are unknown -> assumed to pass (optimistic)
  - option confirmation approximated by: the contract's price rose in the trigger minute
  - exits on minute bars, stop checked before the new high (conservative): -35% hard stop, +12% locks +3%,
    from +20% floor +20% and backstop at half the peak gain (the live 15-second peak exit usually sells
    nearer the peak, so this understates runners), no-progress exit after 5 min below +5%,
    15-min deadline unless the trade is >= +5% up, flat at 15:15
  - sizing: confidence share of free cash (40/70/100%), far strikes 20%, at most 2 lots on the primary
    (NOCAP lots unknown), risk per trade <= 15% of its allocation at the -35% stop,
    daily loss limit Rs 10,000; one position per stock at a time
"""
import argparse, json, math, os, sqlite3, sys
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--day', default='2026-10-26')
    ap.add_argument('--data', default='/var/lib/kadima')
    ap.add_argument('--cash', type=float, default=None)
    ap.add_argument('--max-lots', type=int, default=2)
    a = ap.parse_args()
    from kadima import stock_blast as sb
    from kadima.greeks import implied_vol_and_greeks
    from kadima.instrument_master import get_lot_size

    cash0 = a.cash
    if cash0 is None:
        try:
            cash0 = json.load(open(os.path.join(a.data, 'daily_cash.json')))['cash']
        except Exception:  # noqa: BLE001
            cash0 = 42160.0
    day = a.day
    d0 = datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=IST)

    # ---- option bars by underlying -------------------------------------------------------
    c = sqlite3.connect('file:' + os.path.join(a.data, 'price_history.db') + '?mode=ro', uri=True)
    contracts = {}   # underlying -> list of dict(symbol, token, strike, typ, expiry, bars{minute_ms: row})
    for key, sym, und, tok in c.execute(
            "SELECT key, symbol, underlying, token FROM price_days WHERE day=? AND asset='OPTION'", (day,)):
        tail = sym[len(und):]
        try:
            exp = datetime.strptime(tail[:7], '%d%b%y').replace(tzinfo=IST, hour=15, minute=30)
            typ, strike = tail[-2:], float(tail[7:-2])
        except ValueError:
            continue
        bars = {r[0]: r for r in c.execute(
            'SELECT minute, first, last, high, low FROM price_minutes WHERE day=? AND key=? ORDER BY minute',
            (day, key))}
        contracts.setdefault(und, []).append(dict(symbol=sym, token=tok, strike=strike, typ=typ,
                                                  expiry=exp, bars=bars))
    c.close()

    # ---- triggers ------------------------------------------------------------------------
    trig = []
    with open(os.path.join(a.data, 'audit', day + '.jsonl'), errors='ignore') as fh:
        for line in fh:
            if '"MOVE_CAPTURE_TRIGGER"' not in line:
                continue
            e = json.loads(line)
            if sb.is_index(e.get('underlying')):
                continue
            trig.append(e)
    trig.sort(key=lambda e: e['ts'])

    def minute_of(ts):
        t = datetime.fromisoformat(ts)
        return int(t.replace(second=0, microsecond=0).timestamp() * 1000), t

    Q = type('Q', (), {})
    delta_cache = {}
    realized, open_until, held = 0.0, {}, []   # held: (exit_minute, cost)
    trades, skipped = [], {}

    def skip(r):
        skipped[r] = skipped.get(r, 0) + 1

    for e in trig:
        und, score, direction, spot = e['underlying'], float(e['score']), e['direction'], float(e['price'])
        m, t = minute_of(e['ts'])
        if not sb.entry_time_allowed(t):
            skip('outside 09:15-15:10'); continue
        if realized <= -10000:
            skip('daily loss limit'); continue
        if open_until.get(und, 0) > m:
            skip('already in this stock'); continue
        pool = [x for x in contracts.get(und, []) if x['typ'] == direction and m in x['bars']]
        if not pool:
            skip('no recorded option bar at trigger'); continue
        nearest = min(x['expiry'] for x in pool)
        pool = [x for x in pool if x['expiry'] == nearest]
        days = max((nearest - t).total_seconds() / 86400, 0.01)
        quotes = []
        gmax = sb.gates(score)
        for x in pool:
            b = x['bars'][m]
            px = b[1]
            if not px or px <= 0:
                continue
            # cheap pre-gates before the (slow) IV solve
            if abs(x['strike'] - spot) / spot * 100 > gmax['max_distance_pct']:
                continue
            if px < gmax.get('cheap_min', 2.0) and gmax is not sb.LOTTERY:
                continue
            q = Q(); q.strike, q.ltp, q.ask, q.bid = x['strike'], px, px, px * 0.98
            q.volume, q.oi = 10 ** 6, 10 ** 6
            ck = (x['symbol'], m)
            if ck not in delta_cache:
                iv, g = implied_vol_and_greeks(spot=spot, strike=x['strike'], premium=px, days=days,
                                               opt_type=direction, tol=1e-3, max_iter=40)
                delta_cache[ck] = g['delta'] if g else None
            q.delta = delta_cache[ck]
            q.x, q.rising = x, (b[2] or px) >= px
            quotes.append(q)

        def pick(gates, lottery_only=False):
            ok = []
            for q in quotes:
                if sb.fail_reason(q, spot, gates):
                    continue
                lot = sb.is_lottery(q, spot)
                if lottery_only != lot:
                    continue
                ok.append(q)
            ok.sort(key=lambda q: sb.leverage(q, spot), reverse=True)
            return ok[0] if ok else None

        legs = []
        g = sb.gates(score)
        main = pick(g, False) or (pick(g, True) if g is sb.LOTTERY else None)
        if main is None:
            skip('no strike passes stock rules'); continue
        legs.append((main, sb.is_lottery(main, spot)))
        if g is sb.LOTTERY and not sb.is_lottery(main, spot):
            far = pick(g, True)
            if far is not None:
                legs.append((far, True))
        for q, lottery in legs:
            if not q.rising:
                skip('option not rising (confirmation)'); continue
            x = q.x
            lot = get_lot_size(x['symbol'], x['token'])
            if not lot:
                skip('lot size unknown'); continue
            entry = q.ltp * 1.02
            frac = sb.size_fraction(score) * (sb.LOTTERY_SIZE if lottery else 1.0)
            free = cash0 + realized - sum(cost for until, cost in held if until > m)
            alloc = max(0.0, free) * frac
            lots = min(a.max_lots, int(alloc // (entry * lot)), int(alloc * 0.15 // (entry * 0.35 * lot)))
            if lots < 1:
                skip('one lot too expensive for the allocation'); continue
            qty = lots * lot
            # ---- exit simulation on minute bars ----
            mins = sorted(k for k in x['bars'] if k > m)
            end = int(d0.replace(hour=15, minute=15).timestamp() * 1000)
            span = [k for k in range(m + 60000, end + 1, 60000)]
            peak, stop, exit_px, reason, exit_m = entry, entry * 0.65, None, None, None
            covered = 0
            for k in span:
                b = x['bars'].get(k)
                age = (k - m) / 60000
                if b is None:
                    continue
                covered += 1
                hi, lo, last = b[3], b[4], b[2]
                if lo is not None and lo <= stop:
                    exit_px, reason, exit_m = stop, 'stop', k; break
                peak = max(peak, hi or peak)
                if peak >= entry * 1.20:
                    stop = max(stop, entry * 1.20, entry + 0.5 * (peak - entry))
                elif peak >= entry * 1.12:
                    stop = max(stop, entry * 1.03)
                if age >= 5 and peak < entry * 1.05:
                    exit_px, reason, exit_m = last, 'no progress 5 min', k; break
                if age >= 15 and peak < entry * 1.05:
                    exit_px, reason, exit_m = last, 'holding deadline', k; break
            if exit_px is None:
                last_bar = max((k for k in mins if k <= end), default=None)
                if last_bar is None:
                    skip('no data after entry'); continue
                exit_px, reason, exit_m = x['bars'][last_bar][2], 'flat at 15:15', last_bar
            window = max(1, int((exit_m - m) / 60000))
            if covered / window < 0.7:
                skip('price data too sparse to simulate'); continue
            pnl = (exit_px - entry) * qty
            realized += pnl
            open_until[und] = exit_m
            held.append((exit_m, entry * qty))
            trades.append(dict(time=t.strftime('%H:%M'), stock=und, score=round(score, 1),
                               contract=x['symbol'][len(und):], far=lottery, lots=lots, qty=qty,
                               entry=round(entry, 2), peak=round(peak, 2), exit=round(exit_px, 2),
                               out=datetime.fromtimestamp(exit_m / 1000, IST).strftime('%H:%M'),
                               reason=reason, pnl=round(pnl)))
    wins = [t for t in trades if t['pnl'] > 0]
    print(json.dumps(dict(
        day=day, morning_cash=cash0, triggers=len(trig), trades=len(trades), wins=len(wins),
        net_pnl=round(sum(t['pnl'] for t in trades)),
        best=sorted(trades, key=lambda t: -t['pnl'])[:10],
        worst=sorted(trades, key=lambda t: t['pnl'])[:6],
        skipped=dict(sorted(skipped.items(), key=lambda kv: -kv[1])),
        far_strike_trades=sum(t['far'] for t in trades),
        far_strike_pnl=round(sum(t['pnl'] for t in trades if t['far']))), indent=1))


if __name__ == '__main__':
    main()
