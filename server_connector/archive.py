"""Incremental, sanitized copy of recent audit days for the read-only connector.

Runs inside the collector (root, read-only view of the engine) once per cycle.
It never imports KADIMA, never edits the engine's audit files, and only writes
under ARCHIVE_DIR. Each run reads a small byte budget (smaller during market hours), so a large
backlog is copied gradually instead of loading the engine host.

What leaves this module is the same allowlisted, redacted projection used for
the live snapshot, widened only by ARCHIVE_EXTRA (diagnostic fields with no
credential meaning). Exception messages, raw broker responses, request bodies,
tokens and free-form payloads stay out.
"""
from datetime import date, timedelta
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from .collector import SAFE_FIELDS, clean

SOURCE_DIR = Path("/var/lib/kadima/audit")
ARCHIVE_DIR = Path("/var/lib/kadima-connector/days")
STATE_NAME = ".state.json"
RETAIN_DAYS = 10
# Source bytes read per collector run (~every 30 s). The host is a burstable
# t3-class instance: keep CPU near zero while the engine trades, catch up after.
MARKET_BUDGET = 1_000_000          # 09:00-15:45 IST on weekdays
OFF_HOURS_BUDGET = 4_000_000
DAY_CAP = 25_000_000               # sanitized bytes kept per day
MAX_LINE = 65_536                  # longer source records are skipped
MIN_FREE_BYTES = 1_500_000_000     # stop archiving below 1.5 GB free ...
MIN_FREE_FRACTION = 0.10           # ... or below 10% free
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Diagnostic fields seen in KADIMA audit calls. Deliberately absent: error
# (exception text), raw, response, response_keys, body_snippet, text, token,
# tokens, key, client.
ARCHIVE_EXTRA = frozenset("""
accepted activation_price adverse_streak age_seconds agreement allowed_lots
already_deployed atm attempt backoff_sec broker_code buffer_pct
candidates_examined capital capped_to cash_committed checked checks closed_at
components contracts cost_inr current_premium currently_tracked delta
entry_premium error_type exch exits_paused expected fallback_price gain_pct
gap_warning indices intent interval limit loss_pct lot_size lots ltp
max_risk_inr mismatched_contracts missing n name new_price new_stop new_value
note old_price open_positions option_type owned_qty passed peak_price pending
pending_orders pnl_rupees premium_change_pct premium_run_30min_pct
previous_stop price range_position raw_score realized_pnl_today received
rejected_count rejections requested requested_lots restored retries risk_inr
root rows rr shortlisted should_exit silence_sec spot spread_pct stop
stop_basis stop_pct stop_price stop_risk_committed strike strike_step
subscriptions success swing_level symbols tier trailing_active uncertain
underlying_ext_pct variety volume volume_avg wait_sec adopted anchor at_step
band_pts cap change day decision deviation_from entry episode filled kind
ladder late live_requested mode modification n_eff peak prob_up projected_pct
qty quote_at quote_calls selected signals slot spot_change stage step
still_monitored tags underlying where
""".split())
ARCHIVE_FIELDS = frozenset(SAFE_FIELDS) | ARCHIVE_EXTRA


def _valid_day(name):
    if not DAY_RE.match(name or ""):
        return False
    try:
        date.fromisoformat(name)
        return True
    except ValueError:
        return False


def _read_state(directory):
    try:
        with (directory / STATE_NAME).open("rb") as stream:
            raw = stream.read(200_001)
        data = json.loads(raw) if len(raw) <= 200_000 else {}
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if _valid_day(k) and isinstance(v, dict)}


def _write_state(directory, state):
    fd, name = tempfile.mkstemp(prefix=".state-", dir=directory)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o640)
            json.dump(state, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, directory / STATE_NAME)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _disk_ok(directory):
    usage = shutil.disk_usage(directory)
    return usage.free >= MIN_FREE_BYTES and usage.free >= usage.total * MIN_FREE_FRACTION


