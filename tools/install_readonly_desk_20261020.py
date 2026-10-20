#!/usr/bin/env python3
"""Install the 2026-10-20 read-only Celestial desk (trader decision, 20-Oct 21:46 IST).

 1. Gateway (/opt/kadima-dashboard/gateway.py): no sign-in, any number of viewers at once;
    every write route (manual exit, buy/sell/SL/cancel/square-off/kill/pause/config) answers
    403 controls_removed. Adds a read-only /forecast/{hourly,days} proxy so the forecast
    panel no longer asks for a control token.
 2. Dashboard (/var/www/html/dashboard.html): all controls and the sign-in box hidden;
    Gamma Blast panel moved to the top; rows with Gamma score above 50 sorted first,
    highlighted and blinking, with an alert banner; a "Gamma Blast suggestions" table
    under Trade Audit (today's Gamma rows with entry, stop, T1, T2, result).
 3. Forecast panel (/var/www/html/forecast.html): BUY CE/PE box removed, reads via the
    gateway with no token.
 4. Trade Audit (kadima/trade_audit.py): every Gamma Blast with Gamma score above 50
    (or final score 60+) is saved as a suggestion with spec levels: entry = LTP,
    stop 35% below entry, T1 = 1.5x entry, T2 = 2x entry. Needs a kadima restart.

Trading is unchanged: auto-trading, stops, exits and the session scheduler do not use
the gateway. Anchor-based and fail-closed per file (a file whose anchors do not match
is left untouched and reported). Backups: <file>.bak-readonly-20261020. Idempotent.

Usage (on EC2, as root):
  python3 install_readonly_desk_20261020.py [--kadima ~ubuntu/kadima-py] [--rollback]
Then: systemctl restart kadima-dashboard; systemctl restart kadima (after market hours).
"""
import os, py_compile, shutil, sys, tempfile
from pathlib import Path

SUFFIX = '.bak-readonly-20261020'

GATEWAY = [
    ("def authenticated(request):\n    session = request.cookies.get(COOKIE, '')\n",
     "# READ-ONLY DESK v1 (2026-10-20): open view for any number of viewers; no sign-in, no controls.\n"
     "READ_ONLY_OPEN = True\n\n"
     "def authenticated(request):\n    if READ_ONLY_OPEN:\n        return\n"
     "    session = request.cookies.get(COOKIE, '')\n",
     'READ_ONLY_OPEN = True'),
    ("async def login(request: Request):\n    origin_check(request)\n",
     "async def login(request: Request):\n    origin_check(request)\n"
     "    if READ_ONLY_OPEN:\n        return JSONResponse({'ok': True, 'read_only': True})\n",
     "'read_only': True"),
    ("def manual_exit(signal_id: int, request: Request):\n    origin_check(request)\n",
     "def manual_exit(signal_id: int, request: Request):\n"
     "    if READ_ONLY_OPEN:\n        raise HTTPException(403, 'controls_removed')\n"
     "    origin_check(request)\n",
     "def manual_exit(signal_id: int, request: Request):\n    if READ_ONLY_OPEN:"),
    ("async def celestial_write(path: str, request: Request):\n    origin_check(request)\n",
     "async def celestial_write(path: str, request: Request):\n"
     "    if READ_ONLY_OPEN:\n        raise HTTPException(403, 'controls_removed')\n"
     "    origin_check(request)\n",
     "async def celestial_write(path: str, request: Request):\n    if READ_ONLY_OPEN:"),
    ("@app.get('/{name}')\ndef read_data(name: str, request: Request):\n",
     "@app.get('/forecast/{sub}')\n"
     "def forecast_read(sub: str, request: Request):\n"
     "    # READ-ONLY DESK v1: forecast panel reads without a control token.\n"
     "    authenticated(request)\n"
     "    if sub not in ('hourly', 'days'):\n"
     "        raise HTTPException(404, 'unknown_route')\n"
     "    from urllib.parse import urlencode\n"
     "    items = list(request.query_params.multi_items())\n"
     "    if (len(items) != len({k for k, _ in items}) or\n"
     "            any(k not in {'index', 'day'} or not re.fullmatch(r'[A-Za-z0-9_-]{1,20}', v) for k, v in items)):\n"
     "        raise HTTPException(422, 'forecast_invalid_filter')\n"
     "    return upstream(8081, '/api/forecast/' + sub + ('?' + urlencode(items) if items else ''))\n\n"
     "@app.get('/{name}')\ndef read_data(name: str, request: Request):\n",
     "def forecast_read("),
]

