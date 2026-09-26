"""Offline transactional repair tests; no broker credentials or account data."""
import importlib.util
import hashlib
import json
from pathlib import Path
import sqlite3
import unittest
from datetime import datetime
import contextlib
import io
import sys
import types
import tempfile
from unittest.mock import patch, Mock

spec = importlib.util.spec_from_file_location('note_repair', Path(__file__).resolve().parents[1]/'momentum_recovery/reconcile_contract_note.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
NOW = datetime.fromisoformat('2026-09-25T14:00:00+05:30')


class Recovery(unittest.TestCase):
    def setUp(self):
        self.e = dict(document_sha256='a'*64, signal_id=1, quantity=60,
                      symbol='TESTPE', token='123', exchange='BFO', trade_date='2026-09-24',
                      expiry='2026-09-24', entry_at='2026-09-24T09:32:00+05:30',
                      intent_at='2026-09-24T09:33:00+05:30', exit_at='2026-09-24T09:39:00+05:30',
                      entry_price=100, exit_price=120, expected_entry_price=105,
                      exchange_order_id='987', trade_id='654', intent='Ktest', day_buy_qty=90, day_sell_qty=90)
        self.c = sqlite3.connect(':memory:')
        self.c.row_factory = sqlite3.Row
        self.c.executescript('''
            CREATE TABLE positions(signal_id INTEGER, symbol TEXT, token TEXT, exchange TEXT,
                kind TEXT, status TEXT, remaining_qty INTEGER, lots INTEGER, lot_size INTEGER,
                entry_price REAL, entry_ts TEXT, exit_price REAL, exit_ts TEXT, exit_order_id TEXT, exit_reason TEXT);
            CREATE TABLE execution_orders(intent TEXT, signal_id INTEGER, side TEXT, quantity INTEGER,
                order_id TEXT, state TEXT, filled INTEGER, notional REAL, created_at TEXT, dry_run INTEGER);
            CREATE TABLE execution_fills(intent TEXT);
            CREATE TABLE celestial_external_fills(signal_id INTEGER, order_id TEXT, cumulative_qty INTEGER,
                quantity INTEGER, price REAL, pnl REAL, ts TEXT, PRIMARY KEY(signal_id,order_id,cumulative_qty));
            CREATE TABLE celestial_config(id INTEGER,payload TEXT);
        ''')
        self.c.execute('INSERT INTO positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (1,'TESTPE','123','BFO','MANUAL_ADOPTED','exit_pending',60,2,30,105,
                        self.e['entry_at'],None,None,None,'trail'))
        self.c.execute('INSERT INTO execution_orders VALUES (?,?,?,?,?,?,?,?,?,?)',
                       ('Ktest',1,'SELL',60,None,'unknown',0,0,self.e['intent_at'],0))
        self.c.execute('INSERT INTO celestial_config VALUES (1,?)',
                       (json.dumps(dict(auto_trade_enabled=False,kill_latched=False)),))
        self.c.commit()

    def tearDown(self):
        self.c.close()

    def apply(self, original=None):
        original = original or module.inspect(self.c,self.e)
        module.repair(self.c,self.e,original,{'flat':True},NOW)

    def test_evidence_dates_and_prices(self):
        self.assertEqual(module.evidence(self.e,NOW),self.e)
        for key,value in [('expiry','2026-09-25'),('entry_price',float('nan')),
                          ('quantity',True),('day_sell_qty',89),('document_sha256','bad'),
                          ('exit_at','2026-09-24T09:30:00+05:30')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                module.evidence(dict(self.e,**{key:value}),NOW)

    def test_expired_same_day_refused(self):
        with self.assertRaises(ValueError):
            module.evidence(self.e,datetime.fromisoformat('2026-09-24T18:00:00+05:30'))

    def test_repair_preserves_unknown_request_and_actual_historical_pnl(self):
        self.apply()
        p=dict(self.c.execute('SELECT * FROM positions').fetchone())
        self.assertEqual((p['status'],p['remaining_qty'],p['entry_price'],p['exit_price']),('closed',0,100,120))
        self.assertEqual(p['exit_ts'],self.e['exit_at'])
        self.assertIsNone(p['exit_order_id'])
        order=dict(self.c.execute('SELECT * FROM execution_orders').fetchone())
        self.assertEqual((order['state'],order['filled'],order['notional']),('terminal',0,0))
        self.assertIsNone(order['order_id'])
        fill=dict(self.c.execute('SELECT * FROM celestial_external_fills').fetchone())
        self.assertEqual((fill['pnl'],fill['ts'],fill['order_id']),(1200,self.e['exit_at'],'EXCHANGE:BFO:987:654'))
        receipt=self.c.execute('SELECT original_position,original_order FROM contract_note_reconciliations').fetchone()
        self.assertEqual(json.loads(receipt[0])['entry_price'],105)
        self.assertEqual(json.loads(receipt[1])['state'],'unknown')
        self.assertEqual(self.c.execute('SELECT count(*) FROM execution_fills').fetchone()[0],0)

    def test_second_apply_cannot_duplicate_profit(self):
        self.apply()
        with self.assertRaises(ValueError): self.apply()
        self.assertEqual(self.c.execute('SELECT count(*) FROM celestial_external_fills').fetchone()[0],1)

    def test_changed_position_rejected(self):
        before=module.inspect(self.c,self.e)
        self.c.execute("UPDATE positions SET exit_reason='changed'"); self.c.commit()
        with self.assertRaises(ValueError): self.apply(before)
        self.assertEqual(self.c.execute('SELECT state FROM execution_orders').fetchone()[0],'unknown')

    def test_known_id_or_fills_must_not_be_overwritten(self):
        for field,value in [('order_id','LIVE'),('filled',30),('state','pending'),('dry_run',1)]:
            with self.subTest(field=field):
                self.c.execute('SAVEPOINT scenario')
                self.c.execute('UPDATE execution_orders SET '+field+'=?',(value,))
                with self.assertRaises(ValueError): module.inspect(self.c,self.e)
                self.c.execute('ROLLBACK TO scenario'); self.c.execute('RELEASE scenario')

    def test_other_pending_intent_blocks(self):
        self.c.execute("INSERT INTO execution_orders VALUES ('OTHER',2,'SELL',10,NULL,'unknown',0,0,'2026-09-24',0)")
        with self.assertRaises(ValueError): module.inspect(self.c,self.e)

    def test_existing_external_fill_blocks(self):
        self.c.execute("INSERT INTO celestial_external_fills VALUES (1,'OTHER',30,30,120,600,'2026-09-24')")
        with self.assertRaises(ValueError): module.inspect(self.c,self.e)

    def test_unpaused_controls_block(self):
        self.c.execute('UPDATE celestial_config SET payload=?',(json.dumps(dict(auto_trade_enabled=True,kill_latched=False)),))
        with self.assertRaises(ValueError): module.inspect(self.c,self.e)

    def test_transaction_rolls_back_if_fill_insert_fails(self):
        before=module.inspect(self.c,self.e)
        self.c.execute("CREATE TRIGGER refuse_fill BEFORE INSERT ON celestial_external_fills BEGIN SELECT RAISE(ABORT,'test'); END")
        self.c.commit()
        with self.assertRaises(sqlite3.IntegrityError): self.apply(before)
        self.assertEqual(module.inspect(self.c,self.e),before)

    def broker(self, positions, orders):
        session=types.SimpleNamespace(ensure=lambda:None,api=types.SimpleNamespace(
            position=lambda:positions,orderBook=lambda:orders))
        output=io.StringIO()
        fake=types.ModuleType('kadima.session'); fake.SESSION=session
        with patch.dict(sys.modules,{'kadima.session':fake}),contextlib.redirect_stdout(output):
            exec(module.BROKER_READ,{})
        return json.loads(output.getvalue().split('NOTE_BROKER ')[-1])

    def test_broker_successful_empty_books(self):
        self.assertTrue(self.broker({'status':True,'data':[]},{'status':True,'data':[]})['flat'])

    def test_broker_failure_never_becomes_empty_book(self):
        for response in ({'status':False,'data':[]},{'status':True},None,{'status':True,'data':{}}):
            with self.subTest(response=response):
                self.assertFalse(self.broker(response,{'status':True,'data':[]})['flat'])

    def test_broker_invalid_or_live_quantities(self):
        for qty in (60,-60,None,True,'NaN','Infinity',''):
            with self.subTest(qty=qty):
                self.assertFalse(self.broker({'status':True,'data':[{'netqty':qty}]},
                                            {'status':True,'data':[]})['flat'])

    def test_broker_pending_orders_refused(self):
        for state in ('open','pending','unknown',None):
            with self.subTest(state=state):
                self.assertFalse(self.broker({'status':True,'data':[]},
                                            {'status':True,'data':[{'status':state}]})['flat'])

    def test_broker_closed_positions_and_terminal_orders(self):
        self.assertTrue(self.broker({'status':True,'data':[{'netqty':'0'}]},
                                   {'status':True,'data':[{'status':'complete'}]})['flat'])

    def test_stock_pause_noop_if_already_paused(self):
        base=types.SimpleNamespace(local_api=lambda *a:{'controls':{'live_enabled':False,'revision':2}})
        with patch.object(module.urllib.request,'build_opener') as build:
            module.pause_stock(base,'not-a-real-token')
            build.assert_not_called()

    def test_stock_pause_uses_current_revision_and_only_disables(self):
        base=types.SimpleNamespace(local_api=lambda *a:{'controls':{'live_enabled':True,'revision':7}},
                                   NoRedirect=module.urllib.request.HTTPRedirectHandler)
        opener=Mock()
        opener.open.return_value=io.BytesIO(b'{"live_enabled":false}')
        with patch.object(module.urllib.request,'build_opener',return_value=opener),contextlib.redirect_stdout(io.StringIO()):
            module.pause_stock(base,'not-a-real-token')
        req=opener.open.call_args.args[0]
        self.assertEqual(req.get_method(),'POST')
        self.assertEqual(json.loads(req.data),{'patch':{'live_enabled':False},'expected_revision':7})

    def test_stock_pause_unknown_state_refused(self):
        base=types.SimpleNamespace(local_api=lambda *a:{'controls':{'live_enabled':'true','revision':7}})
        with self.assertRaises(ValueError): module.pause_stock(base,'not-a-real-token')


class WeekendRecovery(unittest.TestCase):
    """Synthetic identities and evidence only; no user/account information."""
    def setUp(self):
        Recovery.setUp(self)
        self.now = datetime.fromisoformat('2026-09-26T22:20:00+05:30')
        self.identity = dict(pid='321', start_ticks=100, boot_time=1,
                             started_at=datetime.fromisoformat('2026-09-26T22:00:00+05:30'))
        self.e['account_sha256'] = hashlib.sha256(b'TESTACCOUNT').hexdigest()
        self.env = dict(ANGEL_CLIENT_ID='  testAccount ', KADIMA_CELESTIAL_ENABLED='1')
        self.status = dict(positions=[], config=dict(rollout_enabled=True,
                           auto_trade_enabled=False, kill_latched=False), system={})
        self.c.executescript('''
            CREATE TABLE stock_fast_controls(id INTEGER,payload TEXT);
            INSERT INTO stock_fast_controls VALUES (1,'{"live_enabled":false}');
            CREATE TABLE celestial_health(name TEXT,ts TEXT,payload TEXT);
        ''')

    def tearDown(self):
        self.c.close()

    def wait(self, **changes):
        args = dict(e=self.e, env=self.env, status=self.status, identity=self.identity,
                    now=self.now, expected_identity=self.identity)
        args.update(changes)
        module.off_session_wait(**args)

    def apply(self, guard=None):
        original = module.inspect(self.c, self.e)
        receipts = {name: dict(flat=True) for name in ('before_stop','after_stop','before_commit')}
        module.repair(self.c, self.e, original, receipts, self.now, guard)
        return original

    def test_account_bound_evidence_required_only_for_explicit_mode(self):
        original = dict(self.e)
        original.pop('account_sha256')
        module.evidence(original, self.now)
        with self.assertRaisesRegex(ValueError, 'reviewed_account_digest_required'):
            module.evidence(original, self.now, account_required=True)
        for value in ('bad', None, 123):
            with self.subTest(value=value), self.assertRaises(ValueError):
                module.evidence(dict(self.e, account_sha256=value), self.now, account_required=True)
        module.evidence(self.e, self.now, account_required=True)

    def test_weekend_unrestored_account_is_normalized(self):
        self.wait()
        module.off_session_persisted(self.c, self.e, self.env, self.identity, self.now)

    def test_wrong_account_missing_account_and_empty_account_rejected(self):
        for value in ('ANOTHERACCOUNT', '', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.wait(env=dict(self.env, ANGEL_CLIENT_ID=value))

    def test_force_start_or_disabled_celestial_rejected(self):
        for patch in ({'KADIMA_FORCE_START':'1'}, {'KADIMA_FORCE_START':'true'},
                      {'KADIMA_CELESTIAL_ENABLED':'0'}, {'KADIMA_CELESTIAL_ENABLED':None}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                self.wait(env=dict(self.env, **patch))

    def test_weekday_old_process_future_start_and_boundary_refused(self):
        for stamp in ('2026-09-28T22:20:00+05:30', '2026-09-27T22:20:00+05:30',
                      '2026-09-26T21:59:59+05:30', '2026-09-26T23:55:00+05:30'):
            with self.subTest(stamp=stamp), self.assertRaises(ValueError):
                self.wait(now=datetime.fromisoformat(stamp))

    def test_changed_or_reused_pid_refused(self):
        for patch in ({'pid':'322'}, {'start_ticks':101}, {'boot_time':2}):
            with self.subTest(patch=patch), self.assertRaisesRegex(ValueError, 'engine_changed'):
                self.wait(identity=dict(self.identity, **patch))

    def test_api_exposure_or_armed_controls_refused(self):
        with self.assertRaises(ValueError):
            self.wait(status=dict(self.status, positions=[dict(signal_id=1)]))
        for patch in ({'rollout_enabled':False}, {'auto_trade_enabled':True}, {'kill_latched':True}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                self.wait(status=dict(self.status, config=dict(self.status['config'], **patch)))

    def test_only_absent_or_strictly_preprocess_health_allowed(self):
        self.wait(status=dict(self.status, system=dict(reconciliation=dict(
            ts='2026-09-25T15:00:00+05:30', ok=False))))
        for health in ({'ts':'2026-09-26T22:00:00+05:30', 'ok':True},
                       {'ts':'2026-09-26T22:10:00+05:30', 'ok':False},
                       {'ts':'2026-09-26T22:00:00'}, {}, 'unknown'):
            with self.subTest(health=health), self.assertRaises(ValueError):
                self.wait(status=dict(self.status, system=dict(reconciliation=health)))

    def test_source_pins_and_direct_environment_account_binding_required(self):
        raw = b'# synthetic reviewed source\n'
        digest = hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
        pins = {name:{digest} for name in module.OFF_SESSION_SOURCES}
        originals = {module.ROOT/name:raw for name in pins}
        originals[module.ROOT/'kadima/config.py'] = (
            b'import os\nfrom zoneinfo import ZoneInfo\nIST = ZoneInfo("Asia/Kolkata")\n'
            b'ANGEL_CLIENT_ID = os.environ.get("ANGEL_CLIENT_ID", "")\n'
            b'DATA_DIR = os.environ.get("KADIMA_DATA_DIR", "/var/lib/kadima")\n'
            b'DB_PATH = os.path.join(DATA_DIR, "kadima.db")\n')
        with patch.object(module, 'OFF_SESSION_SOURCES', pins):
            module.off_session_sources(originals)
            for name in pins:
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'source_unreviewed'):
                    module.off_session_sources(originals | {module.ROOT/name:b'changed'})
            for raw in (b'ANGEL_CLIENT_ID = "other"', b'import os\nANGEL_CLIENT_ID = os.environ.get("OTHER", "")',
                        originals[module.ROOT/'kadima/config.py']+b'ANGEL_CLIENT_ID = "second"\n'):
                with self.subTest(raw=raw), self.assertRaises(ValueError):
                    module.off_session_sources(originals | {module.ROOT/'kadima/config.py':raw})

            config = originals[module.ROOT/'kadima/config.py']
            for raw in (config.replace(b'Asia/Kolkata', b'UTC'),
                        config+b'IST = ZoneInfo("UTC")\n',
                        config.replace(b'IST = ZoneInfo("Asia/Kolkata")\n', b'')):
                with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, 'timezone_unreviewed'):
                    module.off_session_sources(originals | {module.ROOT/'kadima/config.py':raw})

            for raw in (config.replace(b'/var/lib/kadima', b'/another/database'),
                        config.replace(b'"kadima.db"', b'"different.db"'),
                        config+b'DATA_DIR = "/other"\n', config+b'DB_PATH = "/other"\n'):
                with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, 'database_config_unreviewed'):
                    module.off_session_sources(originals | {module.ROOT/'kadima/config.py':raw})

            for name in ('IST', 'ANGEL_CLIENT_ID', 'DATA_DIR', 'DB_PATH', 'ZoneInfo', 'os'):
                for extra in (f'from other import {name}\n', f'import other as {name}\n',
                              f'def {name}(): pass\n', f'class {name}: pass\n',
                              f'def callback({name}): pass\n'):
                    with self.subTest(name=name, extra=extra), self.assertRaises(ValueError):
                        module.off_session_sources(originals | {module.ROOT/'kadima/config.py':config+extra.encode()})
            for raw in (config.replace(b'from zoneinfo import ZoneInfo', b'from other import ZoneInfo'),
                        config.replace(b'from zoneinfo import ZoneInfo', b''),
                        config+b'from other import *\n'):
                with self.subTest(raw=raw), self.assertRaises(ValueError):
                    module.off_session_sources(originals | {module.ROOT/'kadima/config.py':raw})

    def test_process_identity_tracks_start_ticks_and_handles_comm_parentheses(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            (proc/'321').mkdir()
            (proc/'stat').write_text('cpu 1 2 3\nbtime 1000\n')
            fields = ['S'] + ['0']*18 + ['250']
            (proc/'321'/'stat').write_text('321 (python ) worker) '+' '.join(fields))
            identity = module.process_identity('321', proc, ticks=100)
            self.assertEqual(identity['start_ticks'], 250)
            self.assertEqual(identity['started_at'].timestamp(), 1002.5)
            fields[-1] = '251'
            (proc/'321'/'stat').write_text('321 (python) '+' '.join(fields))
            self.assertNotEqual(module.process_identity('321', proc, ticks=100), identity)
            with self.assertRaises(ValueError): module.process_identity('0', proc, ticks=100)

    def test_paused_and_health_guards_repeat_inside_transaction(self):
        original = module.inspect(self.c, self.e)
        self.c.execute('INSERT INTO celestial_health VALUES (?,?,?)',
                       ('reconciliation', self.now.isoformat(), '{"ok":true}'))
        self.c.commit()
        guard = lambda c: module.off_session_persisted(c, self.e, self.env, self.identity, self.now)
        with self.assertRaises(ValueError): self.apply(guard)
        self.assertEqual(module.inspect(self.c, self.e), original)
        self.assertIsNone(self.c.execute("SELECT name FROM sqlite_master WHERE name='contract_note_reconciliations'").fetchone())

    def test_stock_intent_must_be_paused_at_transaction(self):
        self.c.execute('UPDATE stock_fast_controls SET payload=?', ('{"live_enabled":true}',))
        self.c.commit()
        with self.assertRaisesRegex(ValueError, 'stock_intent_must_be_paused'):
            self.apply(lambda c: module.off_session_persisted(c, self.e, self.env, self.identity, self.now))

    def test_read_only_plan_requires_persistent_controls_even_if_api_defaults_false(self):
        self.c.execute('DELETE FROM stock_fast_controls')
        self.c.commit()
        original = module.inspect(self.c, self.e)
        with self.assertRaisesRegex(ValueError, 'persisted_controls_required'):
            module.off_session_persisted(self.c, self.e, self.env, self.identity, self.now,
                                         require_stock_paused=False)
        self.assertEqual(module.inspect(self.c, self.e), original)

    def test_plan_accepts_existing_stock_intent_until_apply_pause_but_checks_health(self):
        self.c.execute('UPDATE stock_fast_controls SET payload=?', ('{"live_enabled":true}',))
        self.c.commit()
        module.off_session_persisted(self.c, self.e, self.env, self.identity, self.now,
                                     require_stock_paused=False)
        self.c.execute('INSERT INTO celestial_health VALUES (?,?,?)',
                       ('reconciliation', self.now.isoformat(), '{"ok":true}'))
        self.c.commit()
        with self.assertRaisesRegex(ValueError, 'reconciliation_started'):
            module.off_session_persisted(self.c, self.e, self.env, self.identity, self.now,
                                         require_stock_paused=False)

    def test_exact_postcheck_and_archive_preserve_completed_entry_time(self):
        # Two synthetic partial buys complete the reviewed quantity at 09:32:15.
        # The local entry_ts is adoption time; evidence records fill completion.
        self.e['entry_at'] = '2026-09-24T09:32:15+05:30'
        original = self.apply(lambda c: module.off_session_persisted(c, self.e, self.env, self.identity, self.now))
        module.verify_committed(self.c, self.e, original)
        stored = json.loads(self.c.execute('SELECT evidence FROM contract_note_reconciliations').fetchone()[0])
        self.assertEqual(stored['entry_at'], self.e['entry_at'])
        self.assertEqual(self.c.execute('SELECT entry_ts FROM positions').fetchone()[0], original[0]['entry_ts'])

    def test_postcheck_rejects_changed_ledger_receipt_or_fill(self):
        original = self.apply()
        mutations = ["UPDATE positions SET remaining_qty=1", "UPDATE positions SET entry_price=101",
                     "UPDATE execution_orders SET filled=60", "UPDATE execution_orders SET order_id='invented'",
                     "UPDATE contract_note_reconciliations SET evidence='{}'",
                     "UPDATE contract_note_reconciliations SET original_order='{}'",
                     "UPDATE contract_note_reconciliations SET broker_receipt='{}'",
                     "UPDATE celestial_external_fills SET quantity=30",
                     "UPDATE celestial_external_fills SET ts='2026-09-26'",
                     "INSERT INTO execution_fills VALUES ('Ktest')",
                     "INSERT INTO execution_orders VALUES ('OTHER',2,'SELL',1,NULL,'unknown',0,0,'2026-09-24',0)"]
        for sql in mutations:
            with self.subTest(sql=sql):
                self.c.execute('SAVEPOINT altered')
                self.c.execute(sql)
                with self.assertRaises(ValueError): module.verify_committed(self.c, self.e, original)
                self.c.execute('ROLLBACK TO altered')
                self.c.execute('RELEASE altered')
        module.verify_committed(self.c, self.e, original)

    def test_broker_child_checks_imported_account_before_login(self):
        session = types.SimpleNamespace(ensure=Mock(), api=types.SimpleNamespace(
            position=lambda:{'status':True,'data':[]}, orderBook=lambda:{'status':True,'data':[]}))
        fake_session = types.ModuleType('kadima.session'); fake_session.SESSION = session
        fake_config = types.ModuleType('kadima.config'); fake_config.ANGEL_CLIENT_ID = 'DifferentAccount'
        output = io.StringIO()
        with patch.dict(module.os.environ, {'KADIMA_NOTE_ACCOUNT_SHA256':self.e['account_sha256']}), \
             patch.dict(sys.modules, {'kadima.session':fake_session, 'kadima.config':fake_config}), \
             contextlib.redirect_stdout(output):
            exec(module.BROKER_READ, {})
        session.ensure.assert_not_called()
        result = json.loads(output.getvalue().split('NOTE_BROKER ')[-1])
        self.assertEqual(result, {'flat':False,'stage':'account'})

    def test_broker_child_fresh_login_with_matching_account(self):
        session = types.SimpleNamespace(ensure=Mock(), api=types.SimpleNamespace(
            position=lambda:{'status':True,'data':[]}, orderBook=lambda:{'status':True,'data':[]}))
        fake_session = types.ModuleType('kadima.session'); fake_session.SESSION = session
        fake_config = types.ModuleType('kadima.config'); fake_config.ANGEL_CLIENT_ID = ' testaccount '
        output = io.StringIO()
        with patch.dict(module.os.environ, {'KADIMA_NOTE_ACCOUNT_SHA256':self.e['account_sha256']}), \
             patch.dict(sys.modules, {'kadima.session':fake_session, 'kadima.config':fake_config}), \
             contextlib.redirect_stdout(output):
            exec(module.BROKER_READ, {})
        session.ensure.assert_called_once_with()
        result = json.loads(output.getvalue().split('NOTE_BROKER ')[-1])
        self.assertTrue(result['flat'])


if __name__ == '__main__': unittest.main()