def _append(path, text):
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o640)
    try:
        os.write(fd, text.encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def _copy_new_lines(source, offset, budget):
    """Return (sanitized_lines, new_offset, bytes_consumed, skipped)."""
    out, consumed, skipped = [], 0, 0
    with source.open("rb") as stream:
        stream.seek(offset)
        while consumed < budget:
            line = stream.readline(MAX_LINE + 1)
            if not line:
                break
            if not line.endswith(b"\n"):
                if len(line) <= MAX_LINE:
                    break  # Record still being written; pick it up next run.
                consumed += len(line)  # Oversized record: skip to its end.
                while True:
                    rest = stream.readline(MAX_LINE + 1)
                    consumed += len(rest)
                    if not rest or rest.endswith(b"\n"):
                        break
                skipped += 1
                continue
            consumed += len(line)
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                skipped += 1
                continue
            if not isinstance(row, dict) or not isinstance(row.get("event"), str):
                skipped += 1
                continue
            out.append(json.dumps(clean(row, fields=ARCHIVE_FIELDS),
                                  separators=(",", ":"), allow_nan=False) + "\n")
    return out, offset + consumed, consumed, skipped


def _prune(directory, state, today):
    oldest = today - timedelta(days=RETAIN_DAYS - 1)
    for entry in directory.iterdir():
        stem = entry.name[:-6] if entry.name.endswith(".jsonl") else None
        if stem and _valid_day(stem) and date.fromisoformat(stem) < oldest:
            if entry.is_file() and not entry.is_symlink():
                entry.unlink()
    for day in [d for d in state if date.fromisoformat(d) < oldest]:
        del state[day]


def run_budget(now):
    minutes = now.hour * 60 + now.minute
    if now.weekday() < 5 and 9 * 60 <= minutes <= 15 * 60 + 45:
        return MARKET_BUDGET
    return OFF_HOURS_BUDGET


def run(now, source_dir=None, archive_dir=None):
    source_dir = Path(source_dir or SOURCE_DIR)
    directory = Path(archive_dir or ARCHIVE_DIR)
    if not source_dir.is_dir():
        return {"ok": False, "error": "audit_source_unavailable", "days": {}}
    if directory.is_symlink():
        return {"ok": False, "error": "archive_dir_symlink_rejected", "days": {}}
    directory.mkdir(mode=0o750, exist_ok=True)
    directory.chmod(0o750)
    today = now.date()
    state = _read_state(directory)
    _prune(directory, state, today)
    paused = None if _disk_ok(directory) else "low_disk"
    budget = run_budget(now)
    # Today first for freshness, then backfill older days with what is left.
    for back in range(RETAIN_DAYS):
        day = (today - timedelta(days=back)).isoformat()
        source = source_dir / (day + ".jsonl")
        try:
            meta = source.lstat()
        except OSError:
            continue
        if not source.is_file() or source.is_symlink():
            continue
        entry = state.setdefault(day, {"offset": 0, "bytes": 0, "capped": False,
                                       "skipped": 0})
        target = directory / (day + ".jsonl")
        if meta.st_size < entry.get("offset", 0):
            # Source shrank or was replaced: rebuild that day from the start.
            if target.is_file() and not target.is_symlink():
                target.unlink()
            entry.update(offset=0, bytes=0, capped=False, skipped=0)
        entry["source_bytes"] = meta.st_size
        if paused or entry.get("capped") or budget <= 0:
            continue
        if entry["offset"] >= meta.st_size:
            continue
        lines, offset, used, skipped = _copy_new_lines(source, entry["offset"], budget)
        budget -= used
        text = "".join(lines)
        if entry["bytes"] + len(text) > DAY_CAP:
            room = DAY_CAP - entry["bytes"]
            kept, size = [], 0
            for line in lines:
                if size + len(line) > room:
                    break
                kept.append(line)
                size += len(line)
            text = "".join(kept) + json.dumps({"event": "connector_archive_capped",
                                               "note": "daily sanitized size cap reached"}) + "\n"
            entry["capped"] = True
        if text:
            _append(target, text)
        entry["bytes"] += len(text)
        entry["offset"] = offset
        entry["skipped"] = entry.get("skipped", 0) + skipped
    _write_state(directory, state)
    days = {}
    for day, entry in sorted(state.items()):
        days[day] = {"source_bytes": entry.get("source_bytes"),
                     "archived_bytes": entry.get("bytes"),
                     "caught_up": entry.get("offset", 0) >= (entry.get("source_bytes") or 0),
                     "capped": bool(entry.get("capped")),
                     "skipped_records": entry.get("skipped", 0)}
    return {"ok": True, "paused": paused, "retain_days": RETAIN_DAYS, "days": days}
