import importlib.util
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,Mock
from datetime import datetime
from zoneinfo import ZoneInfo
from test_server_resources import gateway

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('operations_under_test',ROOT/'operations/control.py')
ops=importlib.util.module_from_spec(spec);spec.loader.exec_module(ops)


class Operations(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        for target,value in [('ROOT',Path(self.tmp.name)),('protected',Mock(return_value=False)),
                ('unit_state',Mock(return_value=dict(LoadState='loaded',ActiveState='active',SubState='running',MainPID='123'))),
                ('run',Mock(return_value=SimpleNamespace(stdout=''))),('cancel_delay',Mock()),
                ('engine_process',Mock(return_value=('python',{},None))),('api',Mock(return_value={'ok':True})),
                ('preflight',Mock())]:
            p=patch.object(ops,target,value);p.start();self.addCleanup(p.stop)

    def test_no_arbitrary_units_actions_or_delay_values(self):
        for request in ({'action':'stop','service':'nginx'},{'action':'shell','service':'engine'},
            {'action':'delay','service':'scanner','minutes':True},{'action':'delay','service':'scanner','minutes':999},
            {'action':'restart','service':'engine','command':'anything'}):
            self.assertFalse(ops.act(request)['ok'])
        ops.run.assert_not_called();ops.api.assert_not_called()

    def test_scan_session_refuses_stop_restart_and_delay(self):
        ops.protected.return_value=True
        for service in ops.UNITS:
            for action in ('stop','restart','delay'):
                self.assertFalse(ops.act(dict(service=service,action=action))['ok'])
        ops.run.assert_not_called();ops.api.assert_not_called()

    def test_unresolved_exposure_never_stops_engine_and_retains_entry_hold(self):
        ops.preflight.side_effect=RuntimeError('secret broker details')
        result=ops.act(dict(service='engine',action='restart'))
        self.assertEqual(result['reason'],'positions_or_orders_unresolved_entries_remain_paused')
        self.assertIn('entry_hold',ops.saved())
        ops.run.assert_not_called()
        self.assertNotIn('secret',str(result))

    def test_hold_acknowledgement_failure_never_calls_preflight_or_stop(self):
        ops.api.return_value={'ok':False}
        self.assertFalse(ops.act(dict(service='engine',action='stop'))['ok'])
        ops.preflight.assert_not_called();ops.run.assert_not_called()

    def test_restart_is_serialized_after_hold_and_flat_check(self):
        events=[]
        ops.api.side_effect=lambda path,body=None:events.append(body['action']) or {'ok':True}
        ops.preflight.side_effect=lambda p:events.append('flat')
        ops.run.side_effect=lambda args,**kw:events.append(args[1]) or SimpleNamespace(stdout='')
        result=ops.act(dict(service='engine',action='restart'))
        self.assertTrue(result['ok']);self.assertEqual(events,['hold','flat','restart','release'])
        self.assertNotIn('entry_hold',ops.saved())

    def test_pause_entries_keeps_every_service_running(self):
        result=ops.act(dict(service='engine',action='pause_entries'))
        self.assertTrue(result['ok']);ops.run.assert_not_called();ops.preflight.assert_not_called()

    def test_start_releases_only_operations_hold(self):
        ops.save({'entry_hold':'a'*32})
        self.assertTrue(ops.act(dict(service='engine',action='start'))['ok'])
        self.assertEqual(ops.api.call_args.args,('entries',{'action':'release','token':'a'*32}))
        ops.preflight.assert_not_called()

    def test_delayed_worker_start_uses_fixed_unit_and_validated_minutes(self):
        ops.unit_state.side_effect=[dict(LoadState='loaded',ActiveState='active'),dict(LoadState='loaded',ActiveState='inactive')]
        result=ops.act(dict(service='scanner',action='delay',minutes=15))
        self.assertTrue(result['ok'])
        commands=[c.args[0] for c in ops.run.call_args_list]
        self.assertEqual(commands[0],['systemctl','stop','kadima-universe-scanner.service'])
        self.assertIn('--on-active=15m',commands[1])
        self.assertEqual(commands[1][-3:],[ops.SCRIPT,'--scheduled-start','scanner'])
        self.assertIsNotNone(ops.saved()['scanner']['delay_until'])

    def test_concurrent_action_returns_busy(self):
        ops.ACTION_LOCK.acquire()
        try:self.assertEqual(ops.act(dict(service='scanner',action='stop'))['reason'],'operation_in_progress')
        finally:ops.ACTION_LOCK.release()
        ops.run.assert_not_called()


class GatewayOperations(unittest.TestCase):
    def test_pending_job_returns_immediately_rejects_duplicate_and_never_replays(self):
        from fastapi.testclient import TestClient
        g=gateway();g.SESSIONS['fixture']=g.time.time()+60
        headers={'Cookie':g.COOKIE+'=fixture','Origin':'https://kadima.test'}
        started=threading.Event();release=threading.Event();finished=threading.Event()
        def action(*args):
            started.set();release.wait(5);finished.set()
            return {'ok':True}
        with TestClient(g.app) as client,patch.object(g,'operation_request',side_effect=action) as send:
            try:
                response=client.post('/operations',headers=headers,json={'service':'scanner','action':'start'})
                self.assertEqual(response.status_code,202);self.assertTrue(started.wait(1))
                route='/operations/jobs/'+response.json()['job_id']
                for _ in range(3):self.assertEqual(client.get(route,headers=headers).json()['state'],'running')
                self.assertEqual(client.post('/operations',headers=headers,json={'service':'scanner','action':'start'}).status_code,409)
                self.assertEqual(send.call_count,1)
            finally:release.set();self.assertTrue(finished.wait(1))

    def test_status_requires_session_writes_also_require_origin_and_fixed_payload(self):
        from fastapi.testclient import TestClient
        g=gateway();g.SESSIONS['fixture']=g.time.time()+60
        auth={'Cookie':g.COOKIE+'=fixture'}
        with TestClient(g.app) as client,patch.object(g,'operation_request',return_value={'ok':True}) as action:
            self.assertEqual(client.get('/operations').status_code,401)
            self.assertEqual(client.get('/operations',headers=auth).status_code,200)
            self.assertEqual(client.post('/operations',headers=auth,json={'service':'engine','action':'stop'}).status_code,403)
            headers={**auth,'Origin':'https://kadima.test'}
            for invalid in ({'service':'nginx','action':'stop'}, {'service':'engine','action':'shell'},
                    {'service':'engine','action':'delay','minutes':True},{'service':'engine','action':'start','args':[]}):
                self.assertEqual(client.post('/operations',headers=headers,json=invalid).status_code,422)
            response=client.post('/operations',headers=headers,json={'service':'scanner','action':'delay','minutes':15})
            self.assertEqual(response.status_code,202)
            identity=response.json()['job_id']
            self.assertEqual(client.get('/operations/jobs/'+identity).status_code,401)
            g.SESSIONS['other']=g.time.time()+60
            self.assertEqual(client.get('/operations/jobs/'+identity,headers={'Cookie':g.COOKIE+'=other'}).status_code,404)
            for _ in range(30):
                result=client.get('/operations/jobs/'+identity,headers=auth)
                if result.json()['state']=='done':break
            self.assertEqual(result.json()['result'],{'ok':True})
            self.assertEqual(action.call_count,2)
            self.assertEqual(client.get('/operations',headers=auth).headers['cache-control'],'no-store')


class ReadOnlyPreflight(unittest.TestCase):
    def test_sdk_uses_private_writable_working_directory_and_original_identity(self):
        owner=SimpleNamespace(pw_uid=1234,pw_gid=1234)
        env={'KADIMA_DATA_DIR':'/var/lib/kadima'}
        seen=[]
        def child(args,**kwargs):
            work=Path(kwargs['cwd']);self.assertTrue(work.is_dir());self.assertNotEqual(work,ops.REPO)
            seen.append(work)
            self.assertIn(repr(str(ops.REPO)),args[-1])
            self.assertEqual(kwargs['user'],owner.pw_uid);self.assertEqual(kwargs['extra_groups'],[])
            self.assertIs(kwargs['env'],env)
            return SimpleNamespace(stdout='KADIMA_PREFLIGHT_OK')
        with patch.object(ops.os,'chown'),patch.object(ops,'run',side_effect=child):
            ops.preflight(('/usr/bin/python3',env,owner))
        self.assertFalse(seen[0].exists())
        with patch.object(ops,'run') as run:
            with self.assertRaises(ValueError):ops.preflight(('/usr/bin/python3',{'KADIMA_DATA_DIR':'relative'},owner))
            run.assert_not_called()


if __name__=='__main__':unittest.main()
