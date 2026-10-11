#!/usr/bin/env python3
"""
KADIMA final fix: adds the missing get_capital_for_signal(kind, tier)
function to capital_guard.py, and fixes auto_executor.py's call site
to use it directly (fixed rupee bands, no percentage applied).

CONTEXT: auto_executor.py was already calling
capital_guard.get_capital_for_tier(tier) -- but that function was
never actually added to capital_guard.py. This was a live bug
(AttributeError on every entry attempt). This script fixes both
sides at once.

Bands (confirmed by Kusumakar, 2026-09-11):
    bull_shield: 25,000 - 30,000
    gamma_blast: 50,000 - 62,000
Used directly as max_risk_inr -- NOT a base with a percentage applied.
CONFIRMED and ACTIVE tiers share the same band. WATCH/unknown tier
returns 0.0 (blocks entry).

Safe by design: backs up both files before editing, aborts with NO
changes written if the auto_executor.py call site doesn't match
exactly what's expected.

USAGE (on the EC2 box, inside ~/kadima-py):
    python3 kadima_final_capital_fix.py
"""

import shutil
import sys

KADIMA_DIR = "/home/ubuntu/kadima-py/kadima"

NEW_FUNCTION = '''

def get_capital_for_signal(kind: str, tier: str) -> float:
    """Fixed rupee capital band per signal source, used directly as
    max_risk_inr (not a base to apply a percentage to).
    bull_shield: 25,000-30,000. gamma_blast: 50,000-62,000.
    WATCH/unknown tier or unknown kind returns 0.0."""
    BANDS = {
        "bull_shield": (25_000.0, 30_000.0),
        "gamma_blast": (50_000.0, 62_000.0),
    }
    band = BANDS.get(kind)
    if band is None or tier not in ("CONFIRMED", "ACTIVE"):
        return 0.0
    lo, hi = band
    try:
        SESSION.ensure()
        resp = SESSION.api.rmsLimit()
        data = (resp or {}).get("data") or {}
        net = float(data.get("net", 0.0))
    except Exception as exc:
        audit.log("capital_guard_signal_rms_exception", kind=kind, tier=tier, error=str(exc))
        return 0.0
    if net <= 0.0:
        return 0.0
    return min(max(net, lo), hi)
'''

AUTO_EXECUTOR_OLD = (
    "        max_risk_inr = capital_guard.get_capital_for_tier(tier) * 0.03\n"
)
AUTO_EXECUTOR_NEW = (
    "        max_risk_inr = capital_guard.get_capital_for_signal(kind, tier)\n"
)


def main() -> int:
    cg_path = f"{KADIMA_DIR}/capital_guard.py"
    ae_path = f"{KADIMA_DIR}/auto_executor.py"

    with open(cg_path, "r") as f:
        cg_content = f.read()
    with open(ae_path, "r") as f:
        ae_content = f.read()

    # Guard: don't double-append if get_capital_for_signal already exists.
    if "def get_capital_for_signal" in cg_content:
        print(f"ABORT: get_capital_for_signal already exists in {cg_path}")
        print("No files were modified.")
        return 1

    count = ae_content.count(AUTO_EXECUTOR_OLD)
    if count != 1:
        print(f"ABORT: expected exactly 1 match in {ae_path}, found {count}")
        print("----- expected text -----")
        print(AUTO_EXECUTOR_OLD)
        print("--------------------------")
        print("No files were modified.")
        return 1

    # Backups
    shutil.copy2(cg_path, cg_path + ".bak_final_capital_fix")
    print(f"Backed up {cg_path}")
    shutil.copy2(ae_path, ae_path + ".bak_final_capital_fix")
    print(f"Backed up {ae_path}")

    # Apply
    cg_content = cg_content.rstrip("\n") + "\n" + NEW_FUNCTION.strip() + "\n"
    ae_content = ae_content.replace(AUTO_EXECUTOR_OLD, AUTO_EXECUTOR_NEW)

    with open(cg_path, "w") as f:
        f.write(cg_content)
    print(f"Patched {cg_path} (appended get_capital_for_signal)")

    with open(ae_path, "w") as f:
        f.write(ae_content)
    print(f"Patched {ae_path} (call site updated)")

    print("\nAll edits applied. Next steps:")
    print(f"  1. python3 -m py_compile {cg_path} {ae_path}")
    print("  2. sudo systemctl restart kadima")
    print("  3. sudo systemctl status kadima")
    return 0


if __name__ == "__main__":
    sys.exit(main())
