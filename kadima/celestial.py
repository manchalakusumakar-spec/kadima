"""Durable Celestial control plane and the single, fail-closed Top 20 view.

No broker transport lives here. Scores are heuristics, never probabilities.
Activation is a separate server rollout flag; installing code does not arm it.
"""
import json
import math
import os
from datetime import datetime, timedelta

from . import storage
from .utils import now_ist

THRESHOLDS = (65, 70, 75, 80, 85, 90)
DEFAULTS = dict(auto_trade_enabled=False, score_threshold=65, dashboard_limit=20,
                mode="AUTONOMOUS_GAMMA_BLAST", kill_latched=False,
                adopt_external_positions=True, revision=0)


def enabled():
    return os.environ.get("KADIMA_CELESTIAL_ENABLED", "0") == "1"


def finite(value, minimum=0, maximum=float("inf")):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and minimum <= value <= maximum)


def fresh(value, now=None, seconds=30):
    try:
        stamp = datetime.fromisoformat(value)
        return stamp.tzinfo is not None and 0 <= ((now or now_ist()) - stamp).total_seconds() <= seconds
    except (ValueError, TypeError):
        return False


def connection():
    c = storage.conn()
    c.executescript('''
    CREATE TABLE IF NOT EXISTS celestial_config (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS celestial_candidates (
        id TEXT PRIMARY KEY, producer TEXT NOT NULL, ts TEXT NOT NULL, payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS celestial_news (
        id TEXT PRIMARY KEY, published_at TEXT NOT NULL, expires_at TEXT NOT NULL, payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS celestial_health (name TEXT PRIMARY KEY, ts TEXT NOT NULL, payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS celestial_rules (signal_id INTEGER PRIMARY KEY, retain REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS celestial_episodes (
        candidate_id TEXT NOT NULL, day TEXT NOT NULL, signal_id INTEGER NOT NULL,
        PRIMARY KEY(candidate_id,day));
    ''')
    return c


def read_config():
    row = connection().execute("SELECT payload FROM celestial_config WHERE id=1").fetchone()
    cfg = DEFAULTS.copy() if row is None else json.loads(row[0])
    return validate_config(cfg)


def validate_config(cfg):
    if (not isinstance(cfg,dict) or set(cfg) != set(DEFAULTS) or type(cfg['score_threshold']) is not int
            or cfg['score_threshold'] not in THRESHOLDS or cfg['dashboard_limit'] != 20
            or cfg['mode'] != DEFAULTS['mode'] or type(cfg['revision']) is not int
            or cfg['revision'] < 0
            or any(type(cfg[k]) is not bool for k in
                   ('auto_trade_enabled', 'kill_latched', 'adopt_external_positions'))):
        raise ValueError('Invalid persistent config; entry disabled')
    return cfg


def update_config(patch, *, expected_revision=None, reset_kill=False):
    allowed = {'auto_trade_enabled', 'score_threshold', 'adopt_external_positions'}
    if not isinstance(patch, dict) or set(patch) - allowed:
        raise ValueError('Unknown or immutable config field')
    if expected_revision is not None and (type(expected_revision) is not int or expected_revision<0):
        raise ValueError('Invalid revision')
    c = connection()
    c.execute('BEGIN IMMEDIATE')
    try:
        cfg = read_config_without_schema(c)
        if expected_revision is not None and expected_revision != cfg['revision']:
            raise ValueError('Config changed; refresh before updating')
        if 'score_threshold' in patch and (type(patch['score_threshold']) is not int
                                            or patch['score_threshold'] not in THRESHOLDS):
            raise ValueError('Threshold must be one of 65,70,75,80,85,90')
        if any(type(v) is not bool for k, v in patch.items() if k != 'score_threshold'):
            raise ValueError('Boolean settings require true or false')
        if cfg['kill_latched'] and patch.get('auto_trade_enabled'):
            raise ValueError('Kill is latched; flat-account reset required')
        if patch.get('auto_trade_enabled') and not enabled():
            raise ValueError('Celestial rollout flag is not enabled')
        cfg.update(patch)
        if reset_kill:
            cfg['kill_latched'] = False
        cfg['revision'] += 1
        c.execute('INSERT OR REPLACE INTO celestial_config VALUES (1,?)', (json.dumps(cfg),))
        c.commit()
        return cfg
    except Exception:
        c.rollback()
        raise


