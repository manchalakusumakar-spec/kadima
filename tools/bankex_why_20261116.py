#!/usr/bin/env python3
"""Read-only report: why did KADIMA show/take a BANKEX CE on a given day?

Reads the audit log (DATA_DIR/audit/<day>.jsonl), kadima.db (read-only URI),
the strategy switch files and the live code hashes. Writes nothing except the
report file. Places no orders and does not touch the running engine.

    python3 bankex_why_20261116.py [YYYY-MM-DD] [INDEX]
"""
import collections
import glob
import hashlib
import json
import os
import sqlite3
import sys

DAY = sys.argv[1] if len(sys.argv) > 1 else "2026-11-16"
NAME = sys.argv[2] if len(sys.argv) > 2 else "BANKEX"
DATA = os.environ.get("KADIMA_DATA_DIR", "/var/lib/kadima")
ROOT = os.environ.get("KADIMA_ROOT", os.path.expanduser("~/kadima-py"))
OUT = os.path.expanduser(f"~/{NAME.lower()}_why_{DAY.replace('-', '')}.txt")

SNAP_1110 = {  # sha256 in kadima-audit live-snapshot-20261110-000309
    "gamma_blast_scanner.py": "02010d88d0c0a9afb9a0d10ce72fc35f368e49685d1e5a2319e0d0752cd9cc8c",
    "trend_rules.py": "4c57bc1b9f12fc91d957dd6775fee7062696e4a1ad736c5857e44ee02eef5914",
    "sniper.py": "a152444a6cbd8514fe073f057b65321d1572e02b560e6e65332a33f834a1dfae",
    "gamma_confirm.py": "57989fb1d97e259c4771c8db0052ee622194acd222d6da4c218247f7f1c14d4a",
    "celestial.py": "d1ab0937e643a6a236629ab3de3ffcdc0c5cfa3fd80b199412aae173f3a8cf11",
    "celestial_scanner.py": "9d632dc0a0db58399a1ed39ca586ab99439a70f96d1e7ac430cba82594e5279d",
    "engine.py": "e7e81f4f561c22df866ed602a37a922e20ad99c29da80ea31d138bc761beea4c",
    "auto_executor.py": "36bfd4330042de1c0c99afbeace7b37f9c1711c9ad5719277548155088b88cfc",
    "option_chain.py": "9780d7de6fe2cf9f6fa1087879b5fb7e5279f299ee8811806f75c09d70fc4c91",
    "config.py": "a1f0243bfd51a6f171ad64d9c4cf415c2884135592f0a6c7e473c55c7e2750e2",
    "gamma_priority.py": "5ca5259df6cb6363e1e4d70726122c2db254c00e57e29eada076364a776ed9ce",
}
EMBED_IF_CHANGED = {"gamma_blast_scanner.py", "trend_rules.py", "sniper.py",
                    "gamma_confirm.py", "celestial_scanner.py", "gamma_priority.py"}
NOISY = {"gamma_score", "gamma_activity_no_direction"}

out = open(OUT, "w")


def p(*a):
    print(*a, file=out)


def clip(s, n=1500):
    s = s if isinstance(s, str) else json.dumps(s, default=str, separators=(",", ":"))
    return s if len(s) <= n else s[:n] + f"...(+{len(s) - n} chars)"


p(f"=== {NAME} report for {DAY} (read-only) ===")

# ---------------------------------------------------------------- switches
p("\n== strategy switch files")
for j in sorted(glob.glob(os.path.join(DATA, "*.json"))):
    b = os.path.basename(j)
    try:
        if os.path.getsize(j) > 4096:
            p(f"{b}: (large, skipped)")
            continue
        txt = open(j).read().replace("\n", " ")
        if any(w in txt.lower() for w in ("password", "secret", "totp", "api_key", "jwt")):
            p(f"{b}: (mentions a credential word, skipped)")
            continue
        p(f"{b}: {txt}")
    except OSError as e:
        p(f"{b}: unreadable {e}")

# ---------------------------------------------------------------- code
p("\n== live code vs 10-Nov snapshot")
os.system(f"cd {ROOT} && echo HEAD $(git rev-parse --short HEAD) >> {OUT} 2>&1")
out.flush()
changed = []
for f, h in SNAP_1110.items():
    path = os.path.join(ROOT, "kadima", f)
    try:
        cur = hashlib.sha256(open(path, "rb").read()).hexdigest()
    except OSError:
        cur = "missing"
    same = cur == h
    p(f"{f}: {'same' if same else 'CHANGED ' + cur[:16]}")
    if not same and cur != "missing":
        changed.append(f)

