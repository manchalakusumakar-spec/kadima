#!/usr/bin/env python3
"""Run as ubuntu: python3 install.py. No trading-engine restart."""
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

BASE=Path(__file__).resolve().parent
ORIGIN='https://18-61-12-184.sslip.io'
TARGET=Path('/var/www/html/dashboard.html')
def run(args, **kwargs):
    return subprocess.run(args,check=True,**kwargs)
def main():
    if os.geteuid()==0:
        sys.exit('Run as ubuntu, without sudo; the installer uses sudo where required.')
    if getpass.getuser()!='ubuntu':
        sys.exit('This installer is configured for the ubuntu account.')
    run(['sudo','-v'])
    python=Path('/home/ubuntu/kadima-py/venv/bin/python3')
    if not python.exists():
        python=Path('/home/ubuntu/kadima-py/.venv/bin/python3')
    if not python.exists():
        sys.exit('Could not locate KADIMA virtual environment. No files changed.')
    run([str(python),'-c','import fastapi,uvicorn'])
    processes=json.loads(subprocess.check_output(['pm2','jlist'],text=True))
    procs=[p for p in processes if p.get('name')=='kadima-bridge']
    if len(procs)!=1:
        sys.exit('Expected one kadima-bridge process. No files changed.')
    env=procs[0].get('pm2_env',{})
    token=env.get('KADIMA_API_TOKEN') or env.get('env',{}).get('KADIMA_API_TOKEN')
    if not token:
        token=getpass.getpass('Existing KADIMA API token (hidden; stays on server): ').strip()
    if not token:
        sys.exit('API token is missing. No files changed.')
    req=urllib.request.Request('http://127.0.0.1:8080/state',headers={'Authorization':'Bearer '+token})
    try:
        with urllib.request.urlopen(req,timeout=10) as r:
            if r.status!=200: raise ValueError()
    except Exception:
        sys.exit('Existing API token did not pass the local state check. No files changed.')
    candidates=[]
    for f in Path('/etc/nginx/sites-enabled').glob('*'):
        if not f.is_file(): continue
        p=f.resolve();text=p.read_text()
        if '18-61-12-184.sslip.io' in text and re.search(r'location\s*=\s*/dashboard\.html\s*\{',text):
            candidates.append((p,text))
    if len(candidates)!=1:
        sys.exit('Could not uniquely identify the dashboard Nginx configuration. No files changed.')
    nginx,text=candidates[0]
    if '# KADIMA-DASHBOARD-GATEWAY-BEGIN' in text:
        sys.exit('Dashboard gateway already installed. Use the saved rollback before reinstalling.')
    if not TARGET.is_file(): sys.exit('Existing dashboard not found. No files changed.')
    password=getpass.getpass('Create a NEW dashboard password (at least 14 characters): ')
    if len(password)<14 or password!=getpass.getpass('Repeat dashboard password: '):
        sys.exit('Passwords did not match or were too short. No files changed.')
    salt=os.urandom(16)
    config={'origin':ORIGIN,'api_token':token,'salt':salt.hex(),'password_hash':hashlib.scrypt(password.encode(),salt=salt,n=16384,r=8,p=1).hex()}
    block='''# KADIMA-DASHBOARD-GATEWAY-BEGIN
    location ^~ /dashboard-api/ {
        proxy_pass http://127.0.0.1:8082/;
        proxy_set_header Host $host;
        proxy_set_header Origin $http_origin;
        proxy_set_header Cookie $http_cookie;
        proxy_read_timeout 12s;
        client_max_body_size 8k;
        add_header Cache-Control "no-store" always;
    }
    # KADIMA-DASHBOARD-GATEWAY-END
    '''
    updated=re.sub(r'(?=location\s*=\s*/dashboard\.html\s*\{)',lambda _:block,text,count=1)
    backup=Path('/var/backups/kadima-dashboard-'+time.strftime('%Y%m%d-%H%M%S'))
    run(['sudo','install','-d','-m','700',str(backup)])
    run(['sudo','cp','-p',str(TARGET),str(backup/'dashboard.html')])
    run(['sudo','cp','-p',str(nginx),str(backup/'nginx.conf')])
    service=Path('/etc/systemd/system/kadima-dashboard.service')
    if service.exists(): sys.exit('Gateway service already exists; stopped without replacing it.')
    rollback='#!/bin/sh\nset -eu\nsudo cp '+str(backup/'dashboard.html')+' '+str(TARGET)+'\nsudo cp '+str(backup/'nginx.conf')+' '+str(nginx)+'\nsudo nginx -t\nsudo systemctl reload nginx\nsudo systemctl disable --now kadima-dashboard.service\n'
    try:
        with tempfile.TemporaryDirectory() as tmp:
            temp=Path(tmp)
            for name,content in [('config.json',json.dumps(config)),('nginx.conf',updated),('rollback.sh',rollback),('kadima-dashboard.service',f'''[Unit]
Description=KADIMA dashboard session gateway
After=network.target
[Service]
User=ubuntu
Group=ubuntu
WorkingDirectory=/opt/kadima-dashboard
Environment=KADIMA_DASHBOARD_CONFIG=/etc/kadima-dashboard/config.json
ExecStart={python} -m uvicorn gateway:app --host 127.0.0.1 --port 8082 --no-access-log
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
[Install]
WantedBy=multi-user.target
''')]:
                path=temp/name;path.write_text(content);path.chmod(0o600)
            run(['sudo','install','-d','-m','755','/opt/kadima-dashboard'])
            run(['sudo','install','-d','-m','750','-o','root','-g','ubuntu','/etc/kadima-dashboard'])
            run(['sudo','install','-m','640','-o','root','-g','ubuntu',str(temp/'config.json'),'/etc/kadima-dashboard/config.json'])
            run(['sudo','install','-m','644',str(BASE/'gateway.py'),'/opt/kadima-dashboard/gateway.py'])
            run(['sudo','install','-m','644',str(temp/'kadima-dashboard.service'),str(service)])
            run(['sudo','install','-m','700',str(temp/'rollback.sh'),str(backup/'rollback.sh')])
            run(['sudo','systemctl','daemon-reload'])
            run(['sudo','systemctl','enable','--now','kadima-dashboard.service'])
            for attempt in range(10):
                try:
                    with urllib.request.urlopen('http://127.0.0.1:8082/health',timeout=2) as r:
                        assert json.load(r)['ok']
                    break
                except Exception:
                    if attempt==9: raise RuntimeError('Gateway health check failed')
                    time.sleep(1)
            run(['sudo','install','-m','644',str(temp/'nginx.conf'),str(nginx)])
            run(['sudo','nginx','-t'])
            run(['sudo','install','-m','644',str(BASE/'dashboard.html'),str(TARGET)])
            run(['sudo','systemctl','reload','nginx'])
        print('Installed. Open '+ORIGIN+'/dashboard.html and sign in with the NEW dashboard password.')
        print('Rollback: sudo sh '+str(backup/'rollback.sh'))
        print('The existing API token remains unchanged. It was embedded in the old HTML; rotate it for every existing client in a separate coordinated update.')
    except Exception:
        run(['sudo','cp','-p',str(backup/'dashboard.html'),str(TARGET)])
        run(['sudo','cp','-p',str(backup/'nginx.conf'),str(nginx)])
        subprocess.run(['sudo','nginx','-t'])
        subprocess.run(['sudo','systemctl','reload','nginx'])
        subprocess.run(['sudo','systemctl','disable','--now','kadima-dashboard.service'])
        sys.exit('Installation failed; original dashboard and Nginx configuration restored. Trading engine was not restarted.')
if __name__=='__main__': main()
