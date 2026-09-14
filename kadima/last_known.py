"""
last_known.py -- persists each index's most recent spot/score/tier/
direction so a freshly (re)started engine, or a dashboard checked outside
market hours, has real last-traded data to show instead of None/0/NONE
until the next live tick arrives.

Same atomic-write JSON pattern as capital_guard.py's capital_state.json --
one flat file (state/last_known.json), written from a single low-frequency
background thread (Engine._persist_last_known_loop, every 20s -- NOT on
every tick; ticks can arrive many times a second across 5 indices, and
fsync-ing on every one of them would be real, unnecessary disk pressure
on an already memory-constrained t3.micro-class box) and read once at
Engine startup to seed IndexState before the feed connects.

Purely informational: nothing here feeds back into trading decisions,
only into what /state (and the dashboard) shows when nothing live has
happened yet this process lifetime.
"""
from __future__ import annotations

import json
import os
import threading

STATE_PATH = os.path.expanduser("~/kadima-py/state/last_known.json")
_LOCK = threading.Lock()


def save_all(states: dict) -> None:
    """states: Engine.states (dict[str, IndexState]). Writes one combined
    snapshot for all indices in a single atomic write."""
    snapshot = {}
    for name, st in states.items():
        if st.spot is None:
            continue  # nothing real to persist for this index yet
        snapshot[name] = {
            "spot": st.spot,
            "score": st.score,
            "tier": st.tier,
            "direction": st.direction,
            "ts": (st.last_updated.isoformat() if st.last_updated else None),
        }
    if not snapshot:
        return
    with _LOCK:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snapshot, f, indent=2)
        os.replace(tmp, STATE_PATH)


def load_all() -> dict:
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
