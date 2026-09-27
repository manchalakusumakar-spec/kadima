"""Offline entry-pause checks; no systemd, server or broker access."""
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('prepare_maintenance_test',ROOT/'momentum_recovery/prepare_maintenance.py')
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
NOW=datetime(2026,9,27,11,0,tzinfo=timezone.utc)


class SnapshotTests(unittest.TestCase):
    def data(self):
        return [dict(ts=NOW.isoformat(),config=dict(auto_trade_enabled=True,kill_latched=False),positions=[]),
                dict(ts=NOW.isoformat(),controls=dict(live_enabled=True,revision=7))]

    def test_empty_weekend_api_never_certifies_broker_flatness(self):
        with patch.object(m,'api',side_effect=self.data()): r=m.snapshot('TEST',NOW)
        self.assertEqual(r['api_position_count'],0)
        self.assertIs(r['broker_flat_verified'],False)

    def test_stale_and_future_controls_are_refused(self):
        for seconds in (-31,1):
            for index in (0,1):
                data=self.data();data[index]['ts']=(NOW+timedelta(seconds=seconds)).isoformat()
                with self.subTest(seconds=seconds,index=index),patch.object(m,'api',side_effect=data):
                    with self.assertRaises(m.Blocked):m.snapshot('TEST',NOW)

    def test_missing_or_boolean_revision_refused(self):
        for revision in (None,True,'7'):
            data=self.data();data[1]['controls']['revision']=revision
            with self.subTest(revision=revision),patch.object(m,'api',side_effect=data):
                with self.assertRaises(m.Blocked):m.snapshot('TEST',NOW)

    def test_unavailable_position_list_is_not_empty(self):
        data=self.data();data[0]['positions']=None
        with patch.object(m,'api',side_effect=data):
            with self.assertRaises(m.Blocked):m.snapshot('TEST',NOW)

    def test_resume_squareoff_and_arbitrary_routes_are_impossible(self):
        for path in ('resume','../order/squareoff-all','config','https://example.test','pause'):
            with self.subTest(path=path),self.assertRaises(m.Blocked): m.api(path,'TEST')
        for path in ('resume','../order/squareoff-all','config','top20'):
            with self.subTest(path=path),self.assertRaises(m.Blocked):m.api(path,'TEST',{})


class PauseTests(unittest.TestCase):
    def setUp(self):
        self.calls=[]
        self.state=dict(auto_trade_enabled=True,stock_live_enabled=True,stock_revision=4,
                        kill_latched=False,api_position_count=1,broker_flat_verified=False)
        self.timer_held=False
        self.pause_count=0
        self.patchers=[]
        def start(name,value):
            p=patch.object(m,name,value);p.start();self.addCleanup(p.stop)
        start('engine_identity',lambda:('100','222'))
        start('snapshot',lambda token:self.state.copy())
        start('hold_timers',lambda:setattr(self,'timer_held',True))
        start('verify_timer_hold',lambda:self.assertTrue(self.timer_held))
        start('api',self.api)

    def api(self,path,token,payload=None):
        self.calls.append((path,payload))
        self.assertTrue(self.timer_held)
        if path=='pause':
            self.state['auto_trade_enabled']=False
            self.state['stock_revision']=5
            return dict(auto_trade_enabled=False)
        if path=='stock-fast/config':
            self.assertEqual(payload,{'patch':{'live_enabled':False},'expected_revision':5})
            self.state['stock_live_enabled']=False
            return dict(live_enabled=False)
        raise AssertionError(path)

    def test_pause_with_open_position_keeps_protection_running(self):
        result=m.pause_entries('TEST')
        self.assertEqual([p for p,v in self.calls],['pause','stock-fast/config'])
        self.assertEqual(result['api_position_count'],1)
        self.assertFalse(result['broker_flat_verified'])

    def test_existing_kill_is_preserved(self):
        self.state['kill_latched']=True
        self.assertTrue(m.pause_entries('TEST')['kill_latched'])

    def test_stock_already_paused_needs_no_revision_write(self):
        self.state['stock_live_enabled']=False
        m.pause_entries('TEST')
        self.assertEqual([p for p,v in self.calls],['pause'])

    def test_unconfirmed_global_pause_prevents_stock_write(self):
        with patch.object(m,'api',return_value={'auto_trade_enabled':True}):
            with self.assertRaisesRegex(m.Blocked,'global_pause_unconfirmed'):m.pause_entries('TEST')
        self.assertTrue(self.state['stock_live_enabled'])

    def test_revision_conflict_does_not_retry_or_restore_entries(self):
        def api(path,token,payload=None):
            if path=='stock-fast/config': raise ValueError('conflict')
            return self.api(path,token,payload)
        with patch.object(m,'api',api):
            with self.assertRaises(ValueError):m.pause_entries('TEST')
        self.assertFalse(self.state['auto_trade_enabled'])
        self.assertTrue(self.timer_held)
        self.assertNotIn('resume',[p for p,v in self.calls])

    def test_engine_restart_blocks_before_control_write(self):
        with patch.object(m,'engine_identity',side_effect=[('100','222'),('101','333')]):
            with self.assertRaisesRegex(m.Blocked,'engine_changed'):m.pause_entries('TEST')
        self.assertEqual(self.calls,[])

    def test_timer_failure_prevents_control_mutation(self):
        with patch.object(m,'hold_timers',side_effect=m.Blocked('timer_pause_failed')):
            with self.assertRaises(m.Blocked):m.pause_entries('TEST')
        self.assertEqual(self.calls,[])


class TimerTests(unittest.TestCase):
    def test_only_exact_timers_can_be_disabled(self):
        observations={name:dict(LoadState='loaded',ActiveState='inactive',UnitFileState='disabled',MainPID='0')
                      for name in m.TIMERS+m.WORKERS}
        calls=[]
        with patch.object(m,'unit',lambda name:observations[name]),patch.object(m.subprocess,'run',
                lambda cmd,**kw:calls.append(cmd) or SimpleNamespace(returncode=0)):
            m.hold_timers()
        self.assertEqual(calls,[['/usr/bin/systemctl','disable','--now',name] for name in m.TIMERS])
        self.assertFalse(any(name in str(calls) for name in m.SERVICES))

    def test_active_worker_is_not_killed(self):
        with (patch.object(m,'unit',return_value=dict(LoadState='loaded',ActiveState='active',MainPID='9')),
                patch.object(m.subprocess,'run') as run):
            with self.assertRaises(m.Blocked):m.hold_timers()
            run.assert_not_called()


class InventoryTests(unittest.TestCase):
    def test_source_content_and_secrets_are_never_printed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'kadima').mkdir()
            (root/'run.py').write_text('secret-example')
            (root/'kadima/config.py').write_text('token-example')
            with patch.object(m,'TARGET',root): report=m.source_inventory()
            self.assertNotIn('secret-example',str(report))
            self.assertNotIn('token-example',str(report))
            self.assertEqual(len(report['run.py']),40)

    def test_linked_files_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'real').write_text('private')
            (root/'link').symlink_to(root/'real')
            with self.assertRaises(m.Blocked):m.read_regular(root/'link')


if __name__=='__main__':unittest.main()
