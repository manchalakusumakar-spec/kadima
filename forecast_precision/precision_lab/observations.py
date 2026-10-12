"""Immutable packet storage and strict original-only horizon scorecards."""
from .core import canonical, digest, EvidenceError, HORIZONS
from .journal import Journal

class ObservationJournal(Journal):
    def __init__(self,path):
        super().__init__(path)
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS snapshots(id TEXT PRIMARY KEY,body TEXT NOT NULL,sha256 TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS immutable_snapshot_update BEFORE UPDATE ON snapshots
        BEGIN SELECT RAISE(ABORT,'immutable'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_snapshot_delete BEFORE DELETE ON snapshots
        BEGIN SELECT RAISE(ABORT,'immutable'); END;
        ''')

    def save_packet(self,packet):
        body, sha=canonical(packet),digest(packet)
        row=self.db.execute('SELECT body,sha256 FROM snapshots WHERE id=?',(packet['id'],)).fetchone()
        if row:
            if row != (body,sha): raise EvidenceError('snapshot ID cannot be overwritten')
            return False
        with self.db:
            self.db.execute('INSERT INTO snapshots VALUES (?,?,?)',(packet['id'],body,sha))
        return True


def horizon_report(scores):
    result={h:{'scheduled':0,'scored':0,'below_5':0,'coverage':None,
               'below_5_fraction':None,'worst_error_points':None} for h in HORIZONS}
    seen=set()
    for score in scores:
        if score['kind'] != 'ORIGINAL':
            raise EvidenceError('scorecard requires originals')
        if score['forecast_id'] in seen: raise EvidenceError('duplicate original')
        seen.add(score['forecast_id'])
        for row in score['rows']:
            target=result[row['horizon_minutes']]; target['scheduled']+=1
            error=row['absolute_error']
            if error is not None:
                target['scored']+=1; target['below_5']+=int(error<5)
                target['worst_error_points']=max(error,target['worst_error_points'] or 0)
    for target in result.values():
        if target['scheduled']: target['coverage']=target['scored']/target['scheduled']
        if target['scored']: target['below_5_fraction']=target['below_5']/target['scored']
    return {'horizons':result,'net_error':None,'net_error_status':'AGGREGATION_NOT_YET_AGREED'}