DESK_JS = r"""<!-- KADIMA READ-ONLY DESK v1 -->
<style>
#login,#logout,.controls,#positionTools,[data-buy],[data-exit],#positions button,#exitMessage,
#kadima-stock-fast select,#kadima-stock-fast button{display:none!important}
#candidates th:last-child,#candidates td:last-child{display:none}
@keyframes gbBlink{0%,49%{background:#ffd400;color:#111}50%,100%{background:#7a0019;color:#fff}}
#candidates tr.gb-hot td{animation:gbBlink 1s step-end infinite;font-weight:700}
#candidates tr.gb-hot{outline:2px solid #ffd400;outline-offset:-2px}
#gbAlert{margin:0 0 12px;padding:12px 14px;border-radius:10px;font-weight:800;letter-spacing:.02em;
 background:#ffd400;color:#111;animation:gbBlink 1s step-end infinite}
#gbSuggest{margin:12px 0 18px;padding:12px;border:1px solid #ffd400;border-radius:10px}
#gbSuggest h3{margin:0 0 6px}#gbSuggest td,#gbSuggest th{white-space:nowrap}
#roBadge{display:inline-block;margin-left:8px;padding:2px 8px;border-radius:999px;font-size:12px;border:1px solid currentColor}
@media (prefers-reduced-motion:reduce){#candidates tr.gb-hot td,#gbAlert{animation:none;background:#ffd400;color:#111}}
</style>
<script>
(()=>{'use strict';
const $=id=>document.getElementById(id);
const fmt=v=>v===null||v===undefined||!isFinite(v)?'—':Number(v).toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2});
const ist=s=>{try{return s?new Date(s).toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata',hour12:false}):'—'}catch(e){return '—'}};
const istDay=()=>new Date(Date.now()+330*60000).toISOString().slice(0,10);
const clock=$('clock');if(clock){const b=document.createElement('span');b.id='roBadge';b.textContent='View only';clock.after(b)}
const desk=$('desk'),cand=$('candidates');let alertBox=null;
if(desk&&cand){const sec=cand.closest('section');if(sec){desk.prepend(sec);alertBox=document.createElement('div');
 alertBox.id='gbAlert';alertBox.hidden=true;alertBox.setAttribute('role','alert');sec.insertBefore(alertBox,sec.children[1]||null)}}
window.gammaAlert=rows=>{if(!alertBox)return;if(!rows.length){alertBox.hidden=true;return}
 alertBox.hidden=false;alertBox.textContent='⚡ GAMMA BLAST ABOVE 50 · '+rows.slice(0,6).map(r=>r.symbol+' · '+Number(r.gamma_score??r.final_score).toFixed(1)).join('   |   ');
 const s=$('gammaScroll');if(s)s.scrollTop=0};
const audit=$('tradeAudit');if(!audit)return;
const box=document.createElement('div');box.id='gbSuggest';
box.innerHTML='<h3>Gamma Blast suggestions · today</h3><p class="note">Every Gamma Blast with score above 50. Spec levels: stop 35% below entry, T1 = 1.5× entry, T2 = 2× entry. Results are quote observations, not fills.</p>'+
 '<div class="scroll" tabindex="0" role="region" aria-label="Gamma Blast suggestions" style="max-height:340px;overflow:auto"><table><thead><tr><th>Contract</th><th>Suggested IST</th><th>Score</th><th>Entry ₹</th><th>Stop ₹</th><th>T1 ₹</th><th>T2 ₹</th><th>Last LTP ₹</th><th>High ₹</th><th>Result</th><th>Execution</th></tr></thead><tbody></tbody></table></div><p class="note" id="gbSuggestStatus">Loading…</p>';
const bar=audit.querySelector('.toolbar');if(bar)bar.after(box);else audit.prepend(box);
const body=box.querySelector('tbody'),status=box.querySelector('#gbSuggestStatus');
async function load(){const c=new AbortController(),t=setTimeout(()=>c.abort(),10000);
 try{const r=await fetch('/dashboard-api/audit?day='+istDay()+'&strategy=GAMMA&status=ALL&limit=50&offset=0',{credentials:'same-origin',cache:'no-store',signal:c.signal});
  if(!r.ok)throw Error('HTTP '+r.status);const d=await r.json();body.replaceChildren();
  for(const r of d.rows||[]){const p=r.plan||{},tr=document.createElement('tr');
   tr.className=r.observed_result==='SL_OBSERVED'?'audit-sl':r.observed_result==='T2_OBSERVED'?'audit-t2':r.observed_result==='T1_OBSERVED'?'audit-t1':'';
   [(p.strike?fmt(p.strike)+' '+(r.direction||'')+' · ':'')+r.symbol,ist(r.suggested_at),fmt(r.score),fmt(p.entry??p.entry_reference),fmt(p.stop),fmt(p.t1),fmt(p.t2),fmt(r.ltp),fmt(r.high_ltp),
    String(r.observed_result||'—').replaceAll('_',' '),r.execution_status||'—'].forEach(v=>{const td=document.createElement('td');td.textContent=v;tr.append(td)});body.append(tr)}
  if(!(d.rows||[]).length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=11;td.textContent='No Gamma Blast above 50 yet today.';tr.append(td);body.append(tr)}
  status.textContent=(d.total??0)+' Gamma suggestions today · updated '+ist(new Date().toISOString())}
 catch(e){status.textContent='Gamma suggestions unavailable: '+e.message}finally{clearTimeout(t)}}
load();setInterval(load,20000);
})();
</script>
"""

