"""Fixed local reads only. Never import KADIMA, call a broker, or change controls."""
import ast
from collections import deque
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
API = "http://127.0.0.1:8081"
ROUTES = {
    "top20": "/api/kadima/top20",
    "desk": "/dashboard_data",
    "stock": "/api/kadima/stock-fast",
    "priority": "/api/kadima/priority/status",
}
REPO = Path("/home/ubuntu/kadima-py")
OUTPUT = Path("/var/lib/kadima-connector/snapshot.json")
SERVICES = ("kadima.service", "kadima-universe-scanner.service",
            "kadima-owned-watchdog.service", "kadima-session-auto.service")
FLAGS = ("KADIMA_CAS_LIVE", "KADIMA_CAS_EXECUTION", "KADIMA_PRIORITY_TRADING",
         "KADIMA_FAST_MOMENTUM_LIVE", "KADIMA_INDEX_SCALP_LIVE",
         "KADIMA_MOVE_CAPTURE_LIVE")
SOURCE_FILES = ("kadima/control_api.py", "kadima/cas_engine.py", "kadima/cas_config.py",
                "kadima/cas_executor.py", "kadima/angel_orders.py", "kadima/engine.py")
# Dynamic text is restricted to these fields and then length/redaction bounded.
SAFE_FIELDS = set("""ts event index index_name underlying symbol direction score stage
eligible blocked_reasons reason signal_id kind strategy source status remaining_qty
filled_owned_quantity entry_price effective_stop exit_reason side quantity order_id
state filled created_at dry_run ok covered_symbols event_count publisher_at
publisher_age_hours scope auto_trade_enabled kill_latched rollout_enabled
broker_orders_enabled revision live_enabled holding_minutes hold_minutes session_exit
worker_alive fast_master_permission broker_order_permission celestial_rollout
global_auto_requested total_observed_rows display_limit mode release priority_requested
cas_live_requested mcx_live_requested equity_last_entry equity_force_exit
mcx_last_entry mcx_force_exit calendar_through cas checked_at observed_at quote_at
fresh_roots warmed_roots unresolved external_pending_orders managed unmanaged
realized_gain_today_inr capital_deployed_inr capital_equity_inr capital_ceiling_inr
daily_loss_used_inr daily_loss_limit_inr position_slots_used position_slots_max
sources errors categories count active pending active_count pending_count
stock_count index_count last_scan_at last_error worker_thread_alive
""".split())
HEALTH_NAMES = ("news", "reconciliation", "index_universe", "stock_universe", "mcx_live")
POLICY_NAMES = set("""CAS_KIND CAS_PRODUCT_TYPE CAS_ENTRY_START CAS_ENTRY_END CAS_MAX_CAPITAL_INR
CAS_MAX_TRADES_PER_DAY CAS_MIN_PREMIUM CAS_MAX_PREMIUM CAS_MAX_SPREAD_PCT CAS_STRIKES_OTM
CAS_TRIGGER_PREMIUM_PCT CAS_TRIGGER_POLLS CAS_TRIGGER_VOLUME_MULT CAS_VOLUME_TRAILING_POLLS
CAS_ENTRY_PRICE_CAP_MULT CAS_ENTRY_STEP_SEC CAS_ENTRY_MAX_STEPS CAS_TRAIL_ARM_PCT
CAS_TRAIL_GIVEBACK_PCT CAS_EXIT_WATCH_SEC CAS_MAX_ORDER_QTY max_capital max_premium
min_premium max_spread_pct min_velocity_pct max_chase_multiple add_multiple max_add_multiple
stop_fraction entry_end force_exit""".split())
SECRET = re.compile(r"(?i)(bearer\s+\S+|(?:password|api[_-]?key|api[_-]?token|access[_-]?token|refresh[_-]?token|authorization)\s*[:=]\s*[^\s,;]+|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)")


def clean(value, depth=0):
    """Never pass raw payloads, request headers, environment or exception messages."""
    if depth > 7:
        return "[depth limit]"
    if isinstance(value, dict):
        return {k: clean(v, depth + 1) for k, v in value.items() if k in SAFE_FIELDS}
    if isinstance(value, list):
        return [clean(v, depth + 1) for v in value[:100]]
    if isinstance(value, str):
        return SECRET.sub("[redacted]", value[:500])
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def get_local(name, token):
    if name not in ROUTES:
        raise ValueError("endpoint_not_allowed")
    request = urllib.request.Request(API + ROUTES[name], method="GET",
        headers={"Authorization": "Bearer " + token})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=4) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("oversized")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("schema")
        return {"ok": True, "data": data}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": "http_" + str(exc.code)}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}


def command(args):
    # Args are fixed by this module, never supplied by an MCP caller.
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=3)
        return result.stdout[:16000] if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def service_state():
    states = {}
    for service in SERVICES:
        output = command(["systemctl", "show", service, "--no-pager", "-p", "ActiveState",
            "-p", "SubState", "-p", "MainPID", "-p", "ExecMainStatus",
            "-p", "ExecMainStartTimestamp"])
        states[service] = dict(line.split("=", 1) for line in (output or "").splitlines()
                               if "=" in line)
    return states


def resources():
    result = {}
    try:
        memory = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            if key in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
                memory[key + "_kib"] = int(value.split()[0])
        result["memory"] = memory
        disk = shutil.disk_usage("/")
        result["disk_bytes"] = {"total": disk.total, "used": disk.used, "free": disk.free}
        result["load_average_1_5_15m"] = list(os.getloadavg())
        result["cpu_count"] = os.cpu_count()
    except (OSError, ValueError):
        result["error"] = "resource_read_unavailable"
    return result


