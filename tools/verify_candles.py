#!/usr/bin/env python3
"""Read-only validation of a completed 5m window, end exclusive.

python tools/verify_candles.py NIFTY 2026-09-22 --start 09:15 --end 15:30
Choose the actual historical session/window; no holiday/calendar inference.
"""
import argparse
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kadima.config import DB_PATH, INDICES, IST  # noqa: E402


def stamp(value):
    value = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('timezone_required')
    return value.astimezone(IST)


def compare_candles(ours, response, start, end, *, now, tolerance=.05):
    """Pure comparison; missing, duplicate, malformed and unclosed bars fail."""
    start, end, now = map(stamp, (start, end, now))
    if (not math.isfinite(tolerance) or tolerance < 0 or end <= start or end > now
            or start.date() != end.date() or start.second or end.second
            or start.microsecond or end.microsecond
            or start.minute % 5 or end.minute % 5):
        raise ValueError('completed_aligned_same_day_window_required')
    expected = set()
    at = start
    while at < end:
        expected.add(at)
        at += timedelta(minutes=5)
    issues = []
    if (not isinstance(response, dict) or response.get('status') is not True
            or not isinstance(response.get('data'), list)):
        return dict(ok=False, expected_bars=len(expected), compared=0,
                    issues=['broker_response_invalid'])

    def index(rows, broker):
        out = {}
        label = 'broker' if broker else 'local'
        for r in rows:
            try:
                t = stamp(r[0] if broker else r['start_ts'])
                # The endpoint may include a bar opening exactly at end.
                if t == end and broker:
                    continue
                if t not in expected:
                    raise ValueError('outside_window_or_unaligned')
                if not broker and r['closed'] != 1:
                    raise ValueError('unclosed')
                raw = r[1:6] if broker else [r[k] for k in ('open','high','low','close','volume')]
                if len(raw) != 5 or any(isinstance(v, bool) or v is None for v in raw):
                    raise ValueError('invalid_values')
                o,h,l,c,v = values = tuple(float(v) for v in raw)
                if (not all(math.isfinite(x) for x in values) or min(o,h,l,c) <= 0
                        or not l <= min(o,c) <= max(o,c) <= h or v < 0 or not v.is_integer()):
                    raise ValueError('invalid_ohlcv')
                if t in out:
                    raise ValueError('duplicate')
                out[t] = values
            except (ValueError, TypeError, KeyError, IndexError, OverflowError) as exc:
                issues.append(f'{label}:invalid_row:{type(exc).__name__}')
        for t in sorted(expected - out.keys()):
            issues.append(f'{label}:missing:{t.isoformat()}')
        return out

    local, broker = index(ours, False), index(response['data'], True)
    for t in sorted(local.keys() & broker.keys()):
        for n,k in enumerate(('open','high','low','close','volume')):
            allowed = tolerance + 1e-9 if k != 'volume' else 0
            if abs(local[t][n] - broker[t][n]) > allowed:
                issues.append(f'mismatch:{t.isoformat()}:{k}')
    return dict(ok=not issues, expected_bars=len(expected),
                compared=len(local.keys() & broker.keys()), issues=issues)


def local_window(path, token, start, end):
    # Never create or migrate the database during verification.
    with sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute(
            'SELECT * FROM candles WHERE token=? AND interval=5 '
            'AND julianday(start_ts)>=julianday(?) AND julianday(start_ts)<julianday(?) '
            'ORDER BY start_ts', (token, start.isoformat(), end.isoformat()))]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('index', choices=sorted(INDICES))
    parser.add_argument('day')
    parser.add_argument('--start', default='09:15')
    parser.add_argument('--end', default='15:30')
    parser.add_argument('--db', default=DB_PATH)
    args = parser.parse_args(argv)
    try:
        start = datetime.strptime(f'{args.day} {args.start}', '%Y-%m-%d %H:%M').replace(tzinfo=IST)
        end = datetime.strptime(f'{args.day} {args.end}', '%Y-%m-%d %H:%M').replace(tzinfo=IST)
        now = datetime.now(IST)
        compare_candles([], {'status': True, 'data': []}, start, end, now=now)
        idx = INDICES[args.index]
        ours = local_window(args.db, idx.token, start, end)
        from kadima.session import SESSION
        SESSION.ensure()
        response = SESSION.api.getCandleData({
            'exchange': idx.exchange, 'symboltoken': idx.token, 'interval': 'FIVE_MINUTE',
            'fromdate': start.strftime('%Y-%m-%d %H:%M'),
            'todate': end.strftime('%Y-%m-%d %H:%M')})
        report = compare_candles(ours, response, start, end, now=now)
        report.update(index=args.index, start=start.isoformat(), end_exclusive=end.isoformat())
        print(json.dumps(report))
        return 0 if report['ok'] else 1
    except Exception as exc:
        # Never print broker exception strings, which may expose credentials.
        print(json.dumps({'ok': False, 'error_type': type(exc).__name__}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
