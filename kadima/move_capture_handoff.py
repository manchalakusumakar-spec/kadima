"""Durable Move Capture handoff: spot service publishes, engine worker consumes.

This module contains no broker calls. A live order can only be submitted by the
central AutoExecutor after option confirmation and all existing execution gates.
"""
from __future__ import annotations

from datetime import datetime
import json
import math

from . import storage
from .utils import now_ist

MAX_PAYLOAD = 100_000
FRESH_SECONDS = 8


def connection():
    c = storage.conn()
    c.execute('''CREATE TABLE IF NOT EXISTS move_capture_handoff (
        name TEXT PRIMARY KEY,
        episode TEXT NOT NULL,
        direction TEXT NOT NULL,
        active INTEGER NOT NULL,
        updated_at TEXT NOT NULL,
        payload TEXT NOT NULL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS move_capture_episodes (
        episode TEXT PRIMARY KEY,
        signal_id INTEGER NOT NULL
    )''')
    return c


def fresh(timestamp, *, seconds=FRESH_SECONDS, now=None):
    try:
        stamp = datetime.fromisoformat(timestamp)
        current = now or now_ist()
        return stamp.tzinfo is not None and 0 <= (current - stamp).total_seconds() <= seconds
    except (TypeError, ValueError):
        return False


def publish(candidate):
    if not isinstance(candidate, dict):
        raise ValueError("move_capture_candidate_invalid")
    name = candidate.get("underlying")
    episode = candidate.get("episode")
    direction = candidate.get("direction")
    score = candidate.get("score")
    observed = candidate.get("observed_at")
    if (not isinstance(name, str) or not name
            or not isinstance(episode, str) or not episode
            or direction not in ("CE", "PE")
            or type(score) not in (int, float) or not math.isfinite(score)
            or not 0 <= score <= 100
            or not fresh(observed, seconds=30)):
        raise ValueError("move_capture_candidate_invalid")
    encoded = json.dumps(candidate, allow_nan=False, separators=(",", ":"))
    if len(encoded) > MAX_PAYLOAD:
        raise ValueError("move_capture_payload_too_large")
    now = now_ist().isoformat()
    c = connection()
    with c:
        c.execute('''INSERT INTO move_capture_handoff
                     (name,episode,direction,active,updated_at,payload)
                     VALUES (?,?,?,?,?,?)
                     ON CONFLICT(name) DO UPDATE SET
                       episode=excluded.episode,
                       direction=excluded.direction,
                       active=excluded.active,
                       updated_at=excluded.updated_at,
                       payload=excluded.payload''',
                  (name, episode, direction, 1, now, encoded))
    return now


def invalidate(name):
    if not isinstance(name, str) or not name:
        return
    c = connection()
    with c:
        c.execute("UPDATE move_capture_handoff SET active=0 WHERE name=?", (name,))


def candidates(now=None):
    current = now or now_ist()
    rows = connection().execute(
        "SELECT * FROM move_capture_handoff WHERE active=1").fetchall()
    result = []
    for raw in rows:
        row = dict(raw)
        if not fresh(row.get("updated_at"), now=current):
            continue
        try:
            data = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if (data.get("episode") != row["episode"]
                or data.get("direction") != row["direction"]
                or data.get("underlying") != row["name"]):
            continue
        result.append(row | {"data": data})
    return sorted(result, key=lambda r: (-float(r["data"].get("score", 0)), r["name"]))


def current(name):
    row = connection().execute(
        "SELECT * FROM move_capture_handoff WHERE name=?", (name,)).fetchone()
    if not row:
        return None
    row = dict(row)
    if not row.get("active") or not fresh(row.get("updated_at")):
        return None
    try:
        data = json.loads(row["payload"])
    except (TypeError, ValueError):
        return None
    return row | {"data": data}


def still_current(name, episode, direction):
    row = current(name)
    return bool(row and row["episode"] == episode and row["direction"] == direction)


def signal_id(candidate):
    """Stable KADIMA signal ID for one Move Capture episode."""
    episode = candidate.get("episode") if isinstance(candidate, dict) else None
    if not isinstance(episode, str) or not episode:
        raise ValueError("move_capture_episode_missing")
    c = connection()
    with c:
        old = c.execute(
            "SELECT signal_id FROM move_capture_episodes WHERE episode=?",
            (episode,)).fetchone()
        if old:
            return int(old[0])
        cur = c.execute(
            '''INSERT INTO signals
               (ts,index_name,tier,score,direction,payload,kind)
               VALUES (?,?,?,?,?,?,?)''',
            (now_ist().isoformat(), candidate["underlying"], "MOVE_CAPTURE",
             float(candidate["score"]), candidate["direction"],
             json.dumps(candidate, allow_nan=False), "move_capture"))
        sid = int(cur.lastrowid)
        c.execute(
            "INSERT INTO move_capture_episodes(episode,signal_id) VALUES (?,?)",
            (episode, sid))
        return sid