def read_config_without_schema(c):
    row = c.execute('SELECT payload FROM celestial_config WHERE id=1').fetchone()
    # Validate before starting a transaction in public handlers as well.
    cfg = DEFAULTS.copy() if row is None else json.loads(row[0])
    return validate_config(cfg)


def latch_kill():
    c = connection()
    c.execute('BEGIN IMMEDIATE')
    try:
        cfg = read_config_without_schema(c)
        cfg.update(kill_latched=True, auto_trade_enabled=False, revision=cfg['revision'] + 1)
        c.execute('INSERT OR REPLACE INTO celestial_config VALUES (1,?)', (json.dumps(cfg),))
        c.commit()
        return cfg
    except Exception:
        c.rollback()
        raise


def health(name, payload=None, timestamp=None):
    c = connection()
    if payload is not None:
        with c:
            c.execute('INSERT OR REPLACE INTO celestial_health VALUES (?,?,?)',
                      (name, timestamp or now_ist().isoformat(), json.dumps(payload, allow_nan=False)))
    row = c.execute('SELECT ts,payload FROM celestial_health WHERE name=?', (name,)).fetchone()
    return None if row is None else dict(ts=row[0], **json.loads(row[1]))


def publish(producer, rows):
    if len(rows) > 100:
        raise ValueError('Bounded chain producer exceeded')
    c = connection()
    encoded = []
    for row in rows:
        if row['id'] != row['exchange'] + ':' + row['token']:
            raise ValueError('Contract identity mismatch')
        encoded.append((row['id'], producer, row['last_updated'], json.dumps(row, allow_nan=False)))
    with c:
        c.execute('DELETE FROM celestial_candidates WHERE producer=?', (producer,))
        c.execute('DELETE FROM celestial_candidates WHERE ts < ?',
                  ((now_ist() - timedelta(days=1)).isoformat(),))
        c.executemany('INSERT OR REPLACE INTO celestial_candidates VALUES (?,?,?,?)', encoded)
    from . import trade_audit
    trade_audit.safe_call(trade_audit.record_gamma, producer, rows)


def news_impact(symbol, direction, now=None):
    now = now or now_ist()
    h = health('news')
    available = bool(h and h.get('ok') is True and fresh(h['ts'], now, 300)
                     and symbol in h.get('covered_symbols',[]))
    events = []
    for r in connection().execute('SELECT payload FROM celestial_news'):
        e = json.loads(r[0])
        if symbol not in e['symbols'] and '*' not in e['symbols']:
            continue
        start, end = map(datetime.fromisoformat, (e['published_at'], e['expires_at']))
        if start <= now < end:
            events.append(e)
    # Duplicate stories for one event cannot stack. Strongest active score wins.
    score = max((e['score'] for e in events), key=abs, default=0)
    return dict(news_score=score if direction == 'CE' else -score,
                news_available=available, news_blocked=any(e['block'] for e in events),
                news_sources=[e['url'] for e in events][:5])


