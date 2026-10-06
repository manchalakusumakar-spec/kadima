"""Evidence views. No healthy-looking inference from missing or stale fields."""
from datetime import datetime
import json
from pathlib import Path
from typing import Literal
from .collector import IST

Strategy = Literal["cas", "cas_legacy", "gamma", "index_scalp", "stock_fast", "move_capture"]
FLAGS = {"cas": "KADIMA_CAS_LIVE", "cas_legacy": "KADIMA_CAS_EXECUTION",
         "index_scalp": "KADIMA_INDEX_SCALP_LIVE", "stock_fast": "KADIMA_FAST_MOMENTUM_LIVE",
         "move_capture": "KADIMA_MOVE_CAPTURE_LIVE"}
KINDS = {"cas": {"cas"}, "cas_legacy": {"cas_blast"}, "gamma": {"gamma", "gamma_blast"},
         "index_scalp": {"index_scalp"}, "stock_fast": {"stock_fast"}, "move_capture": {"move_capture"}}
MAX_AGE = 90


def age_seconds(stamp, now):
    try:
        then = datetime.fromisoformat(stamp)
        if then.tzinfo is None:
            return None
        return (now - then).total_seconds()
    except (TypeError, ValueError):
        return None


class Evidence:
    def __init__(self, path, clock=None):
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(IST))

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
