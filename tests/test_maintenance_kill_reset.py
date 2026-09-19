import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
import io

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('maintenance_reset_test', BASE/'multi_position_update/reset_kill.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
AT = datetime(2026, 9, 20, 0, 30, tzinfo=r.IST)

class Fake:
    def __init__(self):
        self.cfg = dict(revision=4, kill_latched=True, auto_trade_enabled=False,
                        broker_orders_enabled=True, rollout_enabled=True, score_threshold=65)
        self.stock = dict(revision=2, live_enabled=False, holding_minutes=15)
        self.gd = dict(ts=AT.isoformat(), config=self.cfg, positions=[])
        self.sd = dict(ts=AT.isoformat(), controls=self.stock, global_auto_requested=False,
                       celestial_rollout=True, broker_order_permission=True, kill_latched=True, positions=[])
        self.calls=[]
        self.failure=None
    def call(self, action):
        self.calls.append(action)
        if action=='global': return copy.deepcopy(self.gd)
        if action=='stock': return copy.deepcopy(self.sd)
        if self.failure: raise r.Blocked(self.failure)
        self.cfg.update(revision=5, kill_latched=False, auto_trade_enabled=False)
        self.sd['kill_latched']=False
        return dict(revision=5, kill_latched=False, auto_trade_enabled=False)

class ResetTests(unittest.TestCase):
    def setUp(self):
        self.clock=patch.object(r,'now',return_value=AT)
        self.clock.start();self.addCleanup(self.clock.stop)
        self.c=Fake()
    def test_single_guarded_reset_keeps_both_switches_off(self):
        result=r.reset_once(self.c,True)
        self.assertIn('RESET VERIFIED',result)
        self.assertEqual(self.c.calls.count('reset'),1)
        self.assertFalse(self.c.cfg['auto_trade_enabled'])
        self.assertFalse(self.c.stock['live_enabled'])
        self.assertEqual(self.c.cfg['score_threshold'],65)
    def test_default_is_plan_only(self):
        self.assertIn('PLAN ONLY',r.reset_once(self.c))
        self.assertNotIn('reset',self.c.calls)
    def test_already_clear_does_not_send_reset(self):
        self.c.cfg['kill_latched']=self.c.sd['kill_latched']=False
        self.assertIn('ALREADY CLEAR',r.reset_once(self.c,True))
        self.assertNotIn('reset',self.c.calls)
    def test_active_global_prevents_reset(self):
        self.c.cfg['auto_trade_enabled']=True
        with self.assertRaises(r.Blocked):r.reset_once(self.c,True)
        self.assertNotIn('reset',self.c.calls)
    def test_active_stock_prevents_reset(self):
        self.c.stock['live_enabled']=True
        with self.assertRaises(r.Blocked):r.reset_once(self.c,True)
        self.assertNotIn('reset',self.c.calls)
    def test_unavailable_broker_permission_prevents_reset(self):
        self.c.cfg['broker_orders_enabled']=False
        with self.assertRaises(r.Blocked):r.reset_once(self.c,True)
        self.assertNotIn('reset',self.c.calls)
    def test_visible_positions_prevent_reset(self):
        self.c.gd['positions']=[dict(status='open')]
        with self.assertRaises(r.Blocked):r.reset_once(self.c,True)
        self.assertNotIn('reset',self.c.calls)
    def test_missing_position_state_is_not_flat(self):
        self.c.gd.pop('positions')
        with self.assertRaises(r.Blocked):r.reset_once(self.c,True)
    def test_broker_rejection_is_not_bypassed(self):
        self.c.failure='verified_flat_account_required'
        with self.assertRaisesRegex(r.Blocked,'verified_flat_account_required'):
            r.reset_once(self.c,True)
        self.assertTrue(self.c.cfg['kill_latched'])
        self.assertEqual(self.c.calls.count('reset'),1)
    def test_response_loss_is_not_retried(self):
        self.c.failure='reset_result_unconfirmed_read_status_before_retry'
        with self.assertRaisesRegex(r.Blocked,'reset_result_unconfirmed'):
            r.reset_once(self.c,True)
        self.assertEqual(self.c.calls.count('reset'),1)
    def test_control_change_after_reset_blocks_upgrade(self):
        old=self.c.call
        def changed(action):
            result=old(action)
            if action=='reset':self.c.stock['holding_minutes']=5
            return result
        self.c.call=changed
        with self.assertRaisesRegex(r.Blocked,'post_reset_controls_changed'):
            r.reset_once(self.c,True)
    def test_invalid_revision_never_coerced(self):
        self.c.stock['revision']=True
        with self.assertRaises(r.Blocked):r.reset_once(self.c,True)
        self.assertNotIn('reset',self.c.calls)
    def test_stale_or_future_data_blocks(self):
        for stamp in ('2026-09-19T10:00:00+05:30','2026-09-21T10:00:00+05:30'):
            self.c.gd['ts']=stamp
            with self.assertRaises(r.Blocked):r.reset_once(self.c,True)
        self.assertNotIn('reset',self.c.calls)
    def test_mismatched_kill_readings_block(self):
        self.c.sd['kill_latched']=False
        with self.assertRaises(r.Blocked):r.reset_once(self.c,True)
    def test_weekday_window_blocks_without_api_calls(self):
        with patch.object(r,'now',return_value=datetime(2026,9,21,9,0,tzinfo=r.IST)):
            with self.assertRaises(r.Blocked):r.reset_once(self.c,True)
        self.assertEqual(self.c.calls,[])
    def test_boundary_buffer_blocks(self):
        for hour,minute in ((8,55),(15,39)):
            with self.assertRaises(r.Blocked):
                r.outside_arming_window(datetime(2026,9,21,hour,minute,tzinfo=r.IST))
        r.outside_arming_window(datetime(2026,9,21,15,40,tzinfo=r.IST))
    def test_wrong_reset_response_never_reported_success(self):
        old=self.c.call
        def changed(action):
            result=old(action)
            if action=='reset': result['revision']=17
            return result
        self.c.call=changed
        with self.assertRaisesRegex(r.Blocked,'reset_response_not_confirmed'):
            r.reset_once(self.c,True)

class TransportTests(unittest.TestCase):
    def client(self):
        c=r.Client.__new__(r.Client);c.token='PRIVATE_TOKEN';c.opener=Mock()
        return c
    def test_only_reset_uses_post(self):
        c=self.client()
        response=Mock(); response.read.return_value=b'{}'
        manager=Mock();manager.__enter__=Mock(return_value=response);manager.__exit__=Mock(return_value=False)
        c.opener.open.return_value=manager
        for name in ('global','stock','reset'):
            c.call(name)
            req=c.opener.open.call_args.args[0]
            self.assertEqual(req.method,'POST' if name=='reset' else 'GET')
            self.assertTrue(req.full_url.startswith('http://127.0.0.1:8081/api/kadima/'))
        with self.assertRaises(r.Blocked):c.call('resume')
    def test_provider_errors_never_print_raw_body(self):
        c=self.client()
        c.opener.open.side_effect=urllib.error.HTTPError('http://unused',409,'failure',{},
            io.BytesIO(b'{"detail":"PRIVATE_TOKEN"}'))
        with self.assertRaises(r.Blocked) as error:c.call('reset')
        self.assertEqual(str(error.exception),'reset_http_409')
    def test_guarded_flat_rejection_is_named(self):
        c=self.client()
        c.opener.open.side_effect=urllib.error.HTTPError('http://unused',409,'failure',{},
            io.BytesIO(b'{"detail":"verified_flat_account_required"}'))
        with self.assertRaisesRegex(r.Blocked,'verified_flat_account_required'):c.call('reset')
    def test_unknown_timeout_never_repeats_write(self):
        c=self.client();c.opener.open.side_effect=TimeoutError('PRIVATE_TOKEN')
        with self.assertRaisesRegex(r.Blocked,'reset_result_unconfirmed'):c.call('reset')
        self.assertEqual(c.opener.open.call_count,1)
    def test_redirects_refused(self):
        self.assertIsNone(r.NoRedirect().redirect_request(None,None,None,None,None,None))

class SourceTests(unittest.TestCase):
    def test_reviewed_file_fingerprints(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); raw=b'approved\n';(root/'file.py').write_bytes(raw)
            sha=hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
            with patch.object(r,'EXPECTED',{'file.py':sha}):
                r.verify_sources(root)
                (root/'file.py').write_text('different')
                with self.assertRaises(r.Blocked):r.verify_sources(root)
    def test_symlink_not_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'a').write_text('private');(root/'b').symlink_to(root/'a')
            with self.assertRaises(r.Blocked):r.read_regular(root/'b')

if __name__=='__main__':unittest.main()