# ---------------------------------------------------------------- audit log
path = os.path.join(DATA, "audit", f"{DAY}.jsonl")
p(f"\n== audit log {path}")
counts = collections.Counter()
buckets = collections.OrderedDict()   # 5-min bucket -> stats
events = []
first_ce_vel = []
other_trend = []
try:
    fh = open(path, errors="replace")
except OSError as e:
    p("cannot open:", e)
    fh = []
for line in fh:
    if NAME not in line and "day_trend_changed" not in line:
        continue
    try:
        r = json.loads(line)
    except ValueError:
        continue
    ev = r.get("event", "")
    name = r.get("name") or r.get("index") or r.get("index_name") or r.get("underlying")
    if ev == "day_trend_changed" and name != NAME:
        other_trend.append(f"{r.get('ts','')[11:19]} {name} {r.get('frm')}->{r.get('to')} spot={r.get('spot')}")
        continue
    if NAME not in json.dumps(r):
        continue
    counts[ev] += 1
    ts = r.get("ts", "")
    hhmm = ts[11:16]
    if ev in NOISY:
        key = f"{hhmm[:3]}{int(hhmm[3:5]) // 5 * 5:02d}" if len(hhmm) == 5 else hhmm
        b = buckets.setdefault(key, dict(CE=0, PE=0, NONE=0, max_score=0.0, tiers=collections.Counter(),
                                         mom=collections.Counter(), ce_hits=0, pe_hits=0))
        d = r.get("direction") or "NONE"
        b[d if d in ("CE", "PE") else "NONE"] += 1
        sc = r.get("score", r.get("raw_score")) or 0
        b["max_score"] = max(b["max_score"], float(sc))
        if ev == "gamma_score":
            b["tiers"][r.get("tier")] += 1
        comp = r.get("components") or {}
        if comp.get("momentum"):
            b["mom"][comp["momentum"]] += 1
        vel = (comp.get("oi_velocity") or {}).get("alerts") if isinstance(comp.get("oi_velocity"), dict) else None
        if d == "CE" and vel and hhmm >= "10:00" and len(first_ce_vel) < 6:
            first_ce_vel.append((ts[11:19], r.get("score"), r.get("tier"), vel[:12], comp))
        continue
    events.append(r)

p("\nevent counts:", dict(counts))
p("\n== gamma direction per 5 minutes (cycles CE / PE / none, max score, tiers, momentum)")
for k, b in buckets.items():
    p(f"{k}  CE={b['CE']:3d} PE={b['PE']:3d} none={b['NONE']:3d} max={b['max_score']:5.1f} "
      f"tiers={dict(b['tiers'])} mom={dict(b['mom'])}")

p("\n== sample CE-direction gamma cycles after 10:00 (OI-drop alerts that picked CE)")
for t, sc, tier, vel, comp in first_ce_vel:
    p(f"{t} score={sc} tier={tier}")
    p("  alerts:", clip(vel, 2500))
    p("  components:", clip({k: v for k, v in comp.items() if k != "oi_velocity"}, 800))

p("\n== other indices' day-trend changes")
for x in other_trend[:60]:
    p(x)

p(f"\n== every other {NAME} event (time order, max 600)")
for r in events[:600]:
    r = dict(r)
    ts = r.pop("ts", "")[11:19]
    ev = r.pop("event", "")
    p(f"{ts} {ev} {clip(r)}")
if len(events) > 600:
    p(f"... {len(events) - 600} more")

# ---------------------------------------------------------------- database
p("\n== kadima.db rows")
try:
    c = sqlite3.connect(f"file:{os.path.join(DATA, 'kadima.db')}?mode=ro", uri=True, timeout=5)
    c.row_factory = sqlite3.Row

    def q(title, sql, args):
        p(f"\n-- {title}")
        try:
            for row in c.execute(sql, args).fetchall():
                p(clip(dict(row), 2500))
        except sqlite3.Error as e:
            p("error:", e)

    q("signals", "SELECT * FROM signals WHERE index_name=? AND ts LIKE ? ORDER BY id", (NAME, DAY + "%"))
    q("trade_audit_plans", "SELECT * FROM trade_audit_plans WHERE underlying=? AND day=? "
      "ORDER BY suggested_at", (NAME, DAY))
    q("positions", "SELECT * FROM positions WHERE index_name=? AND entry_ts LIKE ?", (NAME, DAY + "%"))
except sqlite3.Error as e:
    p("db error:", e)

# ---------------------------------------------------------------- changed code
for f in changed:
    if f in EMBED_IF_CHANGED:
        p(f"\n== LIVE SOURCE {f} (changed since 10-Nov)")
        p(open(os.path.join(ROOT, "kadima", f)).read())

out.close()
print("WROTE", OUT, os.path.getsize(OUT), "bytes")
