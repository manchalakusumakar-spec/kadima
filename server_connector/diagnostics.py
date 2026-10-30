"""Evidence views. No healthy-looking inference from missing or stale fields."""
from collections import Counter, deque
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import re
from typing import Literal
from .collector import IST

Strategy = Literal["cas", "cas_legacy", "gamma", "index_scalp", "stock_fast", "move_capture"]
FLAGS = {"cas": "KADIMA_CAS_LIVE", "cas_legacy": "KADIMA_CAS_EXECUTION",
         "index_scalp": "KADIMA_INDEX_SCALP_LIVE", "stock_fast": "KADIMA_FAST_MOMENTUM_LIVE",
         "move_capture": "KADIMA_MOVE_CAPTURE_LIVE"}
KINDS = {"cas": {"cas"}, "cas_legacy": {"cas_blast"}, "gamma": {"gamma", "gamma_blast"},
         "index_scalp": {"index_scalp"}, "stock_fast": {"stock_fast"}, "move_capture": {"move_capture"}}
MAX_AGE = 90
ARCHIVE_RETAIN_DAYS = 10
SCAN_CAP = 30_000_000              # bytes read per archive query
EVENT_FILTER = re.compile(r"^[a-z0-9_]{1,64}\*?$")
# Event-name fragments that mark a refusal, block or failure. Grouping uses the
# event's own name and reason; strategy is never inferred from symbol or time.
REFUSAL_HINTS = ("reject", "block", "refus", "skip", "denied", "disabled", "unfilled",
                 "failed", "not_granted", "paused", "capped", "exhausted", "unavailable")


def age_seconds(stamp, now):
    try:
        then = datetime.fromisoformat(stamp)
        if then.tzinfo is None:
            return None
        return (now - then).total_seconds()
    except (TypeError, ValueError):
        return None


