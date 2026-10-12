"""Immutable, local research journal. Never open the trading/forecast database here."""
from __future__ import annotations
import json
from pathlib import Path
import sqlite3
from .core import canonical, digest, EvidenceError

class Journal:
    def __init__(self, path: str | Path):
        path = Path(path)
        if not path.name.endswith('.precision-lab.sqlite3'):
            raise EvidenceError('use a separate *.precision-lab.sqlite3 research database')
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS forecasts (
          id TEXT PRIMARY KEY, parent_id TEXT REFERENCES forecasts(id),
          body TEXT NOT NULL, sha256 TEXT NOT NULL, snapshot_id TEXT NOT NULL,
          model_id TEXT NOT NULL, UNIQUE(snapshot_id,model_id));
        CREATE TRIGGER IF NOT EXISTS immutable_forecast_update
        BEFORE UPDATE ON forecasts BEGIN SELECT RAISE(ABORT,'immutable'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_forecast_delete
        BEFORE DELETE ON forecasts BEGIN SELECT RAISE(ABORT,'immutable'); END;
        ''')

    def save(self, forecast: dict) -> bool:
        body, hash_ = canonical(forecast), digest(forecast)
        old = self.db.execute('SELECT body,sha256 FROM forecasts WHERE id=?',
                              (forecast['id'],)).fetchone()
        if old:
            if old != (body, hash_):
                raise EvidenceError('same forecast ID cannot be overwritten')
            return False
        parent_id = forecast.get('parent_id')
        if bool(parent_id) != (forecast.get('kind') == 'REVISION'):
            raise EvidenceError('revision/parent mismatch')
        if parent_id:
            parent = self.get(parent_id)
            from .core import instant
            if instant(forecast['issued_at']) <= instant(parent['issued_at']):
                raise EvidenceError('revision must be issued after its parent')
        existing = self.db.execute('SELECT id FROM forecasts WHERE snapshot_id=? AND model_id=?',
                                   (forecast['snapshot_id'],forecast['model_id'])).fetchone()
        if existing:
            raise EvidenceError('snapshot/model pair already published; do not rewrite source evidence')
        with self.db:
            self.db.execute('INSERT INTO forecasts VALUES (?,?,?,?,?,?)',
                            (forecast['id'], parent_id, body, hash_, forecast['snapshot_id'],forecast['model_id']))
        return True

    def get(self, forecast_id: str) -> dict:
        row = self.db.execute('SELECT body,sha256 FROM forecasts WHERE id=?', (forecast_id,)).fetchone()
        if not row:
            raise EvidenceError('parent/forecast not found')
        body, stored = row
        value = json.loads(body)
        if digest(value) != stored:
            raise EvidenceError('journal hash mismatch')
        return value

    def close(self):
        self.db.close()
