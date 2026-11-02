"""
Gamma entry/exit timing module -- Phase 1: RECORD ONLY.

Writes one JSON line per Gamma entry evaluation to
DATA_DIR/timing/{day}.jsonl, so the after-market outcome job
(timing_outcome.py) can measure, for every Gamma signal -- traded,
rejected or missed -- how deep the premium dipped after the entry moment
(MAE), how far it ran (MFE) and how long it took to peak.

HARD RULES FOR THIS MODULE
    * Observe only. Nothing here changes a trading decision, a strike, a
      stop, a size or an order. It never calls the broker or the network.
    * It must never break the trading path. Every public function
      swallows its own exceptions and reports them once to the audit log.
    * It adds no network calls: it only reads the chain quotes the engine
      has already polled this cycle.

Design reference: "KADIMA Gamma Entry/Exit Timing Module -- Design"
(Claude Docs), sections "Data records" and "Rollout phases" (phase 1).
"""
from __future__ import annotations

import json
import os
import statistics
import threading
import time
from datetime import datetime
from typing import Any, Optional

from . import audit
from .config import DATA_DIR, IST

TIMING_DIR = os.path.join(DATA_DIR, "timing")
SCHEMA_VERSION = "timing-record-v1"

_lock = threading.Lock()
_handle = None
_handle_day: Optional[str] = None
_seen_signals: set[int] = set()
_choices: dict[int, dict] = {}       # signal_id -> contract chosen this call
_error_logged = False


# ------------------------------------------------------------------ writing
def _enabled() -> bool:
    from . import config
    return bool(getattr(config, "TIMING_RECORDER_ENABLED", True))


def _file_for(now: datetime):
    global _handle, _handle_day, _seen_signals
    day = now.strftime("%Y-%m-%d")
    if _handle_day != day:
        os.makedirs(TIMING_DIR, exist_ok=True)
        if _handle:
            _handle.close()
        _handle = open(os.path.join(TIMING_DIR, f"{day}.jsonl"), "a", buffering=1)
        _handle_day = day
        _seen_signals = set()
    return _handle


def _write(record: dict) -> None:
    now = datetime.now(IST)
    record = {"ts": now.isoformat(), "schema": SCHEMA_VERSION, **record}
    line = json.dumps(record, default=str, separators=(",", ":"))
    with _lock:
        fh = _file_for(now)
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _report_error(where: str, exc: Exception) -> None:
    """Log the first failure only, so a broken recorder can't flood the audit log."""
    global _error_logged
    if _error_logged:
        return
    _error_logged = True
    try:
        audit.log("timing_recorder_error", where=where,
                  error_type=type(exc).__name__, error=str(exc)[:200])
    except Exception:                                   # noqa: BLE001
        pass


# ------------------------------------------------------------------ helpers
def _num(value) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def quote_fields(q) -> dict:
    """The contract snapshot we keep for every evaluation."""
    ltp, bid, ask = _num(getattr(q, "ltp", None)), _num(getattr(q, "bid", None)), _num(getattr(q, "ask", None))
    spread_pct = None
    if bid and ask and ask >= bid and (bid + ask) > 0:
        spread_pct = round((ask - bid) / ((ask + bid) / 2) * 100, 3)
    return {
        "token": str(getattr(q, "token", "")),
        "symbol": getattr(q, "symbol", None),
        "strike": _num(getattr(q, "strike", None)),
        "opt_type": getattr(q, "opt_type", None),
        "ltp": ltp, "bid": bid, "ask": ask, "spread_pct": spread_pct,
        "iv": _num(getattr(q, "iv", None)),
        "delta": _num(getattr(q, "delta", None)),
        "oi": getattr(q, "oi", None),
        "oi_change": getattr(q, "oi_change", None),
        "volume": getattr(q, "volume", None),
    }


def atm_reference(chain, direction: str, spot: float) -> Optional[Any]:
    """Nearest-to-spot contract on the signal's side. Used only when the
    executor rejected before choosing a strike, so the outcome job still
    has a contract to measure the missed move on. It is a measuring
    reference, never an input to strike selection."""
    try:
        quotes = [q for q in chain.quotes.values()
                  if getattr(q, "opt_type", None) == direction and _num(getattr(q, "ltp", None))]
        if not quotes or not spot:
            return None
        return min(quotes, key=lambda q: abs(float(q.strike) - float(spot)))
    except Exception:                                   # noqa: BLE001
        return None


def _age(mono_attr) -> Optional[float]:
    try:
        if not mono_attr:
            return None
        return round(time.monotonic() - float(mono_attr), 2)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ public API
def note_choice(signal_id, kind: str, quote, stop_price=None) -> None:
    """Called by the executor right after its (unchanged) strike choice.
    Stores the chosen contract for the evaluation record written after
    the entry decision returns."""
    if kind != "gamma_blast" or not _enabled():
        return
    try:
        snap = quote_fields(quote)
        snap["stop_price"] = _num(stop_price)
        with _lock:
            _choices[int(signal_id)] = snap
    except Exception as exc:                            # noqa: BLE001
        _report_error("note_choice", exc)


def record_evaluation(*, signal_id, kind: str, index_name: str, direction: str,
                      tier: str, score, spot, chain, snap=None,
                      result: Optional[dict] = None, source: str = "engine",
                      candidate_id=None) -> None:
    """One line per Gamma entry evaluation (every OI poll while the
    signal is live, ~10-15 s). Safe to call from the trading path."""
    if kind != "gamma_blast" or not _enabled():
        return
    try:
        sid = int(signal_id)
        with _lock:
            chosen = _choices.pop(sid, None)
        contract, contract_source = chosen, "executor_choice"
        if contract is None:
            ref = atm_reference(chain, direction, _num(spot) or 0)
            contract = quote_fields(ref) if ref is not None else None
            contract_source = "atm_reference" if ref is not None else None

        first = False
        with _lock:
            _file_for(datetime.now(IST))            # rolls the day set if needed
            if sid not in _seen_signals:
                _seen_signals.add(sid)
                first = True

        result = result or {}
        vwap = _num(getattr(snap, "vwap", None)) if snap is not None else None
        common = {
            "signal_id": sid, "index": index_name, "direction": direction,
            "tier": tier, "score": _num(score), "source": source,
            "candidate_id": candidate_id,
        }
        if first:
            _write({"type": "timing_signal", **common, "spot": _num(spot), "vwap": vwap,
                    "opt_exchange": getattr(getattr(chain, "index", None), "opt_exchange", None)})
        _write({
            "type": "timing_eval", **common,
            "spot": _num(spot), "vwap": vwap,
            "decision_ok": bool(result.get("ok")),
            "decision_reason": result.get("reason"),
            "order_id": result.get("order_id"),
            "contract_source": contract_source,
            "contract": contract,
            "chain_age_sec": _age(getattr(chain, "_last_poll", None)),
            "spot_age_sec": _age(getattr(chain, "_spot_received_at", None)),
        })
    except Exception as exc:                            # noqa: BLE001
        _report_error("record_evaluation", exc)


def median_spread_pct(evals: list[dict], token: str) -> Optional[float]:
    """Helper shared with the outcome job."""
    vals = [e["contract"]["spread_pct"] for e in evals
            if e.get("contract") and e["contract"].get("token") == token
            and e["contract"].get("spread_pct") is not None]
    return statistics.median(vals) if vals else None


def close() -> None:
    global _handle, _handle_day
    with _lock:
        if _handle:
            _handle.close()
        _handle, _handle_day = None, None