class Evidence:
    def __init__(self, path, clock=None, archive_dir=None):
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(IST))
        self.archive_dir = Path(archive_dir) if archive_dir else self.path.parent / "days"

    def load(self):
        now = self.clock()
        try:
            with self.path.open("rb") as stream:
                raw = stream.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError("too_large")
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get("schema") != 1:
                raise ValueError("schema")
        except (OSError, ValueError):
            return {"snapshot_status": "unavailable", "checked_at": now.isoformat()}
        age = age_seconds(data.get("observed_at"), now)
        data["snapshot_age_seconds"] = age
        data["snapshot_status"] = "fresh" if age is not None and 0 <= age <= MAX_AGE else "stale"
        data["checked_at"] = now.isoformat()
        return data

    @staticmethod
    def envelope(data):
        return {k: data.get(k) for k in ("checked_at", "observed_at", "completed_at",
                "snapshot_status", "snapshot_age_seconds")}

    def server(self):
        data = self.load()
        return dict(self.envelope(data), services=data.get("services", {}),
                    resources=data.get("resources", {}),
                    source=data.get("source", {}), process_flags=data.get("process_flags", {}),
                    api_reads={k: {"ok": v.get("ok"), "error": v.get("error")}
                               for k, v in data.get("api", {}).items()},
                    capability="Read-only diagnostics. No shell, deploy, restart, configuration or broker actions.")

    def readiness(self, strategy: Strategy):
        if strategy not in KINDS:
            raise ValueError("unsupported_strategy")
        data = self.load()
        api = data.get("api", {})
        top = api.get("top20", {}).get("data", {})
        desk = api.get("desk", {}).get("data", {})
        flags = data.get("process_flags", {})
        cfg = top.get("config", {})
        blocks = []
        if data["snapshot_status"] != "fresh":
            blocks.append("diagnostic_snapshot_" + data["snapshot_status"])
        now = self.clock()
        top_age = age_seconds(top.get("ts"), now)
        if top_age is None or not 0 <= top_age <= MAX_AGE:
            blocks.append("configuration_snapshot_unverified")
        if cfg.get("kill_latched") is True:
            blocks.append("kill_latched")
        # Legacy CAS can have different master controls; present shared values
        # without asserting that they gate it until the deployed path is traced.
        keys = ("auto_trade_enabled", "broker_orders_enabled", "rollout_enabled") if strategy != "cas_legacy" else ()
        for key in keys:
            if cfg.get(key) is False:
                blocks.append(key + "_false")
        flag = FLAGS.get(strategy)
        requested = flags.get(flag) if flag else None
        if requested is False:
            blocks.append("strategy_live_flag_false")
        # Never transfer a denial from one strategy to another.
        specific = {}
        if strategy == "cas":
            specific = {"priority_api": api.get("priority", {}), "cas_panel": desk.get("cas", {}),
                        "candidates": desk.get("cas_candidates", [])}
        elif strategy == "cas_legacy":
            specific = {"runtime_status_endpoint": "not_available_in_reviewed_source",
                        "note": "The newer cas panel/priority endpoint does not prove legacy cas_blast readiness."}
        elif strategy in ("stock_fast", "move_capture"):
            specific = {"stock_api": api.get("stock", {})}
        return dict(self.envelope(data), strategy=strategy,
            readiness="BLOCKED" if blocks else "UNVERIFIED", observed_blocks=blocks,
            requested_flag=flag, live_requested=requested,
            simultaneous_cas_flags=(flags.get("KADIMA_CAS_LIVE") is True
                                    and flags.get("KADIMA_CAS_EXECUTION") is True),
            global_config=cfg, shared_health=top.get("system", {}),
            strategy_evidence=specific,
            required_live_checks=["Strategy-specific current permit and entry window",
                "Fresh broker reconciliation, cash and exposure", "Executable quotes and sizing",
                "Loaded source version and exit protection"],
            note="Shared health is evidence, not proof that each strategy uses the same gates. Flags, candidates and empty position lists do not prove readiness or broker flatness.")

    def execution(self, strategy="all", limit=20):
        if strategy not in (*KINDS, "all") or isinstance(limit, bool) or not 1 <= limit <= 50:
            raise ValueError("invalid_filter")
        data = self.load()
        desk = data.get("api", {}).get("desk", {}).get("data", {})
        positions = desk.get("positions", [])
        audit = data.get("audit", {}).get("rows", [])
        # Only explicit kinds/strategy are attribution evidence, never symbol or clock time.
        attribution = {}
        conflicts = set()
        for row in positions + audit:
            kind = row.get("kind") or row.get("strategy")
            signal = row.get("signal_id")
            if signal is None or not isinstance(kind, str):
                continue
            signal = str(signal)
            if signal in attribution and attribution[signal] != kind:
                conflicts.add(signal)
            attribution[signal] = kind
        def tagged(row):
            result = dict(row)
            signal = str(row.get("signal_id"))
            result["attributed_kind"] = (None if signal in conflicts else
                row.get("kind") or row.get("strategy") or attribution.get(signal))
            return result
        def select(rows):
            rows = [tagged(r) for r in rows]
            if strategy != "all":
                rows = [r for r in rows if r["attributed_kind"] in KINDS[strategy]]
            return rows[-limit:]
        return dict(self.envelope(data), strategy=strategy, positions=select(positions),
                    ledger_rows=select(desk.get("execution_progress", [])),
                    audit_events=select(audit),
                    evidence_scope="Bounded API ledger and audit tail, not a complete broker book or session.",
                    note="Missing strategy labels remain unattributed. Local ledger fills are reported evidence, not a fresh independent broker verification.")

    def diagnostics(self, limit=20):
        if isinstance(limit, bool) or not 1 <= limit <= 50:
            raise ValueError("invalid_limit")
        data = self.load()
        desk = data.get("api", {}).get("desk", {}).get("data", {})
        return dict(self.envelope(data), entry_diagnostics=desk.get("entry_diagnostics", [])[-limit:],
                    audit=data.get("audit", {}).get("rows", [])[-limit:],
                    audit_read_ok=data.get("audit", {}).get("ok"),
                    note="Unlabelled lane_permit_not_granted events must not be attributed to CAS.")

    # ---------------------------------------------------------- audit history
    def _archive_status(self):
        data = self.load()
        archive = data.get("archive") if isinstance(data.get("archive"), dict) else {}
        days = archive.get("days") if isinstance(archive.get("days"), dict) else {}
        return data, archive, days

    def _day_path(self, day):
        if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise ValueError("date_must_be_YYYY-MM-DD")
        try:
            wanted = date.fromisoformat(day)
        except ValueError:
            raise ValueError("date_must_be_YYYY-MM-DD") from None
        today = self.clock().date()
        if not today - timedelta(days=ARCHIVE_RETAIN_DAYS - 1) <= wanted <= today:
            raise ValueError("date_outside_retained_window")
        path = self.archive_dir / (wanted.isoformat() + ".jsonl")
        if path.is_symlink():
            raise ValueError("archive_entry_rejected")
        return path

    def _rows(self, path):
        """Stream archived records in file order, bounded by SCAN_CAP bytes."""
        read = 0
        truncated = False
        try:
            with path.open("rb") as stream:
                for line in stream:
                    read += len(line)
                    if read > SCAN_CAP:
                        truncated = True
                        break
                    try:
                        row = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if isinstance(row, dict):
                        yield row
        except OSError:
            return
        if truncated:
            yield {"event": "connector_scan_cap_reached"}

    def _day_envelope(self, day, days, archive):
        status = days.get(day, {})
        return {"date": day, "archive_ok": archive.get("ok"),
                "archive_paused": archive.get("paused"),
                "day_status": status or "not_archived",
                "complete": bool(status.get("caught_up")) and not status.get("capped"),
                "note": "Sanitized, allowlisted copy of the engine audit log. Exception text, "
                        "raw broker responses and credentials are excluded. Incomplete days "
                        "are still being copied or hit the size cap."}

    def audit_days(self):
        data, archive, days = self._archive_status()
        listed = []
        for day in sorted(days, reverse=True):
            info = days[day] if isinstance(days[day], dict) else {}
            listed.append({"date": day, "archived_bytes": info.get("archived_bytes"),
                           "source_bytes": info.get("source_bytes"),
                           "complete": bool(info.get("caught_up")) and not info.get("capped"),
                           "capped": info.get("capped")})
        return dict(self.envelope(data), archive_ok=archive.get("ok"),
                    archive_paused=archive.get("paused"),
                    archive_error=archive.get("error"), days=listed)

    def audit_search(self, day, contains="", event="", limit=50):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("invalid_limit")
        if not isinstance(contains, str) or len(contains) > 80:
            raise ValueError("contains_must_be_text_up_to_80_chars")
        if not isinstance(event, str) or (event and not EVENT_FILTER.match(event)):
            raise ValueError("event_must_be_lowercase_name_optionally_ending_in_star")
        path = self._day_path(day)
        data, archive, days = self._archive_status()
        needle = contains.lower()
        prefix = event[:-1] if event.endswith("*") else None
        matches, total, scanned, capped = deque(maxlen=limit), 0, 0, False
        for row in self._rows(path):
            if row.get("event") == "connector_scan_cap_reached":
                capped = True
                break
            scanned += 1
            name = row.get("event", "")
            if event and not (name.startswith(prefix) if prefix is not None else name == event):
                continue
            if needle and needle not in json.dumps(row).lower():
                continue
            total += 1
            matches.append(row)
        return dict(self.envelope(data), **self._day_envelope(day, days, archive),
                    filters={"contains": contains, "event": event},
                    records_scanned=scanned, total_matches=total,
                    returned=len(matches), scan_cap_reached=capped,
                    matches=list(matches),
                    order="Most recent matches, oldest first within the returned window.")

    def rejection_summary(self, day, event_prefix="", top=40):
        if isinstance(top, bool) or not isinstance(top, int) or not 1 <= top <= 100:
            raise ValueError("invalid_top")
        if not isinstance(event_prefix, str) or (event_prefix and not re.fullmatch(r"[a-z0-9_]{1,64}", event_prefix)):
            raise ValueError("event_prefix_must_be_lowercase_name")
        path = self._day_path(day)
        data, archive, days = self._archive_status()
        groups, first, last, symbols = Counter(), {}, {}, {}
        block_reasons = Counter()
        scanned, capped = 0, False
        for row in self._rows(path):
            if row.get("event") == "connector_scan_cap_reached":
                capped = True
                break
            scanned += 1
            name = row.get("event", "")
            if event_prefix and not name.startswith(event_prefix):
                continue
            reasons = row.get("blocked_reasons")
            if isinstance(reasons, list):
                for reason in reasons[:20]:
                    if isinstance(reason, str):
                        block_reasons[(name, reason)] += 1
            if not any(hint in name for hint in REFUSAL_HINTS):
                continue
            reason = row.get("reason") if isinstance(row.get("reason"), str) else None
            key = (name, reason, row.get("kind") if isinstance(row.get("kind"), str) else None)
            groups[key] += 1
            stamp = row.get("ts")
            first.setdefault(key, stamp)
            last[key] = stamp
            symbol = row.get("symbol")
            if isinstance(symbol, str):
                seen = symbols.setdefault(key, [])
                if symbol not in seen and len(seen) < 3:
                    seen.append(symbol)
        rows = [{"event": k[0], "reason": k[1], "kind": k[2], "count": n,
                 "first_ts": first.get(k), "last_ts": last.get(k),
                 "sample_symbols": symbols.get(k, [])}
                for k, n in groups.most_common(top)]
        reasons = [{"event": k[0], "blocked_reason": k[1], "count": n}
                   for k, n in block_reasons.most_common(top)]
        return dict(self.envelope(data), **self._day_envelope(day, days, archive),
                    event_prefix=event_prefix, records_scanned=scanned,
                    scan_cap_reached=capped, refusal_groups=rows,
                    blocked_reason_counts=reasons,
                    attribution_note="Grouped by the event's own name, reason and explicit kind only. "
                         "An event without a kind is not attributed to any strategy.")
