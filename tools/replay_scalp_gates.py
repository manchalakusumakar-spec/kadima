#!/usr/bin/env python3
"""Read-only FAST_CANDIDATE gate comparison; sampled LTP outcomes, no broker."""
import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sqlite3
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kadima.config import DB_PATH, GAMMA_INDICES, IST
from kadima.index_event_gate import macro_event_block
from kadima.oi_confirmation import read_confirmation, scalp_oi_block

RULES = ('a: old', 'b: index news', 'c: news + live OI')


def open_readonly(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only=ON')
    return connection


def price_window(connection, row, at, minutes=60):
    """Entry from the most recent stored LTP available at decision time."""
    start, end = at - timedelta(seconds=30), at + timedelta(minutes=minutes)
    quotes = [dict(r) for r in connection.execute(
        'SELECT ts, ltp FROM oi_snapshots WHERE index_name=? AND ts>=? AND ts<=? '
        'AND token=? AND symbol=? AND ltp>0 ORDER BY ts, id',
        (row['underlying'], start.isoformat(), end.isoformat(), str(row['token']), row['symbol']))]
    before = [q for q in quotes if datetime.fromisoformat(q['ts']) <= at]
    after = [q for q in quotes if datetime.fromisoformat(q['ts']) > at]
    if not before:
        return dict(outcome='missing_entry', best=None, worst=None, complete=False, entry=None)
    entry = before[-1]['ltp']
    best, worst, outcome = entry, entry, 'neither'
    gaps, previous = [], at
    for quote in after:
        stamp, price = datetime.fromisoformat(quote['ts']), quote['ltp']
        gaps.append((stamp - previous).total_seconds())
        previous = stamp
        best, worst = max(best, price), min(worst, price)
        if outcome == 'neither':
            if price >= entry * 1.10:
                outcome = 'T1_first'
            elif price <= entry * .92:
                outcome = 'stop_first'
    complete = bool(after and (end - previous).total_seconds() <= 90 and max(gaps) <= 90)
    if outcome == 'neither' and not complete:
        outcome = 'incomplete_window'
    return dict(outcome=outcome, best=(best / entry - 1) * 100,
                worst=(worst / entry - 1) * 100, complete=complete, entry=entry)


def day_has_data(connection, day):
    start = day + 'T00:00:00+05:30'
    end = (date.fromisoformat(day) + timedelta(days=1)).isoformat() + 'T00:00:00+05:30'
    for name, index in GAMMA_INDICES.items():
        oi = connection.execute('SELECT 1 FROM oi_snapshots WHERE index_name=? AND ts>=? AND ts<? LIMIT 1',
                                (name, start, end)).fetchone()
        if not oi:
            continue
        if all(connection.execute('SELECT 1 FROM candles WHERE token=? AND interval=? '
                                  'AND start_ts>=? AND start_ts<? AND closed=1 LIMIT 1',
                                  (index.token, interval, start, end)).fetchone() for interval in (1, 5)):
            return True
    return False


def gate_sets(row, at, confirmation):
    # Historical blockers preserve historical switches/controls/news-risk.
    old = list(row.get('blocked_reasons', []))
    news = [r for r in old if r != 'fresh_news_coverage_required']
    macro = macro_event_block(row['underlying'], at)
    if macro and macro not in news:
        news.append(macro)
    oi = list(news)
    block = scalp_oi_block(row['direction'], at, confirmation)
    if block:
        oi.append(block)
    return old, news, oi


def summarize(rows):
    counts = Counter(r['outcome'] for r in rows)
    evaluated = sum(counts[k] for k in ('T1_first', 'stop_first', 'neither'))
    full = [r for r in rows if r['complete']]
    return dict(allowed=len(rows), evaluated=evaluated, wins=counts['T1_first'], stops=counts['stop_first'],
                t1_pct=100 * counts['T1_first'] / evaluated if evaluated else None,
                stop_pct=100 * counts['stop_first'] / evaluated if evaluated else None,
                best=sum(r['best'] for r in full) / len(full) if full else None,
                worst=sum(r['worst'] for r in full) / len(full) if full else None,
                full=len(full), missing=len(rows) - evaluated)


def format_value(value):
    return '-' if value is None else f'{value:.2f}'


def print_table(title, grouped):
    print('\n' + title)
    print('day         rule                 allowed eval wins stops T1-first% stop-first% avg-best% avg-worst% full60 missing')
    for (day, rule), values in sorted(grouped.items()):
        s = summarize(values)
        print(f"{day:11} {rule:20} {s['allowed']:7} {s['evaluated']:4} {s['wins']:4} {s['stops']:5} "
              f"{format_value(s['t1_pct']):>9} {format_value(s['stop_pct']):>11} "
              f"{format_value(s['best']):>9} {format_value(s['worst']):>10} {s['full']:6} {s['missing']:7}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default=DB_PATH)
    parser.add_argument('--audit-dir', default='/var/lib/kadima/audit')
    args = parser.parse_args()
    connection = open_readonly(args.db)
    grouped, totals, explicit = defaultdict(list), defaultdict(list), []
    data_days, events, oi_reasons = [], 0, Counter()
    try:
        connection.execute('BEGIN')  # one consistent read snapshot
        for path in sorted(Path(args.audit_dir).glob('*.jsonl')):
            day = path.stem
            try:
                trading_day = date.fromisoformat(day)
            except ValueError:
                continue
            if trading_day.weekday() >= 5 or not day_has_data(connection, day):
                continue
            data_days.append(day)
            for rule in RULES:
                grouped[day, rule] = []
                totals['TOTAL', rule] = totals['TOTAL', rule]
            for line in path.open():
                if 'FAST_CANDIDATE' not in line or 'SETUP_READY' not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if (row.get('event') != 'FAST_CANDIDATE' or row.get('stage') != 'SETUP_READY'
                        or row.get('underlying') not in GAMMA_INDICES):
                    continue
                at = datetime.fromisoformat(row['ts']).astimezone(IST)
                if not (9, 15) <= (at.hour, at.minute) < (15, 40):
                    continue
                events += 1
                confirmation = read_confirmation(connection, row['underlying'], at)
                oi_reasons[confirmation.get('reason') or 'valid'] += 1
                outcome = price_window(connection, row, at)
                gates = gate_sets(row, at, confirmation)
                for rule, blockers in zip(RULES, gates):
                    if not blockers:
                        grouped[day, rule].append(outcome)
                        totals['TOTAL', rule].append(outcome)
                if day == '2026-10-09' and row['underlying'] == 'BANKNIFTY' and at.hour == 9 and 30 <= at.minute <= 33:
                    explicit.append(dict(time=at.isoformat(), symbol=row['symbol'], score=row['score'],
                                         outcome=outcome, oi=confirmation,
                                         gates=dict(zip(RULES, gates))))
        print_table('PER DAY', grouped)
        print_table('TOTALS', totals)
        print('\n2026-10-09 BANKNIFTY setups')
        for row in explicit:
            print(json.dumps(row, ensure_ascii=False))
        print(f'\nData days: {len(data_days)}; SETUP_READY audit events: {events}; OI quality: {dict(oi_reasons)}')
        print('Each logged SETUP_READY event is counted separately; repeated episodes are not deduplicated.')
        print('Percent denominators exclude missing entry/incomplete neither outcomes. Best/worst averages use complete 60-minute windows only.')
        print('Historical switches and controls come from recorded blockers; current news-health rows cannot reconstruct expired news.')
        print('Hits are first sampled crossings of +10%/-8%, not executable fills or the live structural-stop/trailing strategy.')
        b, c = (summarize(totals['TOTAL', RULES[i]]) for i in (1, 2))
        recommend = c['wins'] > c['stops'] and c['wins'] >= b['wins'] and not oi_reasons.get('sample_spot_unavailable')
        print('Recommendation: ' + ('live passes the requested numerical test' if recommend else 'stay shadow') + '; flag remains shadow.')
    finally:
        connection.close()


if __name__ == '__main__':
    main()
