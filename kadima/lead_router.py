"""AUTOTRADE UNBLOCK v1 - one route per underlying, chosen by engine priority.

Trader's flow (2026-10-10):
    leads (technical scan; news only adds symbols to the scan focus list)
      -> Top 20 shortlist (technical evidence only)
      -> engines: Gamma, Intraday (Bull/Bear Shield, Gap-and-Go), Scalping (fast momentum), CAS
      -> router: the highest-priority engine that accepted owns the underlying
      -> auto executor (safety gates only) -> order

Priority before 15:15: GAMMA > INTRADAY > SCALPING.
From 15:15 the INTRADAY-product engines stop taking new entries (Angel squares
off and blocks MIS orders around 15:20), so CAS, which has its own CARRYFORWARD
entry path and opens at 15:15, owns that window.

The router never places, sizes or prices anything and never reads news.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, time as dtime

from . import audit
from .utils import now_ist

# Intraday (Shield) entries no longer need a matching Gamma row in the Top 20.
SHIELD_NEEDS_GAMMA_ROW = False

# No new INTRADAY-product entries at/after this time on NFO/BFO (CAS window).
INTRADAY_ENTRY_CUTOFF = (15, 15)

# A higher-priority engine's acceptance holds the underlying this long, even if
# the executor then rejected it for a safety reason (fresh quote, capacity...).
ROUTE_HOLD_SEC = 30.0

# Flat stop used when no confirmed swing pivot exists yet (e.g. before ~09:40).
FALLBACK_STOP_PCT = 0.30

ENGINE_OF = {
    'gamma_blast': 'GAMMA',
    'bull_shield': 'INTRADAY',
    'bear_shield': 'INTRADAY',
    'gap_and_go': 'INTRADAY',
    'fast_momentum': 'SCALPING',
    'cas': 'CAS',
    'cas_blast': 'CAS',
}
PRIORITY = {'CAS': 0, 'GAMMA': 1, 'INTRADAY': 2, 'SCALPING': 3}

_lock = threading.Lock()
_claims: dict[str, tuple[str, float, object]] = {}   # underlying -> (engine, monotonic, signal_id)


def engine_of(kind) -> str | None:
    return ENGINE_OF.get(kind)


def past_intraday_entry_cutoff(exchange: str = 'NFO', now: datetime | None = None) -> bool:
    if exchange not in ('NFO', 'BFO'):
        return False      # MCX and other venues keep their own session rules
    now = now or now_ist()
    return now.time() >= dtime(*INTRADAY_ENTRY_CUTOFF)


def _open_owner(positions, underlying):
    for p in list((positions or {}).values()):
        if getattr(p, 'index', None) != underlying or getattr(p, 'status', None) == 'closed':
            continue
        if getattr(p, 'kind', None) in ('forecast_manual', 'MANUAL_ADOPTED'):
            continue   # human trades are not routes; existing contract checks still apply
        return engine_of(getattr(p, 'kind', None)) or str(getattr(p, 'kind', 'UNKNOWN')).upper()
    return None


def admit(kind, underlying, direction, positions, signal_id, clock: float | None = None):
    """Called once an engine has ACCEPTED a lead (its own strategy filters passed).

    Returns None when this engine owns the route, else a rejection reason.
    Kinds the router does not know (manual clicks, MCX, legacy) pass untouched.
    """
    engine = engine_of(kind)
    if engine is None:
        return None
    clock = time.monotonic() if clock is None else clock
    owner = _open_owner(positions, underlying)
    if owner is not None:
        return f'underlying_owned_by_{owner}'
    with _lock:
        held = _claims.get(underlying)
        if held and 0 <= clock - held[1] <= ROUTE_HOLD_SEC and held[0] != engine \
                and PRIORITY.get(held[0], 9) < PRIORITY.get(engine, 9):
            reason = f'routed_to_{held[0]}'
        else:
            reason = None
            if not held or held[0] != engine or held[2] != signal_id:
                audit.log('LEAD_ROUTED', underlying=underlying, direction=direction,
                          engine=engine, signal_id=signal_id,
                          replaced=held[0] if held and 0 <= clock - held[1] <= ROUTE_HOLD_SEC else None)
            _claims[underlying] = (engine, clock, signal_id)
    return reason


def routes(clock: float | None = None) -> dict:
    """Current live routes, for the dashboard."""
    clock = time.monotonic() if clock is None else clock
    with _lock:
        return {u: dict(engine=e, age_sec=round(clock - t, 1), signal_id=s)
                for u, (e, t, s) in _claims.items() if 0 <= clock - t <= ROUTE_HOLD_SEC}


def reset() -> None:
    with _lock:
        _claims.clear()
