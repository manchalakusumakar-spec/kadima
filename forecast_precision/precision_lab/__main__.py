"""Explicit local-file operations only; outputs never overwrite existing files."""
import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
from .core import fit, predict, score, PathModel, EvidenceError, IST, HORIZONS, FEATURES
from .evaluate import walk_forward
from .journal import Journal


def load(path):
    return json.loads(Path(path).read_text())


def records(path):
    with Path(path).open() as stream:
        return [json.loads(line) for line in stream if line.strip()]


def save(path, data):
    with Path(path).open('x') as stream:
        json.dump(data, stream, indent=2, allow_nan=False)
        stream.write('\n')


def synthetic_records():
    """Deterministic fixture, NOT market data, backtest evidence, or a claimed return."""
    out = []
    for day in range(18):
        for j in range(12):
            origin = datetime(2026, 1, 1, 9, 30, tzinfo=IST) + timedelta(days=day, minutes=5*j)
            issued = origin + timedelta(seconds=2)
            anchor = 23000.0 + day * 4 + j
            z = ((day * 5 + j * 3) % 19 - 9) / 10
            vals = {'ret_5m_bps': z*4, 'ret_15m_bps': z*7,
                    'ema20_gap_bps': z*3, 'ema50_gap_bps': z*5,
                    'rsi_centered': z/2, 'weighted_ret_5m_bps': z*2,
                    'weighted_up_share': (z+1)/2, 'constituent_weight_coverage': 1.0}
            features = {k: {'status':'AVAILABLE', 'value':v, 'source':'SYNTHETIC_FIXTURE',
                            'observed_at':origin.isoformat(), 'available_at':issued.isoformat()}
                        for k,v in vals.items()}
            s = {'id':f'SYNTHETIC-{day}-{j}', 'instrument':'NIFTY',
                 'session_date':origin.date().isoformat(), 'origin_at':origin.isoformat(),
                 'issued_at':issued.isoformat(), 'anchor':anchor,
                 'anchor_observed_at':origin.isoformat(), 'anchor_available_at':issued.isoformat(),
                 'closed_bar_end':origin.isoformat(), 'bar_complete':True,
                 'source_id':'SYNTHETIC_NOT_ANGELONE', 'config_id':'SYNTHETIC_V1', 'features':features}
            actuals = {}
            for h in HORIZONS:
                target = origin + timedelta(minutes=h)
                # Alternating shapes deliberately test independent-horizon fitting.
                bps = z * (h/4 if h % 10 else -h/5) + h/30
                actuals[str(h)] = {'target_at':target.isoformat(), 'observed_at':target.isoformat(),
                                  'available_at':(target+timedelta(seconds=2)).isoformat(),
                                  'complete':True, 'price':anchor*(1+bps/10000),
                                  'source':'SYNTHETIC_FIXTURE'}
            out.append({'snapshot':s,'actuals':actuals})
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='cmd',required=True)
    f=sub.add_parser('fit'); f.add_argument('--records',required=True); f.add_argument('--train-before',required=True); f.add_argument('--out',required=True)
    f.add_argument('--min-samples',type=int,default=100); f.add_argument('--min-sessions',type=int,default=10); f.add_argument('--lambda',dest='lam',type=float,default=10)
    q=sub.add_parser('predict'); q.add_argument('--model',required=True); q.add_argument('--snapshot',required=True); q.add_argument('--out',required=True); q.add_argument('--parent-id'); q.add_argument('--journal')
    q=sub.add_parser('score'); q.add_argument('--forecast',required=True); q.add_argument('--actuals',required=True); q.add_argument('--as-of',required=True); q.add_argument('--out',required=True)
    q=sub.add_parser('walk-forward'); q.add_argument('--records',required=True); q.add_argument('--out',required=True)
    q=sub.add_parser('synthetic-demo'); q.add_argument('--out-dir',required=True)
    a=p.parse_args()
    try:
        if a.cmd=='fit':
            result=fit(records(a.records),a.train_before,min_samples=a.min_samples,min_sessions=a.min_sessions,ridge_lambda=a.lam).to_dict()
            save(a.out,result)
        elif a.cmd=='predict':
            result=predict(PathModel.from_dict(load(a.model)),load(a.snapshot),parent_id=a.parent_id)
            if a.journal:
                j=Journal(a.journal)
                try: j.save(result)
                finally: j.close()
            save(a.out,result)
        elif a.cmd=='score':
            save(a.out,score(load(a.forecast),load(a.actuals),as_of=a.as_of))
        elif a.cmd=='walk-forward':
            save(a.out,walk_forward(records(a.records)))
        else:
            root=Path(a.out_dir); root.mkdir(parents=True,exist_ok=False)
            data=synthetic_records()
            with (root/'SYNTHETIC_records.jsonl').open('x') as stream:
                for row in data: stream.write(json.dumps(row)+'\n')
            m=fit(data,'2026-01-16'); f=predict(m,data[-1]['snapshot'])
            save(root/'SYNTHETIC_model.json',m.to_dict())
            save(root/'snapshot_example.json',data[-1]['snapshot'])
            save(root/'actuals_example.json',data[-1]['actuals'])
            save(root/'SYNTHETIC_forecast.json',f)
            save(root/'SYNTHETIC_score.json',score(f,data[-1]['actuals'],as_of='2026-01-18T15:30:00+05:30'))
            j=Journal(root/'demo.precision-lab.sqlite3'); j.save(f); j.close()
            save(root/'SYNTHETIC_walk_forward.json',walk_forward(data))
            print('Synthetic fixture only. Not a market backtest or evidence of achieved accuracy.')
        return 0
    except (EvidenceError, KeyError, OSError, ValueError) as exc:
        print(f'ERROR: {exc}',file=sys.stderr)
        return 2

if __name__=='__main__':
    raise SystemExit(main())
