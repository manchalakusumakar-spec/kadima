"""
Append-only event log. Every tick batch, signal, API call, error and state
change lands here. One file per day, JSON lines, fsync'd on write.

This exists from day one deliberately: an audit log switched on later has no
operating history, and the first time you need it is the first time something
went wrong.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime

from .config import AUDIT_PATH, IST

_lock = threading.Lock()
_handle = None
_handle_date = None


def _file_for(dt: datetime):
    global _handle, _handle_date
    day = dt.strftime("%Y-%m-%d")
    if _handle_date != day:
        os.makedirs(AUDIT_PATH, exist_ok=True)
        if _handle:
            _handle.close()
        _handle = open(os.path.join(AUDIT_PATH, f"{day}.jsonl"), "a", buffering=1)
        _handle_date = day
    return _handle


def log(event: str, **fields) -> None:
    """Write one immutable event record."""
    now = datetime.now(IST)
    record = {"ts": now.isoformat(), "event": event, **fields}
    line = json.dumps(record, default=str, separators=(",", ":"))
    with _lock:
        fh = _file_for(now)
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def close() -> None:
    global _handle, _handle_date
    with _lock:
        if _handle:
            _handle.close()
        _handle, _handle_date = None, None
