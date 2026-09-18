"""Apply additive live dashboard changes without replacing installed engine code."""
import argparse
import ast
from datetime import datetime, timezone
from pathlib import Path
import os

HERE = Path(__file__).resolve().parent
MARKER = '// KADIMA LIVE INPUTS V1'

def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError('Unsupported installed version: expected one anchor ' + old[:90])
    return text.replace(old, new, 1)

def background_universe(text):
    """Remove only the universe presentation; keep polling and ranking intact."""
    if 'id="legacy-universe"' not in text:
        if 'function renderUniverse(){' in text or 'renderUniverse();' in text:
            raise ValueError('Incomplete universe panel removal')
        return text
    panel = '<section class="panel wide"><div class="split"><h2>F&O Universe</h2><span id="legacy-universeCount" class="muted"></span></div><div id="legacy-universe" class="wall"></div></section>'
    text = replace_once(text, panel, '<!-- F&O universe scanning continues in the background. -->')
    start = text.index('function renderUniverse(){')
    end = text.index('function renderCommodities(){', start)
    text = text[:start] + text[end:]
    return replace_once(text, 'renderMacro();renderUniverse();renderCommodities();', 'renderMacro();renderCommodities();')

def dashboard(text):
    if MARKER in text:
        return background_universe(text)
    text = replace_once(text, "'use strict';", "'use strict';\n" + MARKER + '\n' + (HERE/'live_view.js').read_text())
    text = replace_once(text, '<th>Control</th>', '<th>Score-based probability</th><th>Control</th>')
    start = text.index('function updateGammaPlans(){')
    end = text.index('\nfor(const id', start)
    text = text[:start] + '''function updateGammaPlans(){
if(!signedIn||!online||!snapshot?.config.rollout_enabled)return;
const rows=el('candidates').tBodies[0].rows;
for(const [i,r] of snapshot.candidates.entries()){
 if(rows[i]?.cells.length!==25)continue;
 const current=online&&liveView.recent(r.last_updated);
 gammaPlan(r,{...planSettings(),current,positions:snapshot.positions,positionsCurrent:online&&liveView.recent(snapshot.ts)}).forEach((v,j)=>rows[i].cells[6+j].textContent=v);
 rows[i].cells[23].textContent=liveView.probability(r.final_score,current);
 if(!current){rows[i].cells[21].textContent='STALE / UNAVAILABLE';const buy=rows[i].querySelector('[data-buy]');if(buy){buy.disabled=true;buy.dataset.ready='false';}}
}}
''' + text[end:]
    text = text.replace('cells.length===24', 'cells.length===25').replace('empty(body,24,', 'empty(body,25,').replace('emptyGlobal(body,24,', 'emptyGlobal(body,25,')
    text = replace_once(text, "timeIST(r.last_updated)].forEach", "timeIST(r.last_updated),liveView.probability(r.final_score,online&&liveView.recent(r.last_updated))].forEach")
    text = replace_once(text, "stamp(r.last_updated),'Read only'", "stamp(r.last_updated),liveView.probability(r.score,current),'Read only'")
    text = replace_once(text, "scores are not probabilities", "score-based probability = Gamma score / 100; not a measured win rate")
    text = replace_once(text, '<p class="note">Missing news coverage', '<p class="note">Score-based probability uses the existing 0–100 Final score in Celestial, or Gamma score in the legacy watchlist. It is a score-derived confidence estimate, not a measured chance of profit.</p><p class="note">Missing news coverage')
    # Existing feed loop prevents overlap and clears data on sign-out.
    text = replace_once(text, "universe:{path:'universe_snapshot',label:'Scanner',ms:20000},macro:{path:'macro_snapshot',label:'Macro',ms:20000}", "universe:{path:'universe_snapshot',label:'Scanner',ms:5000},macro:{path:'macro_snapshot',label:'Macro',ms:5000},publicNews:{path:'live_news',label:'Public news',ms:15000}")
    text = replace_once(text, "return [...idx,...stk].filter(r=>r.score>0", "return [...idx,...stk].filter(r=>onlineLegacy(r.feed)&&liveView.recent(r.last_updated)&&r.score>0")
    text = replace_once(text, "kind:'Index',feed:'state'", "best_contract:d.gamma.best_contract,kind:'Index',feed:'state'")
    text = replace_once(text, "const cfg=data.config;", "const cfg=data.config;data={...data,candidates:data.candidates.filter(r=>liveView.recent(r.last_updated)).slice(0,20)};")
    # Snapshot must match visible row indices for the plan updates.
    text = replace_once(text, "const body=el('candidates').tBodies[0];body.replaceChildren();", "snapshot=data;const body=el('candidates').tBodies[0];body.replaceChildren();")
    text = replace_once(text, "$('news').innerHTML='<li>", "$('news').innerHTML=liveView.news(feeds.publicNews.data,onlineLegacy('publicNews'))+'<li>")
    text = replace_once(text, "html+='<details><summary>'+esc(a.name)", "html+='<details open><summary>'+esc(a.name)")
    text = replace_once(text, "+'</p>'+table(['Target time IST'", "+'</p>'+liveView.forecast(a,feeds.state.data?.indices?.[a.name])+table(['Target time IST'")
    text = replace_once(text, "[f,onlineLegacy('dashboard'),expired]", "[f,onlineLegacy('dashboard'),expired,feeds.state.data?.indices,Math.floor(Date.now()/1000)]")
    text = replace_once(text, "if(r){\nhtml+=", "if(!r)html+='<p>No projected path available. '+(f.status==='provider_http_429'?'Forecast provider rate limit / quota: waiting for the next scheduled attempt.':'Waiting for a valid forecast.')+'</p>';\nif(r){\nhtml+=")
    return background_universe(text)

ROUTE = '''
# KADIMA LIVE NEWS V1
@app.get('/live_news')
def live_news(request: Request):
    authenticated(request)
    try:
        with open('/var/lib/kadima-live-inputs/news.json') as handle:
            raw = handle.read(2000001)
        if len(raw) > 2000000:
            raise ValueError('oversized_news')
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError('invalid_news')
        return data
    except (OSError, ValueError):
        raise HTTPException(503, 'public_news_unavailable')

'''

def gateway(text):
    if '# KADIMA LIVE NEWS V1' in text:
        return text
    result = replace_once(text, "@app.get('/{name}')", ROUTE + "@app.get('/{name}')")
    ast.parse(result)
    return result

def atomic(path, text):
    tmp = path.with_name(path.name+'.live-inputs.tmp')
    tmp.write_text(text)
    tmp.chmod(path.stat().st_mode & 0o777)
    os.replace(tmp, path)

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--html', type=Path, required=True)
    p.add_argument('--gateway', type=Path, required=True)
    p.add_argument('--apply', action='store_true')
    args = p.parse_args()
    paths = [args.html, args.gateway]
    before = [f.read_text() for f in paths]
    after = [dashboard(before[0]), gateway(before[1])]
    if not args.apply:
        print('Compatible: HTML and gateway validated; pass --apply to write backups and apply.')
        return
    tag = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    changed = []
    try:
        for path, old, new in zip(paths, before, after):
            if old == new:
                continue
            backup = path.with_name(path.name+'.before-live-inputs-'+tag)
            backup.write_text(old)
            backup.chmod(path.stat().st_mode & 0o777)
            atomic(path, new)
            changed.append((path, old))
    except Exception:
        for path, old in reversed(changed):
            atomic(path, old)
        raise
    print('Updated', len(changed), 'files. Restart dashboard gateway after installing news service.')

if __name__ == '__main__':
    main()