def ranked_candidates(now=None, *, manual=False, candidates=None):
    """Score source rows before display deduplication and the twenty-row limit.

    Audit capture supplies the bounded published batch so another producer
    cannot replace that snapshot before its qualifying leads are recorded.
    """
    now = now or now_ist()
    cfg = read_config()
    if candidates is None:
        candidates = (json.loads(record[0]) for record in
                      connection().execute('SELECT payload FROM celestial_candidates'))
    rows = []
    for candidate in candidates:
        row = dict(candidate)
        if (not fresh(row.get('last_updated'), now) or not finite(row.get('premium_move_pct'), 5)
                or not finite(row.get('gamma_score'), 0, 100)):
            continue
        row.update(news_impact(row['underlying'], row['direction'], now))
        score = row['gamma_score'] + row['news_score'] + row.get('sector_score', 0) - row.get('event_risk_penalty', 0)
        row['final_score'] = round(max(0, min(100, score)), 2)
        reasons = []
        if not enabled(): reasons.append('rollout_disabled')
        if not cfg['auto_trade_enabled'] and not manual: reasons.append('auto_paused')
        if cfg['kill_latched']: reasons.append('kill_latched')
        if row['final_score'] < cfg['score_threshold']: reasons.append('below_threshold')
        if row.get('volume_spike') is not True: reasons.append('volume_unconfirmed')
        if row.get('oi_support') is not True: reasons.append('oi_unconfirmed')
        if row.get('spread_status') != 'CLEAN': reasons.append('spread_not_clean')
        if row.get('tradeable') is not True: reasons.append('contract_not_tradeable')
        if row.get('direction_clear') is not True: reasons.append('direction_unconfirmed')
        from .config import GAMMA_INDICES
        from .index_event_gate import index_event_block
        if row['underlying'] in GAMMA_INDICES:
            block = index_event_block(row['underlying'], now)
            if block and block != 'news_risk_block': reasons.append(block)
        elif not row['news_available']: reasons.append('news_coverage_unavailable')
        if row['news_blocked']: reasons.append('news_risk_block')
        if not fresh(row.get('spot_at'), now): reasons.append('underlying_stale')
        row['eligible'] = not reasons
        row['blocked_reasons'] = reasons
        row['action'] = 'READY' if row['eligible'] else 'RADAR'
        row['tier'] = ('BLAST' if score >= 90 else 'CONFIRMED' if score >= 85 else
                       'ACTIVE' if score >= 70 else 'WATCH' if score >= 40 else 'RADAR')
        if row['final_score'] >= 40:
            rows.append(row)
    rows.sort(key=lambda r: (r['priority'], -r['final_score'],
                            0 if r['spread_status'] == 'CLEAN' else 1, -r['premium_move_pct'], r['id']))
    return rows


def top20(now=None, *, manual=False):
    rows = ranked_candidates(now, manual=manual)
    # One best strike per underlying/direction; no padding to twenty weak rows.
    selected, used = [], set()
    for row in rows:
        key = row['underlying'], row['direction']
        if key not in used:
            used.add(key)
            row['rank'] = len(selected) + 1
            selected.append(row)
            if len(selected) == 20:
                break
    return selected


def candidate(candidate_id, *, manual=False):
    return next((r for r in top20(manual=manual) if r['id'] == candidate_id), None)


def entry_candidate(underlying, direction, *, manual=False, candidate_id=None):
    return next((r for r in top20(manual=manual) if r['underlying'] == underlying
                 and r['direction'] == direction and r['eligible']
                 and (candidate_id is None or r['id'] == candidate_id)), None)


def retain_fraction(signal_id):
    row = connection().execute('SELECT retain FROM celestial_rules WHERE signal_id=?', (signal_id,)).fetchone()
    return row[0] if row else .5


def signal_for(row):
    """One durable entry opportunity per contract/day; restart is not a new signal."""
    c = connection()
    day = now_ist().date().isoformat()
    c.execute('BEGIN IMMEDIATE')
    try:
        saved = c.execute('SELECT signal_id FROM celestial_episodes WHERE candidate_id=? AND day=?',
                          (row['id'],day)).fetchone()
        if saved:
            c.commit()
            return saved[0]
        sid = c.execute('INSERT INTO signals (ts,index_name,tier,score,direction,payload,kind) '
                        'VALUES (?,?,?,?,?,?,?)', (now_ist().isoformat(),row['underlying'],row['tier'],
                        row['final_score'],row['direction'],json.dumps(row),'gamma_blast')).lastrowid
        c.execute('INSERT INTO celestial_episodes VALUES (?,?,?)',(row['id'],day,sid))
        c.commit()
        return sid
    except Exception:
        c.rollback()
        raise


def set_retain(signal_id, value):
    if not finite(value, .5, .95):
        raise ValueError('Retain must be between 0.50 and 0.95')
    c = connection()
    with c:
        c.execute('INSERT INTO celestial_rules VALUES (?,?) ON CONFLICT(signal_id) '
                  'DO UPDATE SET retain=MAX(retain,excluded.retain)', (signal_id, value))


def trail_stop(entry, peak, previous, structural, retain=.5):
    if not all(finite(v, .000001) for v in (entry, peak, structural)):
        raise ValueError('Missing protection inputs')
    gain = peak / entry - 1
    active = gain >= .05 - 1e-12
    fraction = max(retain, .8 if gain >= .75 else .75 if gain >= .35 else
                   .7 if gain >= .2 else .6 if gain >= .1 else .5)
    stop = entry + (peak-entry) * fraction if active else structural
    return max(previous or 0, structural, stop), active