DASHBOARD = [
    ("candidates:data.candidates.filter(r=>liveView.recent(r.last_updated)).slice(0,20)}",
     "candidates:data.candidates.filter(r=>liveView.recent(r.last_updated)).sort((a,b)=>Number(Number(b.gamma_score??b.final_score)>50)-Number(Number(a.gamma_score??a.final_score)>50)).slice(0,20)}",
     ".sort((a,b)=>Number(Number(b.gamma_score"),
    ("for(const r of data.candidates){const tr=document.createElement('tr');[r.rank,",
     "const gbHot=r=>Number(r.gamma_score??r.final_score)>50;"
     "data.candidates=[...data.candidates].sort((a,b)=>(gbHot(b)-gbHot(a))||(gbHot(a)&&gbHot(b)?Number(b.gamma_score??b.final_score)-Number(a.gamma_score??a.final_score):0));"
     "(window.gammaAlert||(()=>{}))(data.candidates.filter(gbHot));"
     "for(const r of data.candidates){const tr=document.createElement('tr');if(gbHot(r))tr.className='gb-hot';[r.rank,",
     "const gbHot="),
    ("</body>", DESK_JS + "</body>", "KADIMA READ-ONLY DESK v1"),
]

FORECAST = [
    ("  const API = (location.pathname.startsWith('/control/') ? '' : '/control') + '/api/forecast';",
     "  const API = '/dashboard-api/forecast';   // READ-ONLY DESK v1: no token, no trading",
     "READ-ONLY DESK v1: no token"),
    ("    if (!token && !getToken()) throw new Error('No token');\n"
     "    const r = await fetch(API + path, {...opts, headers: {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}});",
     "    if ((opts.method || 'GET') !== 'GET') throw new Error('Controls removed (view-only desk)');\n"
     "    const r = await fetch(API + path, {...opts, credentials: 'same-origin', cache: 'no-store', headers: {'Content-Type': 'application/json'}});",
     "Controls removed (view-only desk)"),
    ("</style>", "#trade{display:none!important}</style>", "#trade{display:none!important}"),
]

AUDIT = [
    ("    for row in celestial.ranked_candidates(candidates=rows):\n"
     "        if row['final_score'] >= 60:\n"
     "            record(dict(row, score=row['final_score']), 'GAMMA', producer)\n",
     "    # 2026-10-20 trader rule: every Gamma Blast with Gamma score above 50 (or final\n"
     "    # score 60+) is a suggestion, saved with spec levels: entry = LTP, stop 35% below\n"
     "    # entry, T1 = 1.5x entry, T2 = 2x entry.\n"
     "    for row in celestial.ranked_candidates(candidates=rows):\n"
     "        gamma = row.get('gamma_score')\n"
     "        gamma = gamma if type(gamma) in (int, float) and math.isfinite(gamma) else row['final_score']\n"
     "        if row['final_score'] >= 60 or gamma > 50:\n"
     "            ltp = number(row.get('option_ltp'), .000001)\n"
     "            spec = {} if ltp is None or row.get('entry') is not None else dict(\n"
     "                entry=ltp, stop=round(ltp * 0.65, 2), t1=round(ltp * 1.5, 2), t2=round(ltp * 2.0, 2))\n"
     "            record(dict(row, score=row['final_score'], **spec), 'GAMMA', producer)\n",
     "gamma > 50:"),
]


def stage(text, patches, label):
    for old, new, marker in patches:
        if marker in text:
            continue
        if text.count(old) != 1:
            print(f'SKIPPED {label}: anchor found {text.count(old)}x (expected 1); file left unchanged:\n  {old[:120]!r}')
            return None
        text = text.replace(old, new)
    return text


def main():
    kadima = Path(sys.argv[sys.argv.index('--kadima') + 1]).expanduser() if '--kadima' in sys.argv \
        else Path('/home/ubuntu/kadima-py')
    targets = [(Path('/opt/kadima-dashboard/gateway.py'), GATEWAY),
               (Path('/var/www/html/dashboard.html'), DASHBOARD),
               (Path('/var/www/html/forecast.html'), FORECAST),
               (kadima / 'kadima/trade_audit.py', AUDIT)]
    if '--rollback' in sys.argv:
        for path, _ in targets:
            bak = Path(str(path) + SUFFIX)
            if bak.exists():
                shutil.copy2(bak, path); print('restored', path)
        return
    for path, patches in targets:
        if not path.exists():
            print('SKIPPED', path, '(not found)'); continue
        original = path.read_text()
        text = stage(original, patches, str(path))
        if text is None:
            continue
        if text == original:
            print('unchanged', path); continue
        if path.suffix == '.py':
            fd, tmp = tempfile.mkstemp(suffix='.py'); os.close(fd)
            Path(tmp).write_text(text); py_compile.compile(tmp, doraise=True); os.unlink(tmp)
        if not Path(str(path) + SUFFIX).exists():
            shutil.copy2(path, str(path) + SUFFIX)
        st = path.stat()
        path.write_text(text)
        os.chown(path, st.st_uid, st.st_gid) if os.geteuid() == 0 else None
        print('wrote', path)


if __name__ == '__main__':
    main()
