"""Private AWS setup: prompts locally; never prints the OpenAI API key."""
import getpass
import json
import os
from pathlib import Path
import pwd
import re
import tempfile


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run with sudo -n python3 configure.py')
    path = Path('/etc/kadima/forecaster.json')
    existing = json.loads(path.read_text()) if path.exists() else {}
    print('Observation mode only. Model requests send selected market data/headlines to OpenAI.')
    print('API requests can incur charges: one batch every 5 minutes, at most 100 attempts/day.')
    model = input('Model [gpt-5-mini]: ').strip() or 'gpt-5-mini'
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', model):
        raise SystemExit('Invalid model name; configuration unchanged.')
    key = getpass.getpass('OpenAI API key (hidden; blank keeps existing key or leaves waiting): ').strip()
    key = key or existing.get('api_key','')
    if any(c.isspace() for c in key) or len(key)>512:
        raise SystemExit('Invalid key format; configuration unchanged.')
    data = dict(enabled=True,model=model,api_key=key,mode='observation')
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o750)
    group = pwd.getpwnam('ubuntu').pw_gid
    os.chown(path.parent,0,group)
    os.chmod(path.parent,0o750)
    fd, temporary = tempfile.mkstemp(dir=path.parent,prefix='.forecaster-')
    try:
        with os.fdopen(fd,'w') as f:
            json.dump(data,f)
            f.flush()
            os.fsync(f.fileno())
        os.chown(temporary,0,group)
        os.chmod(temporary,0o640)
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print('Saved private configuration. Observer reloads within 5 minutes; no service restart needed.')
    print('Key configured.' if key else 'No key configured: forecasts remain waiting_for_api_key.')
    print('No orders sent. Forecast exit deferral is not enabled by this setup.')


if __name__=='__main__':
    main()
