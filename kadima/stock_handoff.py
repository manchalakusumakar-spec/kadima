"""Durable latest-signal handoff. Scanner publishes; only central executor orders."""
import json
import math
from datetime import datetime
from . import storage
from .utils import now_ist


def connection():
    c = storage.conn()
    c.execute('''CREATE TABLE IF NOT EXISTS stock_handoff (
        name TEXT PRIMARY KEY, signal_id INTEGER, direction TEXT, active INTEGER NOT NULL,
        updated_at TEXT NOT NULL, payload TEXT NOT NULL)''')
    return c


def fresh(timestamp, seconds=30, now=None):
    try:
        age = ((now or now_ist()) - datetime.fromisoformat(timestamp)).total_seconds()
        return 0 <= age <= seconds
    except (TypeError, ValueError):
        return False


def publish(name, direction, tier, score, payload):
    """Keep one durable signal ID until the confirmed direction ends, even on restart."""
    active = tier == 'CONFIRMED' and direction in ('CE', 'PE') and math.isfinite(score) and score >= 70
    encoded = json.dumps(payload, allow_nan=False)
    if len(encoded) > 200_000:
        raise ValueError('Stock payload too large')
    now = now_ist().isoformat()
    c = connection()
    with c:
        old = c.execute('SELECT * FROM stock_handoff WHERE name=?', (name,)).fetchone()
        sid = old['signal_id'] if old else None
        same = old and old['signal_id'] is not None and old['direction'] == direction and old['updated_at'][:10] == now[:10]
        if active and not same:
            sid = c.execute('INSERT INTO signals (ts,index_name,tier,score,direction,payload,kind) '
                            'VALUES (?,?,?,?,?,?,?)',
                            (now,name,tier,score,direction,encoded,'gamma_blast')).lastrowid
        c.execute('INSERT INTO stock_handoff VALUES (?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET '
                  'signal_id=excluded.signal_id,direction=excluded.direction,active=excluded.active,'
                  'updated_at=excluded.updated_at,payload=excluded.payload',
                  (name,sid,direction if active else None,int(active),now,encoded))
    return sid if active else None


def invalidate(name, end_episode=False):
    c = connection()
    with c:
        c.execute('UPDATE stock_handoff SET active=0 WHERE name=?', (name,))
        if end_episode:
            c.execute('UPDATE stock_handoff SET direction=NULL WHERE name=?', (name,))


def candidates():
    rows = connection().execute('SELECT * FROM stock_handoff WHERE active=1').fetchall()
    result = []
    for row in rows:
        if fresh(row['updated_at']):
            data = json.loads(row['payload'])
            result.append(dict(row) | {'data': data})
    return sorted(result, key=lambda r: r['data'].get('score', 0), reverse=True)


def still_current(name, sid, timestamp):
    row = connection().execute('SELECT * FROM stock_handoff WHERE name=?', (name,)).fetchone()
    return bool(row and row['active'] and row['signal_id'] == sid
                and row['updated_at'] == timestamp and fresh(timestamp))
