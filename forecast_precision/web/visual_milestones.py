"""Exact-source static Celestial 5-minute marker patch. Does NOT enable PR43.
No broker, market data, orders, new polling, Python engine or service changes.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone

SOURCE_SHA="e52ec259cd2ee07db2e6fea578ab0a16d49eb08255eed521ff0fbb1c3ba9972f"
D="$"

MARKERS="""
    const milestoneMarks = pts.map(p => {
      const f = Number(p.forecast);
      const t = String(p.ts || '').slice(11, 16);
      if (!Number.isFinite(f) || !/^\\[0-9]{2}:\\[0-9]{2}$/.test(t)) return '';
      const x = xs(p.ts), y = ys(f);
      const a = p.actual == null ? null : Number(p.actual);
      const good = a !== null && Number.isFinite(a);
      const delta = good ? a - f : null;
      const bad = good && INDEX === 'NIFTY' && Math.abs(delta) >= 5;
      let out = '<circle cx="' + x + '" cy="' + y +
        '" r="4" fill="var(--fc)"><title>' + t +
        ' forecast '+ fmt(f,1) + '</title></circle>' +
        '<text x="'+x+'" y="'+(y-8)+'" fill="var(--fc)" font-size="9" text-anchor="middle">'+
        fmt(f,1)+'</text>';
      if(good)out += '<circle cx="'+x+'" cy="'+ys(a)+'" r="4.5" '+
        'fill="var(--actual)" stroke="'+(bad?'var(--down)':'none')+
        '" stroke-width="2.5"><title>'+t+' API actual '+fmt(a,1)+
        '; deviation '+sign(delta)+fmt(delta,1)+' points</title></circle>';
      return out;
    }).join('');
"""

TABLE="""
  function milestoneTable(s) {
    if (!Array.isArray(s.points) || !s.points.length) return '';
    let rows = '';
    for (const p of s.points) {
      const t=String(p.ts || '').slice(11,16), f=Number(p.forecast);
      if(!/^\\[0-9]{2}:\\[0-9]{2}$/.test(t)||!Number.isFinite(f))continue;
      const a=p.actual == null?null:Number(p.actual);
      const has=a!==null&&Number.isFinite(a),d=has?a-f:null;
      const red=INDEX==='NIFTY'&&has&&Math.abs(d)>=5;
      rows+='<tr><td>'+t+'</td><td>'+fmt(f,1)+'</td><td>'+
        (has?fmt(a,1):'Pending')+'</td><td style="color:'+
        (red?'var(--down)':'var(--muted)')+'">'+
        (has?sign(d)+fmt(d,1)+' pts':'—')+'</td></tr>';
    }
    return '<div style="overflow-x:auto;margin-top:8px">' +
      '<table style="font-size:11px;width:100%;min-width:310px;text-align:right">' +
      '<thead><tr><th>IST</th><th>Forecast</th><th>API actual</th><th>Deviation</th></tr></thead>'+
      '<tbody>'+rows+'</tbody></table><div style="font-size:10px;color:var(--muted)">'+
      'Five-minute API milestones only. Red means |deviation| >= 5 NIFTY points. '+
      'Independent actual timestamps not yet verified. No automatic reforecast or trade is activated.'+
      '</div></div>';
  }

