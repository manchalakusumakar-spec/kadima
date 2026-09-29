#!/usr/bin/env python3
"""Run one allowlisted legacy suite with network/session/credentials disabled."""
import os
from pathlib import Path
import runpy
import sys
import tempfile
import types

ROOT=Path(__file__).resolve().parents[1]
if len(sys.argv)!=2 or sys.argv[1] not in ('test_offline.py','test_audit_fixes.py'):
    raise SystemExit('Choose test_offline.py or test_audit_fixes.py')
sys.path.insert(0,str(ROOT))
os.environ['KADIMA_DATA_DIR']=tempfile.mkdtemp(prefix='cas-regression-')
os.environ['KADIMA_CAS_EXECUTION']='0'
for k in list(os.environ):
    if k.startswith(('ANGEL_','TELEGRAM_')) or k=='KADIMA_API_TOKEN':
        os.environ.pop(k,None)
def deny(*args,**kwargs):
    raise AssertionError('Broker/network forbidden in offline regression')
def guard(event,args):
    if event.startswith('socket.') or event in ('subprocess.Popen','os.system'):
        deny()
sys.addaudithook(guard)
x=types.ModuleType('kadima.session');x.SESSION=types.SimpleNamespace(ensure=deny,api=None)
sys.modules['kadima.session']=x
x=types.ModuleType('kadima.alerts');x.enqueue=lambda *a,**k:None;x.send=x.enqueue
sys.modules['kadima.alerts']=x
runpy.run_path(str(ROOT/'tools'/sys.argv[1]),run_name='__main__')
