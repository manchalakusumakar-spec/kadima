"""Update only dashboard HTML; keep session gateway and engine running."""
import hashlib
import os
from pathlib import Path
import shutil
import tempfile

def main():
    if os.geteuid() != 0:
        raise SystemExit("Run this updater using sudo -n python3.")
    target = Path("/var/www/html/dashboard.html")
    source = Path(__file__).with_name("dashboard.html")
    old, new = target.read_bytes(), source.read_bytes()
    if old == new:
        print("Dashboard timestamps already installed.")
        return
    current = hashlib.sha1(b"blob " + str(len(old)).encode() + b"\0" + old).hexdigest()
    if current != "8264abbd83fc83ee5eb80258c2a6afb64efb4e1a":
        raise SystemExit("Dashboard differs from the reviewed version; no changes made.")
    if not Path("/etc/kadima-dashboard/config.json").exists():
        raise SystemExit("Dashboard gateway configuration missing; no changes made.")
    backup = Path(tempfile.mkdtemp(prefix="kadima-dashboard-matrix-", dir="/var/backups"))
    shutil.copy2(target, backup / "dashboard.html")
    fd, tmp = tempfile.mkstemp(prefix=".dashboard-matrix-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(new)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    finally:
        Path(tmp).unlink(missing_ok=True)
    print("Installed Matrix animation. Refresh the browser with Ctrl+F5.")
    print("Backup:", backup / "dashboard.html")
    print("Rollback: sudo -n cp " + str(backup / "dashboard.html") + " " + str(target))
    print("No services restarted.")
if __name__ == "__main__":
    main()
