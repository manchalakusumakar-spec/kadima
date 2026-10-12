"""Run one existing suite in a temporary home with outbound sockets denied."""
import os
from pathlib import Path
import runpy
import socket
import sys
import tempfile

ALLOWED = {'test_hourly_forecast.py','test_hourly_forecast_201.py',
           'test_forecast_manual_buy.py','test_audit_fixes.py','test_offline.py',
           'test_oi_confirmation.py','test_shield_v9.py','test_timing.py','test_cas_executor.py'}

def deny(*a,**kw): raise RuntimeError('outbound sockets disabled in forecast CI')

def main():
    if len(sys.argv)!=2 or sys.argv[1] not in ALLOWED:
        raise SystemExit('choose a reviewed offline test filename')
    root=Path(__file__).resolve().parents[1]
    script=root/'tools'/sys.argv[1]
    if not script.is_file(): raise SystemExit('existing suite unavailable: '+script.name)
    with tempfile.TemporaryDirectory(prefix='precision-offline-') as home:
        os.environ.update(HOME=home,KADIMA_DATA_DIR=home,ORDER_PLACEMENT_ENABLED='0',
                          KADIMA_CELESTIAL_ENABLED='0',KADIMA_INTRADAY_TREND_LIVE='0')
        socket.socket.connect=deny; socket.socket.connect_ex=deny; socket.create_connection=deny
        socket.socket.sendto=deny; socket.getaddrinfo=deny
        sys.path.insert(0,str(root)); sys.argv=[str(script)]
        runpy.run_path(str(script),run_name='__main__')

if __name__=='__main__': main()
