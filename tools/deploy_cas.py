#!/usr/bin/env python3
"""Deploy CAS auto-execution onto the live EC2 checkout, which is NEWER than
GitHub. Nothing is changed unless every step's pre-check passes.

Run from ~/kadima-py after:  git fetch origin cas-auto-execution
    python3 tools/deploy_cas.py            # check only, changes nothing
    python3 tools/deploy_cas.py --apply    # back up, install, compile, test
Rollback:  tar xzf <backup printed below> -C ~/kadima-py  && sudo systemctl restart kadima
"""
import os, re, subprocess, sys, tarfile, time

BASE = "c53365d"                 # GitHub main the branch was built on
REF = "FETCH_HEAD"
ROOT = os.path.expanduser("~/kadima-py")
PATCHED = ["kadima/angel_orders.py", "kadima/auto_executor.py", "kadima/celestial_adoption.py",
           "kadima/engine.py", "kadima/execution_ledger.py", "kadima/external_reconciliation.py",
           "kadima/test_order_pathway.py"]
NEW = ["kadima/cas_config.py", "kadima/cas_executor.py", "kadima/cas_eod_guard.py",
       "kadima/cas_blast_scanner.py", "tools/test_cas_executor.py"]
# Two watchdog designs exist. GitHub's eod_watchdog places its own INTRADAY
# orders (needs the edits below). The live box's owned_watchdog asks the engine
# to exit via executor._close_position (already CAS-aware) and only needs its
# broker-matching key to use the position's own product.
OWNED = "kadima/owned_watchdog.py"
OWNED_EDITS = [("(p.exchange, p.token, 'INTRADAY')",
                "(p.exchange, p.token, 'CARRYFORWARD' if getattr(p, 'kind', None) == 'cas_blast' else 'INTRADAY')")]
WATCHDOG = "kadima/eod_watchdog.py"
WATCHDOG_EDITS = [
    ('        "producttype": "INTRADAY",\n',
     '        "producttype": str(pos.get("producttype") or "INTRADAY"),\n'),
    ('    for pos in open_positions:\n        _close_position(pos)\n',
     '    from kadima import cas_eod_guard\n'
     '    for pos in cas_eod_guard.to_close(open_positions, now_ist()):\n        _close_position(pos)\n'),
]


def sh(*cmd, check=True, **kw):
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, **kw)
    if check and r.returncode:
        sys.exit(f"FAILED: {' '.join(cmd)}\n{r.stdout}{r.stderr}")
    return r


def engine_python():
    out = sh("systemctl", "show", "kadima", "-p", "ExecStart", check=False).stdout
    m = re.search(r"path=(\S+)", out)
    return m.group(1) if m and os.path.exists(m.group(1)) else "python3"


def main():
    apply = "--apply" in sys.argv
    os.chdir(ROOT)
    sh("git", "cat-file", "-e", REF + "^{commit}")
    sh("git", "cat-file", "-e", BASE + "^{commit}")
    patch = sh("git", "diff", BASE, REF, "--", *PATCHED).stdout
    open("/tmp/cas_core.patch", "w").write(patch)
    chk = sh("git", "apply", "--check", "/tmp/cas_core.patch", check=False)
    wd_text = open(WATCHDOG).read()
    if "placeOrder" in wd_text:                         # GitHub-style watchdog
        target, edits = WATCHDOG, WATCHDOG_EDITS
    elif os.path.exists(OWNED) and "owned_watchdog" in wd_text:   # live owned watchdog
        target, edits = OWNED, OWNED_EDITS
    else:
        sys.exit("STOP: unrecognised watchdog design; nothing changed. Send this output to Claude.")
    wd = open(target).read()
    missing = [old for old, _ in edits if wd.count(old) != 1]
    refs = sh("grep", "-rln", "--include=*.py", "cas_blast_scanner", "kadima", "run.py", check=False).stdout.split()
    refs = [r for r in refs if r not in ("kadima/cas_blast_scanner.py", "kadima/engine.py",
                                         "kadima/cas_executor.py")]
    print("core patch (7 files):", "OK" if chk.returncode == 0 else "CONFLICT\n" + chk.stderr)
    print(f"watchdog ({target}) anchors:", "OK" if not missing else f"NOT FOUND {missing}")
    other = sh("grep", "-rlnE", "--include=*.py", "['\"]INTRADAY['\"]", "kadima", check=False).stdout.split()
    other = sorted(set(other) - set(PATCHED) - {WATCHDOG, OWNED, "kadima/angel_orders.py"})
    if other:
        print("note - other live files with a literal INTRADAY (not changed; send to Claude):", other)
    print("other live users of cas_blast_scanner:", refs or "none")
    if chk.returncode or missing:
        sys.exit("STOP: nothing changed. Send this output to Claude.")
    if not apply:
        print("Check passed. Re-run with --apply to install."); return

    backup = os.path.expanduser(f"~/kadima-backup-cas-{time.strftime('%Y%m%d-%H%M%S')}.tgz")
    with tarfile.open(backup, "w:gz") as t:
        for f in PATCHED + [target] + NEW:
            if os.path.exists(f):
                t.add(f)
    print("backup:", backup)
    sh("git", "apply", "/tmp/cas_core.patch")
    for f in NEW:
        open(f, "w").write(sh("git", "show", f"{REF}:{f}").stdout)
    for old, new in edits:
        wd = wd.replace(old, new)
    open(target, "w").write(wd)

    py = engine_python()
    files = PATCHED + NEW + [target]
    comp = sh(py, "-m", "py_compile", *[f for f in files if f.endswith(".py")], check=False)
    test = sh(py, "tools/test_cas_executor.py", check=False,
              env=dict(os.environ, KADIMA_DATA_DIR=f"/tmp/cas-test-{os.getpid()}"))
    print(test.stdout[-1500:])
    if comp.returncode or test.returncode:
        with tarfile.open(backup) as t:
            saved = set(t.getnames())
            t.extractall(ROOT)
        for f in NEW:
            if f not in saved and os.path.exists(f):
                os.remove(f)
        sys.exit(f"FAILED ({comp.stderr or test.stderr[-800:]}) -- live files restored from backup.")
    print("INSTALLED and tested with", py, "-> now: sudo systemctl restart kadima")


if __name__ == "__main__":
    main()
