#!/usr/bin/env python3
"""Deploy the 2026-09-30 CAS live fixes onto the EC2 checkout.

Fixes (built against the captured runtime 2474a7c):
  1. CARRYFORWARD prices snapped to the Rs 0.05 tick grid (BUY up, SELL down,
     never below one tick). INTRADAY order prices are unchanged.
  2. Exit SELL price never rounds to Rs 0.0 for a collapsed premium (all lanes).
  3. cas_blast (CARRYFORWARD) entries are not blocked by the broker's 15:15
     INTRADAY entry window; calendar, schedule and lane permit still apply.
  4. owned_watchdog leaves cas_blast positions to the engine's 15:38 close.

Run from ~/kadima-py after:  git fetch origin cas-blast-live-fixes
    python3 /tmp/deploy_cas_fixes.py            # check only, changes nothing
    python3 /tmp/deploy_cas_fixes.py --apply    # back up, patch, compile, test
Nothing is changed unless the check passes; any failure restores the backup.
"""
import os, re, subprocess, sys, tarfile, time

BASE = "2474a7c0f0738a7a4d32aef6a86f39b352edfce3"   # captured EC2 runtime
REF = "FETCH_HEAD"
ROOT = os.path.expanduser("~/kadima-py")
FILES = ["kadima/angel_orders.py", "kadima/auto_executor.py", "kadima/cas_executor.py",
         "kadima/execution_ledger.py", "kadima/market_session_gate.py",
         "kadima/owned_watchdog.py", "tools/test_cas_executor.py"]


def sh(*cmd, check=True, **kw):
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, **kw)
    if check and r.returncode:
        sys.exit(f"FAILED: {' '.join(cmd)}\n{r.stdout}{r.stderr}")
    return r


def engine_python():
    m = re.search(r"path=(\S+)", sh("systemctl", "show", "kadima", "-p", "ExecStart", check=False).stdout)
    return m.group(1) if m and os.path.exists(m.group(1)) else "python3"


def main():
    apply = "--apply" in sys.argv
    os.chdir(ROOT)
    sh("git", "cat-file", "-e", BASE + "^{commit}")
    patch = sh("git", "diff", BASE, REF, "--", *FILES).stdout
    open("/tmp/cas_fixes.patch", "w").write(patch)
    chk = sh("git", "apply", "--check", "/tmp/cas_fixes.patch", check=False)
    changed = [f for f in FILES if sh("git", "diff", "--quiet", BASE, "--", f, check=False).returncode]
    print("files differing from the 2474a7c capture:", changed or "none")
    print("patch:", "OK" if chk.returncode == 0 else "CONFLICT\n" + chk.stderr)
    if chk.returncode:
        sys.exit("STOP: nothing changed. Send this output to Claude.")
    if not apply:
        print("Check passed. Re-run with --apply to install."); return
    backup = os.path.expanduser(f"~/kadima-backup-casfix-{time.strftime('%Y%m%d-%H%M%S')}.tgz")
    with tarfile.open(backup, "w:gz") as t:
        for f in FILES:
            t.add(f)
    print("backup:", backup)
    sh("git", "apply", "/tmp/cas_fixes.patch")
    py = engine_python()
    comp = sh(py, "-m", "py_compile", *FILES, check=False)
    test = sh(py, "tools/test_cas_executor.py", check=False,
              env=dict(os.environ, KADIMA_DATA_DIR=f"/tmp/casfix-test-{os.getpid()}"))
    print(test.stdout[-2500:])
    if comp.returncode or test.returncode:
        with tarfile.open(backup) as t:
            t.extractall(ROOT)
        sys.exit(f"FAILED ({(comp.stderr or test.stderr)[-800:]}) -- files restored from backup.")
    print("INSTALLED and tested with", py)
    print("Restart only when flat and outside 15:10-15:40:  sudo systemctl restart kadima")


if __name__ == "__main__":
    main()
