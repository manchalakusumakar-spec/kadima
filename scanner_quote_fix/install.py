"""Install the bounded universe quote retry fix; no engine restart."""
import ast
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path("/home/ubuntu/kadima-py")
TARGET = ROOT / "kadima/stock_universe_scanner.py"
SOURCE = Path(__file__).resolve().parents[1] / "kadima/stock_universe_scanner.py"
UNIT = "kadima-universe-scanner.service"
EXPECTED = "18d40bd9ead639e852a56475c51b820c147898f0"

def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()

def blob(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()

def main():
    if os.geteuid() == 0:
        raise SystemExit("Run with python3 as ubuntu; the installer uses sudo for service commands.")
    old, new = TARGET.read_bytes(), SOURCE.read_bytes()
    ast.parse(new)
    if old == new:
        print("Quote retry fix already installed.")
        return
    if blob(old) != EXPECTED:
        raise SystemExit("Scanner source differs from the reviewed version. No changes made.")
    run("sudo", "-n", "true")
    command = run("systemctl", "show", UNIT, "--property=ExecStart", "--value")
    working = run("systemctl", "show", UNIT, "--property=WorkingDirectory", "--value")
    if str(ROOT) != working or "kadima.stock_universe_scanner" not in command:
        raise SystemExit("Scanner service launch path differs. No changes made.")
    if run("systemctl", "is-active", UNIT) != "active":
        raise SystemExit("Scanner is not active. No changes made.")
    backup = Path(tempfile.mkdtemp(prefix="kadima-scanner-backup-", dir="/home/ubuntu"))
    saved = backup / TARGET.name
    shutil.copy2(TARGET, saved)
    print("Backup:", saved, flush=True)
    print("Rollback: cp " + str(saved) + " " + str(TARGET) +
          " && sudo -n systemctl restart " + UNIT, flush=True)
    staged = TARGET.with_name(TARGET.name + ".update")
    shutil.copy2(SOURCE, staged)
    os.chmod(staged, TARGET.stat().st_mode & 0o777)
    try:
        run("sudo", "-n", "systemctl", "stop", UNIT)
        os.replace(staged, TARGET)
        run("sudo", "-n", "systemctl", "start", UNIT)
        run("systemctl", "is-active", UNIT)
    except Exception:
        shutil.copy2(saved, TARGET)
        run("sudo", "-n", "systemctl", "start", UNIT)
        raise SystemExit("Installation failed; original scanner restored.")
    finally:
        staged.unlink(missing_ok=True)
    print("Installed; scanner active. Quote coverage requires the next market-session scan.")
    print("This fixes quote collection retries. Gamma shortlist remains 20.")

if __name__ == "__main__":
    main()
