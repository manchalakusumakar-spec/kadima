"""Add an observation forecast panel without replacing the deployed dashboard."""
import os
from pathlib import Path
import shutil
import tempfile

PANEL = '<section class="panel wide"><h2>KADIMA Checklist Forecast · Observation</h2><div id="forecastPanel"></div></section>\n'
SCRIPT = r'''
let forecastRenderKey='';
function renderForecast(){
const f=feeds.dashboard.data?.forecaster, r=f?.latest;
const expired=r?.expires_at ? Date.now()>Date.parse(r.expires_at) : true;
const key=JSON.stringify([f,online('dashboard'),expired]);if(key===forecastRenderKey)return;forecastRenderKey=key;
const box=$('forecastPanel');if(!box)return;
if(!f){box.innerHTML=empty('Forecaster status unavailable');return}
let html='<p><strong>Observation only — no order or exit authority.</strong></p><p>'+esc(f.status)+' · Checked '+stamp(f.checked_at)+'</p>';
if(!online('dashboard'))html+='<p class="warn">Dashboard disconnected · last received forecast below.</p>';
if(r){
html+='<p class="'+(expired?'warn':'muted')+'">'+(expired?'EXPIRED · LAST KNOWN':'Recorded forecast')+' · Model '+esc(r.model)+'<br>Inputs as of '+stamp(r.as_of)+'<br>Issued '+stamp(r.issued_at)+' · Horizon ends '+stamp(r.expires_at)+'</p>';
for(const a of r.assets||[]){
html+='<details><summary>'+esc(a.name)+' · '+esc(a.bias)+' · heat '+esc(a.heat)+' / 5</summary><p>'+esc(a.reason)+'</p><small>Spot '+stamp(a.source_times?.spot_at)+' · Technicals '+stamp(a.source_times?.technicals_at)+' · Chain '+stamp(a.source_times?.option_chain_at)+'</small><p>'+esc((a.missing_inputs||[]).join('; '))+'</p><p>Invalidation: '+esc(a.invalidation)+'</p>'+table(['Target time IST','Low scenario','Expected','High scenario'],(a.checkpoints||[]).map(p=>'<tr><td>'+stamp(p.target_at)+'</td><td>'+fmt(p.low)+'</td><td>'+fmt(p.expected)+'</td><td>'+fmt(p.high)+'</td></tr>'))+'</details>';
}
for(const p of r.premium_scenarios||[]){html+='<p>30-minute option scenario: '+esc(p.symbol||'Unavailable')+' · '+esc(p.status)+(p.status==='scenario_only'?' · '+money(p.expected_premium)+' at '+stamp(p.target_at)+'<br>'+esc(p.assumption):'')+'</p>'}
if((r.held_contracts_without_scenario||[]).length)html+='<p class="warn">No valid premium scenario: '+esc(r.held_contracts_without_scenario.join(', '))+'</p>';
html+='<details><summary>Checklist availability and exclusions</summary>'+Object.entries(r.checklist_availability||{}).map(([k,v])=>'<p>'+esc(k.replaceAll('_',' '))+': '+esc(v)+'</p>').join('')+Object.entries(r.excluded_assets||{}).map(([k,v])=>'<p>'+esc(k)+': '+esc(v)+'</p>').join('')+'</details>';
}
const m=f.metrics||{};html+='<p>Validation: '+esc(m.scored_checkpoints??0)+' / '+esc(m.total_checkpoints??0)+' checkpoints scored. Scenarios and heat are not probabilities.</p>';
if((m.by_asset_horizon||[]).length)html+='<details><summary>Forecast vs actual</summary>'+table(['Asset','Minutes','Samples','Forecast MAE','Unchanged-price MAE','Range hits'],m.by_asset_horizon.map(x=>'<tr><td>'+esc(x.name)+'</td><td>'+esc(x.minute)+'</td><td>'+esc(x.n)+'</td><td>'+fmt(x.mae_points)+'</td><td>'+fmt(x.unchanged_price_mae_points)+'</td><td>'+fmt(x.scenario_range_hit_fraction*100)+'%</td></tr>'))+'<p>'+esc(m.note)+'</p></details>';
box.innerHTML=html;
}
'''


def transform(s):
    if 'id="forecastPanel"' in s:
        if 'function renderForecast()' not in s:
            raise ValueError('Partial forecast panel; manual review required')
        return s
    anchor='<section class="panel wide"><h2>Data Coverage</h2>'
    if s.count(anchor)!=1 or s.count('function renderAll(){')!=1:
        raise ValueError('Unrecognized dashboard layout; no changes')
    s=s.replace(anchor,PANEL+anchor,1)
    s=s.replace('function renderAll(){',SCRIPT+'\nfunction renderAll(){renderForecast();',1)
    s=s.replace('ranked candidate strikes, next-hour forecasts, per-position','ranked candidate strikes, per-position')
    return s


def main():
    if os.geteuid()!=0:
        raise SystemExit('Run with sudo -n python3 update_dashboard.py')
    path=Path('/var/www/html/dashboard.html')
    old=path.read_text();new=transform(old)
    if old==new:
        print('Forecast panel already installed.')
        return
    backup=Path(tempfile.mkdtemp(prefix='kadima-forecast-panel-',dir='/var/backups'))
    shutil.copy2(path,backup/'dashboard.html')
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.dashboard-forecast-')
    try:
        with os.fdopen(fd,'w') as f:
            f.write(new)
        os.chmod(tmp,path.stat().st_mode & 0o777)
        os.chown(tmp,path.stat().st_uid,path.stat().st_gid)
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    print('Forecast panel installed. Refresh dashboard with Ctrl+F5.')
    print('Backup:',backup/'dashboard.html')


if __name__=='__main__':
    main()