"""

def sha(b):
    return hashlib.sha256(b).hexdigest()

def replace_one(s,old,new):
    if s.count(old)!=1:
        raise ValueError("exact audited HTML pattern changed: "+old[:60])
    return s.replace(old,new,1)

def transform(s):
    if "milestoneTable(s)" in s or "milestoneMarks" in s:
        raise ValueError("already modified")
    s=replace_one(s,"const W = 340, H = 170, L = 50, R = 18, T = 10, B = 22;",
        "const W = Math.max(340, pts.length * 84 + 100), H = 190, L = 50, R = 18, T = 22, B = 30;")
    s=replace_one(s,"const step = pts.length > 6 ? 3 : 1;","const step = 1;")
    s=replace_one(s,"    const liveDot = s.live ?",MARKERS+"    const liveDot = s.live ?")
    s=replace_one(s,D+"{dev}"+D+"{liveDot}</svg>",
                  D+"{dev}"+D+"{liveDot}"+D+"{milestoneMarks}</svg>")
    s=replace_one(s,'<svg viewBox="0 0 '+D+'{W} '+D+'{H}" role="img"',
        '<svg style="width:'+D+'{W}px;max-width:none" viewBox="0 0 '+D+'{W} '+D+'{H}" role="img"')
    s=replace_one(s,"  function card(s) {",TABLE+"  function card(s) {")
    s=replace_one(s,D+"{kv}"+D+"{chainInputs(s)}"+D+"{chart(s)}"+D+"{auditBlock(s)}</div>",
      D+"{kv}"+D+"{chainInputs(s)}<div style=\"overflow-x:auto\">"+D+"{chart(s)}</div>"+
      D+"{milestoneTable(s)}"+D+"{auditBlock(s)}</div>")
    return s

def check_js(html):
    node=shutil.which("node")
    if not node:raise ValueError("node needed for static JavaScript syntax check")
    scripts=re.findall(r"<script>([\s\S]*?)</script>",html)
    if len(scripts)!=1:raise ValueError("expected exactly one inline script")
    result=subprocess.run([node,"--check","-"],input=scripts[0].encode(),
                          capture_output=True,timeout=15)
    if result.returncode:raise ValueError("inline JavaScript failed syntax check")

def run(repo,site,release_sha,apply):
    if not re.fullmatch(r"[0-9a-f]{40}",release_sha):
        raise ValueError("exact Git commit SHA required")
    if subprocess.check_output(["git","-C",str(repo),"rev-parse","HEAD"],text=True).strip()!=release_sha:
        raise ValueError("source worktree is not reviewed release SHA")
    dirty=subprocess.check_output(["git","-C",str(repo),"status","--porcelain",
                                   "--untracked-files=no"],text=True)
    if dirty.strip():raise ValueError("source has uncommitted tracked changes")
    if site.is_symlink() or not site.is_file():raise ValueError("live page absent/symlink")
    old=site.read_bytes()
    if sha(old)!=SOURCE_SHA:raise ValueError("STOP: live HTML differs from pinned source")
    new=transform(old.decode("utf-8")).encode("utf-8")
    check_js(new.decode("utf-8"))
    receipt={"scope":"STATIC_FORECAST_UI_ONLY","status":"DRY_RUN_READY",
       "release_sha":release_sha,"original_sha256":sha(old),
       "new_sha256":sha(new),"trading_changes":False,
       "services_restarted":False,"autoreforecast_enabled":False}
    if not apply:return receipt
    backupdir=Path("/var/backups/kadima-forecast-ui")
    backupdir.mkdir(mode=0o700,parents=True,exist_ok=True)
    backup=backupdir/("forecast-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+".html")
    if backup.exists():raise ValueError("backup collision; abort")
    backup.write_bytes(old)
    os.chmod(backup,0o600)
    fd,tmp=tempfile.mkstemp(prefix=".kadima-forecast-",dir=str(site.parent))
    try:
        os.fchmod(fd,site.stat().st_mode&0o777)
        with os.fdopen(fd,"wb") as f:
            f.write(new)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp,site)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    if sha(site.read_bytes())!=sha(new):
        raise ValueError("post-install hash mismatch: restore "+str(backup))
    receipt["status"]="STATIC_UI_APPLIED"
    receipt["backup"]=str(backup)
    return receipt

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--repo",default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--site",default="/var/www/html/forecast.html")
    parser.add_argument("--release-sha",required=True)
    parser.add_argument("--apply",action="store_true")
    a=parser.parse_args()
    print(json.dumps(run(Path(a.repo),Path(a.site),a.release_sha,a.apply),indent=2))

if __name__=="__main__":
    main()
