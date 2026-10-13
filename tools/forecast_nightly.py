"""Nightly forecast maintenance (runs at 16:05 IST weekdays via the kadima-forecast-nightly timer).

1. History: tops up NIFTY 5-minute candles from Angel One into forecast.db
   (history_candles), working back towards two years. The engine has stopped
   and logged out by 15:40, so this short login cannot disturb live trading.
   Skipped if the disk has less than 300 MB free.
2. Replay: re-forecasts every slot of the last ~120 sessions exactly as the
   live thread would (data before each slot only, option-chain inputs where
   KADIMA recorded them), and stores the results as calibration samples.
3. Retune: fits backtest starting values (the prior) and runs the same guarded
   champion/challenger step the live thread runs after every slot.

Never touches trading, orders or engine settings.

    python tools/forecast_nightly.py              # normal run
    python tools/forecast_nightly.py --no-fetch   # skip the Angel One download
    python tools/forecast_nightly.py --force      # allow a replay during market hours (no fetch)
"""
import json
import argparse
import math
import os
import shutil
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kadima import audit, hourly_forecast as HF, hourly_forecast_model as M  # noqa: E402
from kadima.config import DATA_DIR, INDICES, IST  # noqa: E402

TARGET_DAYS = 730
CHUNK_DAYS = 90
REPLAY_DAYS = 120
ANALOG_POOL_DAYS = 120
MIN_FREE_MB = 300
NIGHTLY_EVAL_SLOTS = 70     # ~10 sessions held out when judging new weights overnight


def log(msg):
    print(datetime.now(IST).strftime('%H:%M:%S'), msg, flush=True)


def market_open(now):
    return now.weekday() < 5 and (9, 0) <= (now.hour, now.minute) < (15, 45)


# ---------------------------------------------------------------- 1. history
def fetch(index_name, now):
    free_mb = shutil.disk_usage(DATA_DIR).free / 1e6
    if free_mb < MIN_FREE_MB:
        log(f'history: skipped, only {free_mb:.0f} MB free')
        return 0
    from kadima.session import SESSION
    idx = INDICES[index_name]
    c = HF.conn()
    c.execute('CREATE TABLE IF NOT EXISTS forecast_meta (key TEXT PRIMARY KEY, value TEXT)')
    have = c.execute('SELECT MIN(start_ts) FROM history_candles WHERE token=?', (idx.token,)).fetchone()
    floor = c.execute('SELECT value FROM forecast_meta WHERE key=?', ('history_floor:' + idx.token,)).fetchone()
    windows = [(now - timedelta(days=10), now)]          # recent days, every night
    oldest = datetime.fromisoformat(have[0]) if have[0] else now - timedelta(days=10)
    target = now - timedelta(days=TARGET_DAYS)
    if floor:                                            # broker had nothing older than this
        target = max(target, datetime.fromisoformat(floor[0]))
    end = oldest
    for _ in range(4):                                   # then up to ~360 days further back per night
        if end <= target:
            break
        start = max(target, end - timedelta(days=CHUNK_DAYS))
        windows.append((start, end))
        end = start
    added = 0
    try:
        SESSION.ensure()
        for start, end in windows:
            resp = SESSION.api.getCandleData(dict(
                exchange=idx.exchange, symboltoken=idx.token, interval='FIVE_MINUTE',
                fromdate=start.strftime('%Y-%m-%d 09:15'), todate=end.strftime('%Y-%m-%d 15:30')))
            ok = isinstance(resp, dict) and resp.get('status')
            rows = (resp.get('data') or []) if ok else []
            batch = []
            for r in rows:
                try:
                    ts = datetime.fromisoformat(str(r[0]))
                    ts = ts if ts.tzinfo else ts.replace(tzinfo=IST)
                    o, h, lo, cl = (float(r[1]), float(r[2]), float(r[3]), float(r[4]))
                except (TypeError, ValueError, IndexError):
                    continue
                if min(o, h, lo, cl) > 0:
                    batch.append((idx.token, ts.astimezone(IST).isoformat(), o, h, lo, cl))
            with c:
                before = c.total_changes
                c.executemany('INSERT OR IGNORE INTO history_candles VALUES (?,?,?,?,?,?)', batch)
                added += c.total_changes - before
            log(f'history: {start:%Y-%m-%d}..{end:%Y-%m-%d} -> {len(rows)} bars')
            if ok and not rows and end <= oldest:
                with c:
                    c.execute('INSERT OR REPLACE INTO forecast_meta VALUES (?,?)',
                              ('history_floor:' + idx.token, end.isoformat()))
                break                                    # nothing older exists; stop asking
            time.sleep(1.5)
    except Exception as exc:                            # noqa: BLE001 - keep going without new history
        log(f'history: stopped ({type(exc).__name__}: {str(exc)[:120]})')
    finally:
        try:
            SESSION.logout()
        except Exception:                               # noqa: BLE001
            pass
    total = c.execute('SELECT COUNT(DISTINCT substr(start_ts,1,10)) FROM history_candles WHERE token=?',
                      (idx.token,)).fetchone()[0]
    log(f'history: +{added} bars, {total} sessions held')
    return added


