"""
Kill switch.

Deliberately dumb and dependency-free: a file on disk. It must work when the
signal engine, the feed and the scorer have all failed, so it shares no code
with any of them.

    Engage:      touch /var/lib/kadima/KILL
    Release:     rm /var/lib/kadima/KILL
    From python: killswitch.engage("reason")

Every loop in the engine checks this before doing anything consequential.
"""
from __future__ import annotations

import os
from datetime import datetime

from . import audit
from .config import IST, KILL_SWITCH_FILE


def engaged() -> bool:
    return os.path.exists(KILL_SWITCH_FILE)


def engage(reason: str = "manual") -> None:
    os.makedirs(os.path.dirname(KILL_SWITCH_FILE), exist_ok=True)
    with open(KILL_SWITCH_FILE, "w") as fh:
        fh.write(f"{datetime.now(IST).isoformat()} {reason}\n")
    audit.log("kill_switch_engaged", reason=reason)


def release() -> None:
    if engaged():
        os.remove(KILL_SWITCH_FILE)
        audit.log("kill_switch_released")


def reason() -> str | None:
    if not engaged():
        return None
    with open(KILL_SWITCH_FILE) as fh:
        return fh.read().strip()
