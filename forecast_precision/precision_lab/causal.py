"""Tiny clock/identity helpers for the event observer; standard library only.

Keeping this independent of the NumPy-based candidate model is intentional:
a read-only market-shock detector should not import a numerical training stack
into the latency-sensitive KADIMA engine merely to validate timestamps.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import hashlib
import json

IST = timezone(timedelta(hours=5,minutes=30))

class EvidenceError(ValueError):
    """Invalid or temporally inconsistent observation."""


def instant(value: datetime | str) -> datetime:
    try:
        ts=datetime.fromisoformat(value) if isinstance(value,str) else value
        if not isinstance(ts,datetime) or ts.tzinfo is None or ts.utcoffset() is None:
            raise ValueError('timezone missing')
        return ts.astimezone(IST)
    except (TypeError, ValueError, OverflowError) as exc:
        raise EvidenceError('timezone-aware timestamp required') from exc


def canonical(payload) -> str:
    return json.dumps(payload,sort_keys=True,separators=(',',':'),allow_nan=False)


def digest(payload) -> str:
    return hashlib.sha256(canonical(payload).encode('utf-8')).hexdigest()
