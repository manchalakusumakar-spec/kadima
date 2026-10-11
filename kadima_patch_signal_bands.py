#!/usr/bin/env python3
"""
KADIMA follow-up patch: fixed capital bands per signal kind.

Run this AFTER kadima_patch_risk_capital.py has already been applied
successfully. This patch:

  1. Adds get_capital_for_signal(kind, tier) to capital_guard.py --
     fixed rupee bands per signal source, confirmed by Kusumakar
     (2026-09-11):
         bull_shield: 25,000 - 30,000
         gamma_blast: 50,000 - 62,000
     CONFIRMED and ACTIVE tiers share the same band per kind; WATCH
     or unknown tiers return 0.0 (blocks sizing). Reads live 'net'
     balance each call and clamps it into the band. Fails closed to
     0.0 on any RMS read failure.

  2. Updates the one call site in auto_executor.py to call
     get_capital_for_signal(kind, tier) instead of
     get_capital_for_tier(tier).

Safe by design: makes a .bak of each touched file before editing, and
ABORTS with no changes written if any expected marker text isn't
found exactly as expected.

USAGE (on the EC2 box, inside ~/kadima-py, AFTER the first patch):
    python3 kadima_patch_signal_bands.py
"""

import shutil
import sys

KADIMA_DIR = "/home/ubuntu/kadima-py/kadima"

NEW_FUNCTION = '''

def get_capital_for_signal(kind: str, tier: str) -> float:
    """
    Capital base for risk_budget's per-trade sizing.

    Fixed rupee bands per signal source (kind), confirmed by Kusumakar
    (2026-09-11): CONFIRMED and ACTIVE tiers share the same band per
    kind -- tier does not change the capital base, only WATCH/unknown
    tiers are blocked (return 0.0).

        bull_shield: 25,000 - 30,000
        gamma_blast: 50,000 - 62,000

    Live 'net' balance from Angel One RMS is clamped into that band --
    if net falls below the band's floor, the floor is used (not the
    live balance), and likewise capped at the ceiling. This still
    reads live data each call (not a hardcoded number), but the
    result is bounded by these fixed limits regardless of actual
    balance -- FAILS CLOSED to 0.0 on any RMS read failure, same as
    the rest of capital_guard.py.
    """
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

EDITS = [
    {
        "file": f"{KADIMA_DIR}/capital_guard.py",
        "old": (
            'def get_capital_for_tier(tier: str) -> float:\n'
        ),
        "new": (
            NEW_FUNCTION.strip() + '\n\n\n'
            'def get_capital_for_tier(tier: str) -> float:\n'
        ),
        "note": "Adds new function ABOVE the old get_capital_for_tier "
                "(left in place, now unused by auto_executor.py after "
                "the second edit below, but not deleted -- safe no-op "
                "if anything else still references it).",
    },
    {
        "file": f"{KADIMA_DIR}/auto_executor.py",
        "old": (
            '        max_risk_inr = capital_guard.get_capital_for_tier(tier) * 0.03\n'
        ),
        "new": (
            '        max_risk_inr = capital_guard.get_capital_for_signal(kind, tier) * 0.03\n'
        ),
    },
]


def main() -> int:
    file_contents = {}
    for edit in EDITS:
        path = edit["file"]
        if path not in file_contents:
            with open(path, "r") as f:
                file_contents[path] = f.read()
        content = file_contents[path]
        count = content.count(edit["old"])
        if count != 1:
            print(f"ABORT: expected exactly 1 match in {path}, found {count}")
            print("----- expected text -----")
            print(edit["old"])
            print("--------------------------")
            print("No files were modified.")
            print()
            print("If this is capital_guard.py and the match count is 0,")
            print("check whether kadima_patch_risk_capital.py has already")
            print("been run -- this follow-up patch depends on it.")
            return 1

    backed_up = set()
    for path in file_contents:
        if path not in backed_up:
            bak = path + ".bak_signal_bands_patch"
            shutil.copy2(path, bak)
            print(f"Backed up {path} -> {bak}")
            backed_up.add(path)

    for edit in EDITS:
        path = edit["file"]
        file_contents[path] = file_contents[path].replace(edit["old"], edit["new"])

    for path, content in file_contents.items():
        with open(path, "w") as f:
            f.write(content)
        print(f"Patched {path}")

    print("\nAll edits applied. Next steps:")
    print("  1. python3 -m py_compile "
          f"{KADIMA_DIR}/capital_guard.py {KADIMA_DIR}/auto_executor.py")
    print("  2. sudo systemctl restart kadima")
    print("  3. sudo systemctl status kadima  (confirm active/running, "
          "no restart-loop)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
