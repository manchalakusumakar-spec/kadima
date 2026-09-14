"""Read-only local forecast report. Run as ubuntu, no credentials required."""
import json
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

state=Path('/home/ubuntu/kadima-py/state')
for file in ('kadima_forecast_status.json','kadima_forecast_observations.json'):
    path=state/file
    if not path.exists():
        print(file+': not produced yet')
        continue
    d=json.loads(path.read_text())
    if file.endswith('status.json'):
        print('Observer:',d.get('status'),'Checked:',d.get('checked_at'))
        print('Validation:',json.dumps(d.get('metrics',{})))
        continue
    print('Observation only | Model:',d.get('model'),'| As of:',d.get('as_of'),'| Issued:',d.get('issued_at'))
    print('Expired:',datetime.fromisoformat(d['expires_at'])<datetime.now(ZoneInfo('Asia/Kolkata')))
    for a in d.get('assets',[]):
        print('\n'+a['name'],a['bias'],'heat',a['heat'])
        print(a['reason'])
        print('Invalidation:',a['invalidation'])
        for p in a['checkpoints']:
            print(p['target_at'],'low',p['low'],'expected',p['expected'],'high',p['high'])
    print('\nAvailability:',json.dumps(d.get('checklist_availability',{})))
    print('Excluded:',json.dumps(d.get('excluded_assets',{})))
    print('Exact-contract 30m scenarios:',json.dumps(d.get('premium_scenarios',[])))
    print('Held contracts without scenario:',json.dumps(d.get('held_contracts_without_scenario',[])))