def process_flags(pid):
    if not str(pid).isdigit() or int(pid) <= 0:
        return {"error": "engine_not_running"}
    try:
        # Only exact boolean switches leave this function. All credentials discarded.
        env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        values = dict(item.split(b"=", 1) for item in env if b"=" in item)
        return {key: (values[key.encode()] == b"1") if key.encode() in values else None
                for key in FLAGS}
    except OSError:
        return {"error": "process_environment_unavailable"}


def disk_source():
    result = {"checkout_commit": (command(["git", "-C", str(REPO), "rev-parse", "HEAD"]) or "").strip(),
              "loaded_code_verified": False, "files": {}}
    for name in SOURCE_FILES:
        try:
            raw = (REPO / name).read_bytes()
            if len(raw) > 2_000_000:
                raise ValueError("oversized")
            result["files"][name] = {"sha256": hashlib.sha256(raw).hexdigest()}
            if name.endswith(("cas_config.py", "cas_engine.py", "angel_orders.py")):
                literals = {}
                tree = ast.parse(raw)
                nodes = list(tree.body)
                nodes += [n for c in tree.body if isinstance(c, ast.ClassDef) and c.name == "Policy" for n in c.body]
                for n in nodes:
                    target = (n.targets[0] if isinstance(n, ast.Assign) and len(n.targets) == 1
                              else n.target if isinstance(n, ast.AnnAssign) else None)
                    if not isinstance(target, ast.Name):
                        continue
                    if target.id not in POLICY_NAMES:
                        continue
                    try:
                        literals[target.id] = ast.literal_eval(n.value)
                    except (ValueError, TypeError, SyntaxError):
                        pass  # Never evaluate env reads, calls or expressions.
                result["files"][name]["literal_policy_on_disk"] = literals
        except Exception as exc:
            result["files"][name] = {"error": type(exc).__name__}
    return result


def audit_tail(now):
    path = Path("/var/lib/kadima/audit") / (now.date().isoformat() + ".jsonl")
    try:
        with path.open("rb") as stream:
            size = stream.seek(0, 2)
            stream.seek(max(0, size - 2_000_000))
            if size > 2_000_000:
                stream.readline()  # Discard truncated first record.
            lines = deque(stream, maxlen=400)
        rows = []
        for line in lines:
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(clean(row))
            except (ValueError, UnicodeError):
                pass
        return {"ok": True, "rows": rows[-100:], "bounded_tail": True,
                "complete_session": False}
    except OSError as exc:
        return {"ok": False, "error": type(exc).__name__, "rows": []}


def transform_api(name, data):
    if name == "top20":
        system = data.get("system") if isinstance(data.get("system"), dict) else {}
        health = {k: clean(system[k]) for k in HEALTH_NAMES if k in system}
        # News category names are dynamic keys; only these known categories pass.
        news = system.get("news") or {}
        if isinstance(news, dict) and isinstance(health.get("news"), dict):
            for field in ("sources", "errors"):
                value = news.get(field)
                if isinstance(value, dict):
                    health["news"][field] = {k: clean(value[k]) for k in
                        ("announcements", "board_meetings", "results", "company_mapping") if k in value}
        return {"ts": clean(data.get("ts")), "config": clean(data.get("config", {})), "system": health}
    if name == "desk":
        return {"ts": clean(data.get("ts")), "positions": clean(data.get("positions", [])),
                "session": clean(data.get("session", {})),
                "cas": clean(data.get("cas", {})),
                "cas_candidates": clean((data.get("cas") or {}).get("candidates", [])),
                "entry_diagnostics": clean(data.get("entry_diagnostics", [])),
                "execution_progress": clean(data.get("execution_progress", []))}
    if name == "stock":
        return {"summary": clean(data), "controls": clean(data.get("controls", {})),
                "worker": clean(data.get("worker", {})), "rows": clean(data.get("rows", []))}
    return clean(data)


def collect(token):
    now = datetime.now(IST)
    services = service_state()
    pid = services.get("kadima.service", {}).get("MainPID", "0")
    snapshot = {"schema": 1, "observed_at": now.isoformat(), "read_only": True,
                "services": services, "process_flags": process_flags(pid),
                "source": disk_source(), "resources": resources(), "api": {}, "audit": audit_tail(now)}
    for name in ROUTES:
        result = get_local(name, token)
        if result["ok"]:
            try:
                result["data"] = transform_api(name, result["data"])
            except (TypeError, ValueError, AttributeError):
                result = {"ok": False, "error": "unexpected_schema"}
        snapshot["api"][name] = result
    snapshot["completed_at"] = datetime.now(IST).isoformat()
    return snapshot


def write_snapshot(snapshot, destination=OUTPUT):
    # destination directory provisioned root:reader, 0750; file inherits collector group.
    fd, name = tempfile.mkstemp(prefix=".snapshot-", dir=destination.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o640)
            json.dump(snapshot, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, destination)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main():
    # systemd copies this one credential privately for the collector only.
    credential = Path(os.environ["CREDENTIALS_DIRECTORY"]) / "dashboard-config"
    token = json.loads(credential.read_text())["api_token"]
    if not isinstance(token, str) or not token:
        raise SystemExit("Local API credential missing")
    write_snapshot(collect(token))


if __name__ == "__main__":
    main()
