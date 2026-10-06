"""Separate service installer; plan by default. Does not edit/restart KADIMA or nginx."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import sys
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
ROOT = Path("/opt/kadima-connector")
UNITS = ("kadima-connector.service", "kadima-connector-collect.service", "kadima-connector-collect.timer")


def validate_files():
    files = sorted(p for p in HERE.iterdir() if p.is_file() and p.suffix in
                   (".py", ".json", ".lock", ".md", ".service", ".timer", ".example"))
    if any(p.is_symlink() for p in files):
        raise ValueError("symlink_source_rejected")
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.name.encode() + b"\0" + path.read_bytes())
    return files, digest.hexdigest()[:20]


def config_check():
    path = Path("/etc/kadima-connector/oauth.json")
    meta = path.lstat()
    if not stat.S_ISREG(meta.st_mode) or meta.st_uid != 0 or meta.st_mode & 0o077:
        raise ValueError("oauth_config_must_be_root_owned_0600")
    cfg = json.loads(path.read_text())
    if cfg.get("read_scope", "kadima:read") not in ("kadima:read", "kadima/read"):
        raise ValueError("supported_read_scope_required")
    for key in ("resource_url", "issuer_url", "jwks_url"):
        p = urlsplit(cfg[key])
        if p.scheme != "https" or not p.hostname or p.username or p.password or p.query or p.fragment or p.port not in (None, 443) or ".example" in p.hostname:
            raise ValueError("verified_https_oauth_configuration_required")
    if urlsplit(cfg["resource_url"]).path != "/mcp":
        raise ValueError("resource_path_must_be_mcp")
    for key in ("allowed_subjects", "allowed_client_ids"):
        values = cfg.get(key)
        if not isinstance(values, list) or not values or any(
            not isinstance(v, str) or not v or v == "*" or v.startswith("REPLACE_") for v in values):
            raise ValueError("oauth_identity_configuration_required")


def run(args, timeout=30):
    subprocess.run(args, check=True, timeout=timeout)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    files, version = validate_files()
    print("PLAN: separate read-only collector timer and OAuth MCP service; release", version)
    print("Trading services, controls, broker orders and nginx configuration will not be changed.")
    if not args.apply:
        print("NEEDED: /etc/kadima-connector/oauth.json (root:root 0600), identity provider, HTTPS proxy.")
        return
    if os.geteuid() != 0:
        raise SystemExit("Run apply through sudo on the KADIMA host.")
    config_check()  # Fail before any system change if authentication is not configured.
    if not Path("/etc/kadima-dashboard/config.json").is_file():
        raise SystemExit("Existing dashboard local API credential unavailable.")
    for unit in UNITS:
        path = Path("/etc/systemd/system") / unit
        if path.exists():
            raise SystemExit("Existing connector installation found; review its version before upgrading.")
    try:
        account = pwd.getpwnam("kadima-reader")
        if account.pw_uid == 0 or account.pw_shell not in ("/usr/sbin/nologin", "/sbin/nologin"):
            raise SystemExit("Existing kadima-reader account is unsuitable.")
    except KeyError:
        run(["useradd", "--system", "--user-group", "--no-create-home", "--home-dir", "/nonexistent",
             "--shell", "/usr/sbin/nologin", "kadima-reader"])
    release = ROOT / "releases" / version
    release.mkdir(parents=True, mode=0o755, exist_ok=False)
    package = release / "server_connector"
    package.mkdir(mode=0o755)
    for source in files:
        target = package / source.name
        shutil.copyfile(source, target)
        target.chmod(0o644)
    run(["/usr/bin/python3", "-m", "venv", str(release / "venv")], timeout=60)
    python = str(release / "venv/bin/python")
    run([python, "-m", "pip", "install", "--disable-pip-version-check", "--require-virtualenv",
         "-r", str(package / "requirements.lock")], timeout=300)
    run([python, "-m", "compileall", "-q", str(package)])
    snapshot_dir = Path("/var/lib/kadima-connector")
    if snapshot_dir.is_symlink():
        raise SystemExit("Snapshot directory cannot be a symlink.")
    snapshot_dir.mkdir(mode=0o750, exist_ok=True)
    shutil.chown(snapshot_dir, user="root", group="kadima-reader")
    snapshot_dir.chmod(0o750)
    (ROOT / "current").symlink_to(release, target_is_directory=True)
    for unit in UNITS:
        shutil.copyfile(package / unit, Path("/etc/systemd/system") / unit)
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "start", "kadima-connector-collect.service"], timeout=50)
    run(["systemctl", "enable", "--now", "kadima-connector-collect.timer", "kadima-connector.service"])
    print("INSTALLED: connector services only; public TLS/OAuth connection still needs verification.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError, KeyError) as exc:
        raise SystemExit("Connector setup stopped: " + type(exc).__name__) from None
