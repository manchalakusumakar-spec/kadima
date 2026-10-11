#!/usr/bin/env python3
"""
KADIMA patch: wire live tier-based capital into risk_budget's max_risk_inr.

Touches:
  - kadima/engine.py        (2 call sites: bull_shield, gamma_blast)
  - kadima/auto_executor.py (function signature + risk_budget call)

Safe by design: makes a .bak of each file before editing, and ABORTS
with no changes written if any expected marker text isn't found
exactly as expected (e.g. if the live file has since diverged from
what this patch expects).

USAGE (on the EC2 box, inside ~/kadima-py):
    python3 kadima_patch_risk_capital.py
"""

import shutil
import sys

KADIMA_DIR = "/home/ubuntu/kadima-py/kadima"

EDITS = [
    {
        "file": f"{KADIMA_DIR}/engine.py",
        "old": (
            '        auto_executor.EXECUTOR.evaluate_and_enter(\n'
            '            index_name=lead.name, direction=lead.direction, chain=lead.chain,\n'
            '            spot=lead.spot, snap=lead.snap, days_to_expiry=self._dte(lead),\n'
            '            kind="bull_shield", source_signal_id=sid)\n'
        ),
        "new": (
            '        auto_executor.EXECUTOR.evaluate_and_enter(\n'
            '            index_name=lead.name, direction=lead.direction, chain=lead.chain,\n'
            '            spot=lead.spot, snap=lead.snap, days_to_expiry=self._dte(lead),\n'
            '            kind="bull_shield", source_signal_id=sid,\n'
            '            tier=lead.tier, score=lead.score)\n'
        ),
    },
    {
        "file": f"{KADIMA_DIR}/engine.py",
        "old": (
            '        auto_executor.EXECUTOR.evaluate_and_enter(\n'
            '            index_name=st.name, direction=result.direction, chain=st.chain,\n'
            '            spot=st.spot, snap=st.snap, days_to_expiry=self._dte(st),\n'
            '            kind="gamma_blast", source_signal_id=sid)\n'
        ),
        "new": (
            '        auto_executor.EXECUTOR.evaluate_and_enter(\n'
            '            index_name=st.name, direction=result.direction, chain=st.chain,\n'
            '            spot=st.spot, snap=st.snap, days_to_expiry=self._dte(st),\n'
            '            kind="gamma_blast", source_signal_id=sid,\n'
            '            tier=result.tier, score=result.score)\n'
        ),
    },
    {
        "file": f"{KADIMA_DIR}/auto_executor.py",
        "old": (
            '    def evaluate_and_enter(self, index_name: str, direction: str, chain, spot: float,\n'
            '                           snap, days_to_expiry: float, kind: str,\n'
            '                           source_signal_id: int) -> None:\n'
        ),
        "new": (
            '    def evaluate_and_enter(self, index_name: str, direction: str, chain, spot: float,\n'
            '                           snap, days_to_expiry: float, kind: str,\n'
            '                           source_signal_id: int, tier: str, score: float) -> None:\n'
        ),
    },
    {
        "file": f"{KADIMA_DIR}/auto_executor.py",
        "old": (
            '        verdict = risk_budget.select_within_budget(\n'
            '            candidates, spot, max_lots=MAX_LOTS_PER_SIGNAL)\n'
        ),
        "new": (
            '        max_risk_inr = capital_guard.get_capital_for_tier(tier) * 0.03\n'
            '\n'
            '        verdict = risk_budget.select_within_budget(\n'
            '            candidates, spot, max_risk_inr=max_risk_inr, max_lots=MAX_LOTS_PER_SIGNAL)\n'
        ),
    },
]


def main() -> int:
    # Pass 1: verify every marker exists exactly once, in its file, BEFORE
    # touching anything. All-or-nothing.
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
            return 1

    # Pass 2: backup every touched file once, then apply all edits.
    backed_up = set()
    for path in file_contents:
        if path not in backed_up:
            bak = path + ".bak_risk_capital_patch"
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
    print("  1. grep -n 'capital_guard' "
          f"{KADIMA_DIR}/auto_executor.py | head -5")
    print("     (confirm capital_guard is imported; add "
          "'from . import capital_guard' near the other imports if missing)")
    print("  2. python3 -m py_compile "
          f"{KADIMA_DIR}/engine.py {KADIMA_DIR}/auto_executor.py")
    print("  3. sudo systemctl restart kadima")
    print("  4. sudo systemctl status kadima  (confirm active/running, "
          "no restart-loop)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
