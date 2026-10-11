#!/usr/bin/env python3
"""Offline regression tests for truthful, side-effect-free preflight evidence."""
import importlib.util
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('preflight',Path(__file__).with_name('preflight_autotrade.py'))
p=importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)
now=datetime(2026,10,9,20,tzinfo=p.config.IST)
policy=dict(schema=1,valid_from='2026-01-01',valid_through='2026-12-31',
            holidays=['2026-10-12'],sources=['offline fixture'],
            arm_at='09:00',entries_at='09:15',stop_entries_at='15:35')
assert p.next_trading_day(now,policy).date().isoformat()=='2026-10-13'
env=p.parse_environment('KADIMA_API_TOKEN="REDACTED"\nKADIMA_FAST_MOMENTUM_LIVE=1\nUNRELATED_SECRET=REDACTED')
assert env=={'KADIMA_API_TOKEN':'REDACTED','KADIMA_FAST_MOMENTUM_LIVE':'1'}
with patch.object(p,'run',return_value=p.SimpleNamespace(returncode=0,stdout='Environment=KADIMA_SESSION_AUTOTRADE=1\nEnvironmentFiles=/one (ignore_errors=no)\nEnvironmentFiles=/two (ignore_errors=no)')), \
        patch.object(p,'read_private',side_effect=['KADIMA_FAST_MOMENTUM_LIVE=1','KADIMA_CELESTIAL_ENABLED=1']):
    values,files=p.service_environment()
    assert files==['/one','/two'] and all(values[k]=='1' for k in p.SWITCHES)
assert p.funds_value({'availablecash':'nan','net':'123'})==123
assert p.funds_value({'availablecash':'-1'}) is None
with tempfile.TemporaryDirectory() as directory:
    path=Path(directory)/'db.sqlite'
    c=sqlite3.connect(path);c.execute('create table sentinel(value)');c.commit();c.close()
    c=p.readonly_database(path)
    try:c.execute('insert into sentinel values(1)')
    except sqlite3.OperationalError:pass
    else:raise AssertionError('preflight database allowed writes')
    c.close()
    capture=Path(directory)/'books.json'
    data=dict(captured_at=now.isoformat(),positions=dict(status=True,data=[]),
              orders=dict(status=True,data=[]),rms=dict(status=True,data={'availablecash':'30000'}))
    capture.write_text(json.dumps(data))
    assert p.broker_capture(capture,now)['rms']['status']
    for stale in (now-timedelta(seconds=151),now+timedelta(seconds=1)):
        data['captured_at']=stale.isoformat();capture.write_text(json.dumps(data))
        try:p.broker_capture(capture,now)
        except ValueError:pass
        else:raise AssertionError('stale/future broker evidence accepted')
    data['captured_at']=now.isoformat();data['orders']['status']=False;capture.write_text(json.dumps(data))
    try:p.broker_capture(capture,now)
    except ValueError:pass
    else:raise AssertionError('failed broker book treated as flat')
    try:p.broker_capture(None,now)
    except ValueError:pass
    else:raise AssertionError('missing broker evidence treated as flat')
    from kadima import celestial, audit
    before=celestial.connection
    with p.read_only_gates(p.readonly_database(path),{'KADIMA_CELESTIAL_ENABLED':'1'}):
        assert celestial.connection is not before
        audit.log('test_must_not_write')
    assert celestial.connection is before
print('PASS: preflight read-only DB, safe environment parsing, calendar, RMS validation, stale/failed broker evidence, and suppressed gate writes')
