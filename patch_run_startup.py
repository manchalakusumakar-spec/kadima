#!/usr/bin/env python3
"""
patch_run_startup.py — make the API reachable regardless of market state.

PROBLEM:
    run.py currently does, in order:
        1. premarket wait loop      <- blocks when market is closed
        2. engine.start()           <- blocks/retries on Angel One WebSocket
        3. start uvicorn thread     <- API only binds here

    Both 1 and 2 sit ahead of the web server. Outside market hours the
    process never reaches step 3, so nothing listens on 8080 and nginx
    returns 502. That is the silent hang seen on 17 Aug: systemd showed
    "active (running)" while `ss -tlnp` showed no listening socket.

FIX:
    Start the API thread FIRST. /state then answers immediately and
    reports honestly (market_open false, indices {}) instead of the whole
    process hanging. The premarket wait and engine.start() run after.

SAFETY CHECK PERFORMED:
    api.state() iterates ENGINE.states.items(). engine.states is
    initialised to {} in __init__ (engine.py:61), not in start(), so
    calling /state before engine.start() yields "indices": {} rather
    than an AttributeError.

USAGE:
    cd ~/kadima-py
    venv/bin/python patch_run_startup.py          # dry run, shows diff
    venv/bin/python patch_run_startup.py --apply  # writes run.py
"""

import sys
import shutil
from pathlib import Path

RUN_PY = Path.home() / "kadima-py" / "run.py"

# The API block as it currently appears (lines 31-38), matched loosely
API_MARKERS = [
    "from kadima import api as state_api",
    "state_api.register(engine)",
    "import threading, uvicorn",
    "threading.Thread(",
    "uvicorn.run(state_api.app",
    "log_level=",
    'daemon=True, name="state-api").start()',
    'audit.log("state_api_started"',
]

WAIT_MARKER = "if not force_start:"
ENGINE_START_MARKER = "engine.start()"


def find_api_block(lines):
    """Locate the contiguous API-start block by its first and last markers."""
    start = end = None
    for i, ln in enumerate(lines):
        if API_MARKERS[0] in ln:
            start = i
        if 'audit.log("state_api_started"' in ln:
            end = i
            break
    if start is None or end is None or end < start:
        return None, None
    return start, end


def find_wait_block_start(lines):
    for i, ln in enumerate(lines):
        if WAIT_MARKER in ln:
            return i
    return None


def main() -> int:
    apply = "--apply" in sys.argv

    if not RUN_PY.exists():
        print(f"ERROR: {RUN_PY} not found")
        return 1

    lines = RUN_PY.read_text().splitlines(keepends=True)

    api_start, api_end = find_api_block(lines)
    wait_start = find_wait_block_start(lines)

    if api_start is None:
        print("ERROR: could not locate the API-start block in run.py.")
        print("       Expected a line containing:", API_MARKERS[0])
        return 1
    if wait_start is None:
        print("ERROR: could not locate the premarket wait block.")
        print("       Expected a line containing:", WAIT_MARKER)
        return 1
    if api_start < wait_start:
        print("Nothing to do: the API block already precedes the wait loop.")
        return 0

    api_block = lines[api_start:api_end + 1]

    header = [
        "    # --- API FIRST -------------------------------------------------\n",
        "    # Bind the web server before any blocking wait, so /state stays\n",
        "    # reachable outside market hours and while the feed reconnects.\n",
        "    # Without this the process hangs below and nginx returns 502.\n",
    ]
    footer = ["\n"]

    new_lines = (
        lines[:wait_start]
        + header
        + api_block
        + footer
        + lines[wait_start:api_start]
        + lines[api_end + 1:]
    )

    print("=" * 62)
    print("BEFORE (order of operations):")
    print(f"  line {wait_start+1:>3}: premarket wait loop")
    print(f"  line {api_start-1:>3}: engine.start()")
    print(f"  line {api_start+1:>3}: uvicorn thread start   <- API binds here")
    print()
    print("AFTER:")
    print("        : uvicorn thread start   <- API binds first")
    print("        : premarket wait loop")
    print("        : engine.start()")
    print("=" * 62)

    if not apply:
        print("\nDRY RUN — no changes written.")
        print("Re-run with --apply to write run.py (a .bak is kept).")
        return 0

    backup = RUN_PY.with_suffix(".py.bak")
    shutil.copy2(RUN_PY, backup)
    RUN_PY.write_text("".join(new_lines))
    print(f"\nWritten. Backup at {backup}")

    # Verify it still parses.
    import ast
    try:
        ast.parse(RUN_PY.read_text())
        print("Syntax check: OK")
    except SyntaxError as exc:
        print(f"SYNTAX ERROR after patch: {exc}")
        shutil.copy2(backup, RUN_PY)
        print("Reverted from backup. No changes applied.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
