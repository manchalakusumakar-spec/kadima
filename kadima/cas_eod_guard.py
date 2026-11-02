"""Which broker positions eod_watchdog may close (2026-09-29, CAS deployment).

KADIMA's CAS trades are CARRYFORWARD. The watchdog must:
  * never touch carry-forward positions KADIMA does not own (the trader's
    manual carries, e.g. MCX), and
  * leave KADIMA's own CAS carry-forward positions to the engine's 15:38 EOD
    close, unless it runs at/after CAS_WATCHDOG_FROM (schedule a 15:39 run).
INTRADAY positions are returned unchanged -- the watchdog's original job.
Fails safe: if KADIMA's own CAS positions cannot be read, no carry-forward
position is closed and an alert is sent.
"""
from __future__ import annotations

from . import alerts, audit

CAS_WATCHDOG_FROM = (15, 39)


def owned_cas_contracts() -> set[tuple[str, str]] | None:
    try:
        from . import storage
        from .angel_orders import CAS_KIND
        return {(str(r["exchange"]), str(r["token"]))
                for r in storage.load_open_positions() if r.get("kind") == CAS_KIND}
    except Exception as exc:                        # noqa: BLE001
        audit.log("eod_watchdog_cas_ownership_unreadable", error=str(exc))
        return None


def to_close(open_positions: list[dict], now) -> list[dict]:
    owned = None
    late = (now.hour, now.minute) >= CAS_WATCHDOG_FROM
    out = []
    for pos in open_positions:
        if str(pos.get("producttype") or "").upper() != "CARRYFORWARD":
            out.append(pos)
            continue
        if owned is None:
            owned = owned_cas_contracts()
            if owned is None:
                alerts.send("\U0001F6A8 EOD WATCHDOG: could not read KADIMA's CAS positions; "
                            "carry-forward positions NOT touched. Check broker now.", bypass=True)
                owned = set()
        key = (str(pos.get("exchange")), str(pos.get("symboltoken")))
        if key not in owned:
            audit.log("eod_watchdog_skip_manual_carryforward", symbol=pos.get("tradingsymbol"))
        elif not late:
            audit.log("eod_watchdog_cas_left_for_engine_eod", symbol=pos.get("tradingsymbol"))
        else:
            out.append(pos)
    return out
