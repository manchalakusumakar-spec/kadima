#!/usr/bin/env python3
"""Which entry features separate the replay's losers from its winners, and what each candidate
filter would have cost/saved on that day. Input: the --dump file from replay_stock_blast.py."""
import json, sys


def stats(rows):
    w = [t for t in rows if t['pnl'] > 0]
    return dict(n=len(rows), win=len(w), lose=len(rows) - len(w),
                winrate=round(100 * len(w) / len(rows)) if rows else 0,
                pnl=round(sum(t['pnl'] for t in rows)),
                loss_rs=round(sum(t['pnl'] for t in rows if t['pnl'] <= 0)))


def bucket(trades, key, edges):
    print(f'\n-- {key}')
    for lo, hi in zip(edges, edges[1:]):
        rows = [t for t in trades if t.get(key) is not None and lo <= t[key] < hi]
        if rows:
            s = stats(rows)
            print(f'  [{lo:>6}, {hi:>6})  n={s["n"]:3d} win%={s["winrate"]:3d}  pnl={s["pnl"]:>9}  losses={s["loss_rs"]:>8}')
    rows = [t for t in trades if t.get(key) is None]
    if rows:
        s = stats(rows)
        print(f'  missing          n={s["n"]:3d} win%={s["winrate"]:3d}  pnl={s["pnl"]:>9}')


def main():
    trades = json.load(open(sys.argv[1]))
    for t in trades:
        h, m = map(int, t['time'].split(':'))
        t['tod'] = h * 60 + m
    base = stats(trades)
    print('ALL', base)
    print('loss exit reasons:', {r: sum(1 for t in trades if t['pnl'] <= 0 and t['reason'] == r)
                                for r in {t['reason'] for t in trades}})
    bucket(trades, 'score', [0, 70, 75, 80, 85, 90, 101])
    bucket(trades, 'trigger_n', [1, 2, 3, 5, 10, 1000])
    bucket(trades, 'day_move', [-50, -1, 0, 1, 2, 4, 50])
    bucket(trades, 'ret5', [-50, 0, 0.3, 0.6, 1, 50])
    bucket(trades, 'ret15', [-50, 0, 0.5, 1, 2, 50])
    bucket(trades, 'nifty15', [-50, -0.1, 0, 0.1, 50])
    bucket(trades, 'opt_mom3', [-100, 0, 5, 10, 20, 1000])
    bucket(trades, 'moneyness', [-50, -2, -0.5, 0.5, 2, 4, 50])
    bucket(trades, 'tod', [555, 585, 600, 660, 780, 870, 900, 920])
    bucket(trades, 'entry', [0, 3, 5, 10, 20, 50, 100000])

    def f(t, k, lo):
        return t.get(k) is not None and t[k] >= lo
    cands = {
        'score>=75': lambda t: t['score'] >= 75,
        'trigger_n>=2': lambda t: t['trigger_n'] >= 2,
        'day_move>=0': lambda t: f(t, 'day_move', 0),
        'day_move>=1': lambda t: f(t, 'day_move', 1),
        'ret5>=0.3': lambda t: f(t, 'ret5', 0.3),
        'ret15>=0.5': lambda t: f(t, 'ret15', 0.5),
        'nifty15>=0': lambda t: f(t, 'nifty15', 0),
        'opt_mom3>=5': lambda t: f(t, 'opt_mom3', 5),
        'entry>=5': lambda t: t['entry'] >= 5,
        'not 09:15-09:45': lambda t: t['tod'] >= 585,
        'day_move>=0 & ret5>=0.3': lambda t: f(t, 'day_move', 0) and f(t, 'ret5', 0.3),
        'day_move>=0 & opt_mom3>=5': lambda t: f(t, 'day_move', 0) and f(t, 'opt_mom3', 5),
        'day_move>=0 & entry>=5': lambda t: f(t, 'day_move', 0) and t['entry'] >= 5,
        'day_move>=0 & ret5>=0.3 & entry>=5': lambda t: f(t, 'day_move', 0) and f(t, 'ret5', 0.3) and t['entry'] >= 5,
    }
    print('\nFILTER                              kept  win  lose  win%      pnl   losses   winners_dropped_pnl')
    for name, fn in cands.items():
        kept = [t for t in trades if fn(t)]
        drop = [t for t in trades if not fn(t)]
        s = stats(kept)
        lost_win = round(sum(t['pnl'] for t in drop if t['pnl'] > 0))
        print(f'{name:34s} {s["n"]:4d} {s["win"]:4d} {s["lose"]:5d} {s["winrate"]:4d} {s["pnl"]:9d} {s["loss_rs"]:8d} {lost_win:12d}')
    big = [t for t in trades if t['pnl'] >= 5000]
    print('\nbig winners (>= Rs 5k) features:')
    for t in sorted(big, key=lambda t: -t['pnl']):
        print(' ', t['time'], t['stock'], t['pnl'], {k: t.get(k) for k in
              ('score', 'trigger_n', 'day_move', 'ret5', 'ret15', 'nifty15', 'opt_mom3', 'moneyness', 'entry')})


if __name__ == '__main__':
    main()
