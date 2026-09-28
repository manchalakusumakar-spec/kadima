"""
Gamma timing -- after-market outcome job (phase 1, record only).

For every Gamma signal recorded by timing_recorder.py today, measures the
premium path after the entry moment and writes one `timing_outcome` line
per signal to DATA_DIR/timing/outcomes/{day}.jsonl (rewritten each run,
so re-running is safe).

Run after the F&O close, e.g. 15:50 IST on weekdays:

    python -m kadima.timing_outcome                  # today
    python -m kadima.timing_outcome --day 2026-09-29

WHAT IT MEASURES (all % of the entry price, per signal)
    mae_pct          deepest dip below entry until the end of the window
    mae_before_mfe   deepest dip before the peak was reached -- the dip
                     you had to sit through to get paid
    mfe_pct          highest gain reached in the window
    t_mfe_min        minutes from entry to that peak
    stop_touched     did the path reach the stop recorded at choice time
    realised_pct     traded signals only: actual exit vs actual entry
    capture_ratio    realised_pct / mfe_pct (traded, mfe > 0)
    missed_mfe_pct   not traded: what the move offered from the first
                     evaluation's price

PRICE BASIS -- READ BEFORE TRUSTING THE NUMBERS
    Entry uses the real fill when traded, otherwise the ask recorded at
    the first evaluation (LTP + half the median spread if no ask).
    The path uses LTP from oi_snapshots (~10-15 s apart) minus half the
    median recorded spread, as an approximation of an executable bid.
    It is not tick data and not depth-aware. Each record carries
    `price_basis`, `path_points` and `max_gap_sec` so thin paths are visible.

It opens the database read-only and never calls the broker.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, time as dtime
from typing import Optional

from .config import DATA_DIR, DB_PATH, IST

TIMING_DIR = os.path.join(DATA_DIR, "timing")
OUTCOME_DIR = os.path.join(TIMING_DIR, "outcomes")
WINDOW_END = dtime(15, 38)          # auto_executor.EOD_FORCE_CLOSE_HHMM
DEFAULT_SPREAD_PCT = 1.0            # used only when no spread was ever recorded
COST_RESERVE_PCT = 1.0              # trade_policy.COST_RESERVE, round trip
SCHEMA_VERSION = "timing-outcome-v1"


# ------------------------------------------------------------------ loading
def load_records(day: str, timing_dir: str = TIMING_DIR) -> list[dict]:
    path = os.path.join(timing_dir, f"{day}.jsonl")
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue            # a torn last line after a crash
    return out


def _db(db_path: str) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def _tables(c) -> set[str]:
    return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def load_trade(c, signal_id: int) -> Optional[dict]:
    """Actual fills for a signal from execution_ledger, if any."""
    if not {"execution_orders", "execution_fills"} <= _tables(c):
        return None
    rows = c.execute(
        "SELECT e.side, f.quantity, f.price, f.ts, e.dry_run FROM execution_fills f "
        "JOIN execution_orders e ON e.intent=f.intent WHERE e.signal_id=? ORDER BY f.ts",
        (signal_id,)).fetchall()
    buys = [r for r in rows if r["side"] == "BUY"]
    if not buys:
        return None
    sells = [r for r in rows if r["side"] == "SELL"]
    bq = sum(r["quantity"] for r in buys)
    trade = {
        "entry_price": sum(r["quantity"] * r["price"] for r in buys) / bq,
        "t_entry": buys[0]["ts"], "quantity": bq,
        "dry_run": bool(buys[0]["dry_run"]),
        "exit_price": None, "t_exit": None, "exit_reason": None,
        "token": None, "structural_stop": None,
    }
    if sells:
        sq = sum(r["quantity"] for r in sells)
        trade["exit_price"] = sum(r["quantity"] * r["price"] for r in sells) / sq
        trade["t_exit"] = sells[-1]["ts"]
    if "positions" in _tables(c):
        cols = {r[1] for r in c.execute("PRAGMA table_info(positions)")}
        want = [x for x in ("token", "exit_reason", "structural_stop") if x in cols]
        if want:
            p = c.execute(f"SELECT {','.join(want)} FROM positions WHERE signal_id=?",
                          (signal_id,)).fetchone()
            if p:
                for k in want:
                    trade[k] = p[k]
    return trade


def load_path(c, token: str, start: str, end: str) -> list[tuple[str, float]]:
    if "oi_snapshots" not in _tables(c):
        return []
    rows = c.execute(
        "SELECT ts, ltp FROM oi_snapshots WHERE token=? AND ts>=? AND ts<=? "
        "AND ltp IS NOT NULL AND ltp>0 ORDER BY ts", (token, start, end)).fetchall()
    return [(r["ts"], float(r["ltp"])) for r in rows]


# ------------------------------------------------------------------ maths
def _minutes(a: str, b: str) -> float:
    return round((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 60, 2)


def path_metrics(entry: float, path: list[tuple[str, float]], t_entry: str,
                 stop: Optional[float] = None) -> dict:
    """path: [(iso_ts, executable_price)] from entry onward. Pure function."""
    if not path or not entry or entry <= 0:
        return {"path_points": len(path)}
    prices = [p for _, p in path]
    i_max = max(range(len(prices)), key=lambda i: prices[i])
    i_min = min(range(len(prices)), key=lambda i: prices[i])
    before_peak = prices[: i_max + 1]
    gaps = [(datetime.fromisoformat(path[i][0]) - datetime.fromisoformat(path[i - 1][0])).total_seconds()
            for i in range(1, len(path))]
    pct = lambda p: round((p - entry) / entry * 100, 2)          # noqa: E731
    out = {
        "mfe_pct": pct(prices[i_max]),
        "t_mfe_min": _minutes(t_entry, path[i_max][0]),
        "mae_pct": round(max(0.0, -pct(prices[i_min])), 2),
        "t_mae_min": _minutes(t_entry, path[i_min][0]),
        "mae_before_mfe_pct": round(max(0.0, -pct(min(before_peak))), 2),
        "path_points": len(path),
        "max_gap_sec": round(max(gaps), 1) if gaps else None,
    }
    if stop and stop > 0:
        hit = next((i for i, p in enumerate(prices) if p <= stop), None)
        out["stop_price"] = round(stop, 2)
        out["stop_touched"] = hit is not None
        out["stop_touched_before_mfe"] = hit is not None and hit <= i_max
        out["t_stop_min"] = _minutes(t_entry, path[hit][0]) if hit is not None else None
    return out


# ------------------------------------------------------------------ per signal
def outcome_for_signal(sid: int, recs: list[dict], c, day: str) -> dict:
    sig = next((r for r in recs if r.get("type") == "timing_signal"), None)
    evals = [r for r in recs if r.get("type") == "timing_eval"]
    base = sig or (evals[0] if evals else {})
    out = {
        "type": "timing_outcome", "schema": SCHEMA_VERSION, "day": day,
        "signal_id": sid, "index": base.get("index"), "direction": base.get("direction"),
        "source": base.get("source"),
        "first_eval": evals[0]["ts"] if evals else None,
        "last_eval": evals[-1]["ts"] if evals else None,
        "evaluations": len(evals),
        "reject_reasons": dict(Counter(e.get("decision_reason") for e in evals
                                       if not e.get("decision_ok"))),
    }
    trade = load_trade(c, sid) if c is not None else None
    chosen = [e for e in evals if e.get("contract_source") == "executor_choice" and e.get("contract")]
    any_contract = [e for e in evals if e.get("contract")]
    pick = (chosen or any_contract or [None])[0]
    contract = pick["contract"] if pick else None
    token = (trade or {}).get("token") or (contract or {}).get("token")
    if not token:
        out.update(status="no_contract_recorded")
        return out
    if contract and contract.get("token") != token:
        contract = next((e["contract"] for e in evals
                         if e.get("contract") and e["contract"].get("token") == token), None)
    out.update(token=str(token), symbol=(contract or {}).get("symbol"),
               strike=(contract or {}).get("strike"),
               contract_source=pick.get("contract_source") if pick else "position")

    spreads = [e["contract"]["spread_pct"] for e in evals
               if e.get("contract") and e["contract"].get("token") == token
               and e["contract"].get("spread_pct") is not None]
    spread_pct = statistics.median(spreads) if spreads else DEFAULT_SPREAD_PCT
    half = spread_pct / 200.0
    out["median_spread_pct"] = round(spread_pct, 3)

    end_of_day = datetime.combine(datetime.fromisoformat(out["first_eval"] or f"{day}T09:15:00+05:30").date(),
                                  WINDOW_END, IST).isoformat()
    stop = (trade or {}).get("structural_stop") or (contract or {}).get("stop_price")

    if trade:
        out["status"] = "traded"
        out["dry_run"] = trade["dry_run"]
        entry, t_entry = trade["entry_price"], trade["t_entry"]
        t_end = trade["t_exit"] or end_of_day
        out.update(entry_price=round(entry, 2), t_entry=t_entry,
                   exit_price=round(trade["exit_price"], 2) if trade["exit_price"] else None,
                   t_exit=trade["t_exit"], exit_reason=trade.get("exit_reason"),
                   price_basis="fill_entry; path=ltp_minus_half_spread")
    else:
        out["status"] = "not_traded"
        first = next((e for e in evals if e.get("contract") and e["contract"].get("token") == token), None)
        c0 = (first or {}).get("contract") or {}
        entry = c0.get("ask") or ((c0.get("ltp") or 0) * (1 + half)) or None
        t_entry = (first or {}).get("ts") or out["first_eval"]
        t_end = end_of_day
        out.update(entry_price=round(entry, 2) if entry else None, t_entry=t_entry,
                   price_basis=("first_eval_ask" if c0.get("ask") else "first_eval_ltp_plus_half_spread")
                   + "; path=ltp_minus_half_spread")

    if not entry or not t_entry:
        out["status"] += "_no_entry_price"
        return out

    raw = load_path(c, str(token), t_entry, t_end) if c is not None else []
    path = [(ts, p * (1 - half)) for ts, p in raw]
    out.update(path_metrics(entry, path, t_entry, stop))
    out["window_end"] = t_end

    if trade and trade["exit_price"]:
        realised = (trade["exit_price"] - entry) / entry * 100
        out["realised_pct"] = round(realised, 2)
        out["net_realised_pct_est"] = round(realised - COST_RESERVE_PCT, 2)
        out["holding_min"] = _minutes(t_entry, trade["t_exit"])
        if out.get("mfe_pct") and out["mfe_pct"] > 0:
            out["capture_ratio"] = round(realised / out["mfe_pct"], 3)
        # What was left after the exit: rest-of-session peak.
        after = [(ts, p * (1 - half)) for ts, p in
                 load_path(c, str(token), trade["t_exit"], end_of_day)]
        if after:
            out["post_exit_peak_pct"] = round((max(p for _, p in after) - entry) / entry * 100, 2)
    elif not trade:
        out["missed_mfe_pct"] = out.get("mfe_pct")
    return out


# ------------------------------------------------------------------ run
def run(day: str, timing_dir: str = TIMING_DIR, db_path: str = DB_PATH,
        outcome_dir: str = OUTCOME_DIR) -> list[dict]:
    records = load_records(day, timing_dir)
    by_signal: dict[int, list[dict]] = defaultdict(list)
    for r in records:
        if r.get("signal_id") is not None:
            by_signal[int(r["signal_id"])].append(r)
    c = _db(db_path) if os.path.exists(db_path) else None
    try:
        outcomes = [outcome_for_signal(sid, recs, c, day) for sid, recs in sorted(by_signal.items())]
    finally:
        if c is not None:
            c.close()
    os.makedirs(outcome_dir, exist_ok=True)
    path = os.path.join(outcome_dir, f"{day}.jsonl")
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        for o in outcomes:
            fh.write(json.dumps(o, default=str, separators=(",", ":")) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return outcomes


def summary(outcomes: list[dict]) -> str:
    if not outcomes:
        return "No Gamma signals recorded."
    lines = [f"{'signal':>7} {'index':<10} {'dir':<3} {'status':<11} {'MAE%':>6} "
             f"{'MAEpre%':>7} {'MFE%':>6} {'tMFE':>6} {'real%':>6} {'capt':>5} stop"]
    for o in outcomes:
        f = lambda k, w, d=1: (f"{o[k]:>{w}.{d}f}" if isinstance(o.get(k), (int, float)) else f"{'-':>{w}}")  # noqa: E731
        lines.append(f"{o['signal_id']:>7} {str(o.get('index')):<10} {str(o.get('direction')):<3} "
                     f"{o.get('status', '')[:11]:<11} {f('mae_pct', 6)} {f('mae_before_mfe_pct', 7)} "
                     f"{f('mfe_pct', 6)} {f('t_mfe_min', 6)} {f('realised_pct', 6)} "
                     f"{f('capture_ratio', 5, 2)} {'hit' if o.get('stop_touched') else ('-' if 'stop_touched' not in o else 'no')}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--day", default=datetime.now(IST).strftime("%Y-%m-%d"))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    outcomes = run(args.day)
    if not args.quiet:
        print(summary(outcomes))
    try:
        from . import audit
        audit.log("timing_outcome_job_done", day=args.day, signals=len(outcomes))
    except Exception:                                   # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