# ---------------------------------------------------------------- 2. replay
def replay(index_name, now):
    idx = INDICES[index_name]
    rows = HF.history_rows(idx.token, now, days=REPLAY_DAYS + ANALOG_POOL_DAYS + 30)
    days = [d for d in M.build_days(rows) if sum(c is not None for c in d.closes) >= 60]
    params = HF.load_params(index_name)
    first = max(10, len(days) - REPLAY_DAYS)
    samples = []
    t_start = time.monotonic()
    for di in range(first, len(days)):
        lo = max(0, di - ANALOG_POOL_DAYS)
        sub = days[lo:di + 1]
        tod = M.tod_profile(sub[:-1])
        d = sub[-1]
        for start, end in M.SLOTS:
            i, n = M.bar_index(start), M.slot_steps(start, end)
            if d.opens[i] is None or any(d.closes[i + k] is None for k in range(n)):
                continue
            t0 = datetime(d.day.year, d.day.month, d.day.day, start[0], start[1], tzinfo=IST)
            oi = HF.oi_features(index_name, t0, idx.strike_step)
            f = M.make_forecast(sub, len(sub) - 1, i, n, d.opens[i], as_of=d.day, tod=tod,
                                params=params, oi=oi)
            if f is None:
                continue
            act = [d.closes[i + k] for k in range(n)]
            inp = f.inputs
            zs = [abs(math.log(a / f.anchor) / f.sigma - inp['mean'][k]) / inp['sd'][k]
                  for k, a in enumerate(act) if inp['sd'][k] > 0]
            smp = {k: inp.get(k) for k in M.COMPONENTS}
            smp.update(day=d.day.isoformat(), slot=M.slot_label(start), p_raw=inp['p_raw'],
                       sd_end=inp['sd_end'], y=math.log(act[-1] / f.anchor) / f.sigma,
                       up=act[-1] > f.anchor, zs=zs)
            samples.append(smp)
    c = HF.conn()
    with c:
        c.execute('DELETE FROM hourly_backtest_samples WHERE index_name=?', (index_name,))
        c.executemany('INSERT OR REPLACE INTO hourly_backtest_samples VALUES (?,?,?,?)',
                      [(index_name, s['day'], s['slot'], json.dumps(s)) for s in samples])
    log(f'replay: {len(samples)} slots over {len(days) - first} sessions in {time.monotonic() - t_start:.0f}s')
    return samples


# ---------------------------------------------------------------- 3. retune
def retune(index_name, samples):
    prior = M.fit_params(samples, prior=M.DEFAULT_PARAMS) if samples else dict(M.DEFAULT_PARAMS)
    c = HF.conn()
    with c:
        c.execute('INSERT OR REPLACE INTO hourly_forecast_model VALUES (?,?,?)',
                  (index_name + ':prior', json.dumps(prior), datetime.now(IST).isoformat()))
    params, decision, change = HF.recalibrate_index(index_name, eval_slots=NIGHTLY_EVAL_SLOTS)
    card = decision.get('scorecard') or {}
    log('retune: ' + (('changed ' + change) if change else decision['reason']))
    log('parts that pointed the right way: ' + ', '.join(
        f"{k} {v['hit']:.0%} (n={v['n']})" for k, v in card.items() if v.get('hit') is not None))
    audit.log('hourly_forecast_nightly', index=index_name, samples=len(samples),
              adopted=decision['adopted'], reason=decision['reason'], change=change,
              band_only=decision.get('band_only'))
    return params, decision


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--only', choices=('bhavcopy',))
    parser.add_argument('--date', type=lambda value: datetime.strptime(value, '%Y-%m-%d').date())
    parser.add_argument('--no-fetch', action='store_true')
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    if args.date and args.only != 'bhavcopy':
        parser.error('--date applies only to --only bhavcopy')
    now = datetime.now(IST)
    force = args.force
    if market_open(now) and not force:
        log('market hours: not running (use --force to replay without fetching)')
        return 1
    from kadima import forecast_bhavcopy
    forecast_bhavcopy.run_nightly(HF.conn(), now, args.date)
    if args.only == 'bhavcopy':
        return 0
    for name in HF.FORECAST_INDICES:
        if name not in INDICES:
            continue
        if not args.no_fetch and not market_open(now):
            fetch(name, now)
        samples = replay(name, now)
        retune(name, samples)
    return 0


if __name__ == '__main__':
    sys.exit(main())
