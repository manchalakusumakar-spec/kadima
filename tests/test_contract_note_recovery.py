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
        self.assertEqual(result, {'flat':False,'stage':'account','reason':'account_mismatch','error':'ValueError'})

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


class BrokerDiagnostics(unittest.TestCase):
    setUp = Recovery.setUp
    tearDown = Recovery.tearDown

    def child(self, position=None, orders=None, login_error=None, import_failure=False):
        session = types.SimpleNamespace(ensure=Mock(side_effect=login_error), api=types.SimpleNamespace(
            position=Mock(return_value=position if position is not None else {'status':True,'data':[]}),
            orderBook=Mock(return_value=orders if orders is not None else {'status':True,'data':[]})))
        self.last_session = session
        fake = types.ModuleType('kadima.session')
        if not import_failure: fake.SESSION = session
        output = io.StringIO()
        with patch.dict(sys.modules, {'kadima.session':fake}), \
             patch.dict(module.os.environ, {'KADIMA_NOTE_ACCOUNT_SHA256':''}), \
             contextlib.redirect_stdout(output):
            exec(module.BROKER_READ, {})
        self.assertNotIn('SECRET', output.getvalue())
        return json.loads(output.getvalue().split('NOTE_BROKER ')[-1])

    def test_child_distinguishes_import_login_response_schema_quantity_and_orders(self):
        cases = [({'import_failure':True}, 'imports', 'imports_failed'),
                 ({'login_error':RuntimeError('SECRET_LOGIN_TOKEN')}, 'login', 'login_failed'),
                 ({'position':{'status':False,'message':'SECRET_PROVIDER'}}, 'position', 'response_unsuccessful'),
                 ({'position':{'status':True}}, 'position', 'response_schema_invalid'),
                 ({'position':{'status':True,'data':{}}}, 'position', 'book_schema_invalid'),
                 ({'position':{'status':True,'data':['SECRET_ROW']}}, 'position', 'row_schema_invalid'),
                 ({'position':{'status':True,'data':[{'netqty':'SECRET_QUANTITY'}]}}, 'position', 'position_quantity_invalid'),
                 ({'position':{'status':True,'data':[{'netqty':60}]}}, 'position', 'position_nonzero'),
                 ({'orders':{'status':False}}, 'orderBook', 'response_unsuccessful'),
                 ({'orders':{'status':True,'data':[{'status':'SECRET_ORDER'}]}}, 'orderBook', 'order_unresolved')]
        for args, stage, reason in cases:
            with self.subTest(stage=stage, reason=reason):
                result = self.child(**args)
                self.assertFalse(result['flat'])
                self.assertEqual((result['stage'], result['reason']), (stage, reason))
                self.assertIn(result['error'], module.BROKER_ERROR_TYPES)

    def test_provider_defined_exception_name_and_message_are_not_printed(self):
        secret_type = type('SECRET_PROVIDER_CLASS', (Exception,), {})
        result = self.child(login_error=secret_type('SECRET_PASSWORD'))
        self.assertEqual(result['error'], 'OtherError')

    def test_nonzero_position_shows_only_first_blocker_identity_and_quantity(self):
        row = dict(exchange='NFO', producttype='CARRYFORWARD', tradingsymbol='NIFTY26OCT25000CE',
                   symboltoken='123456', netqty='75', clientcode='SECRET_CLIENT', orderid='SECRET_ORDER',
                   buyavgprice=120, pnl=555, daybuyqty=150, cfbuyqty=75, message='SECRET_MESSAGE')
        result = self.child(position={'status':True,'data':[{'netqty':'0'}, row,
                            dict(row, tradingsymbol='SECOND26OCT100CE')]})
        self.assertEqual(result['reason'], 'position_nonzero')
        self.assertEqual(result['position_scope'], 'first_nonzero_blocker')
        self.assertEqual(result['position'], dict(exchange='NFO', producttype='CARRYFORWARD',
                         tradingsymbol='NIFTY26OCT25000CE', symboltoken='123456', netqty=75))
        self.last_session.api.orderBook.assert_not_called()
        self.assertNotIn('SECOND', json.dumps(result))
        self.assertEqual(result['position'], module.safe_position(row))

    def test_position_filters_reject_malformed_sensitive_and_unbounded_fields(self):
        valid = dict(exchange='BFO', producttype='INTRADAY', tradingsymbol='INDEX26OCT100PE',
                     symboltoken='12345', netqty=-60)
        for field, values in {
            'exchange':['SECRET_EXCHANGE',{},None], 'producttype':['SECRET_PRODUCT',[],None],
            'tradingsymbol':['<SECRET>', 'SECRET\nVALUE', 'X'*81, '日本株'],
            'symboltoken':['SECRET_TOKEN', '1'*17, True, -1],
            'netqty':[True,float('nan'),float('inf'),1_000_000_001,'9'*33,{},None],
        }.items():
            for value in values:
                row = dict(valid, **{field:value})
                with self.subTest(field=field,value=value):
                    filtered = module.safe_position(row)
                    self.assertNotIn(field, filtered)
                    if field != 'netqty':
                        child = self.child(position={'status':True,'data':[row]})
                        self.assertEqual(child['position'], filtered)
        self.assertEqual(module.safe_position(['SECRET_ROW']), {})

    def test_parent_position_resanitization_is_limited_to_nonzero_position_failure(self):
        receipt = dict(flat=False, stage='position', reason='position_nonzero', error='ValueError',
            position=dict(exchange='MCX',producttype='CARRYFORWARD',tradingsymbol='GOLD26OCTFUT',
                          symboltoken='123',netqty=1,clientcode='SECRET_CLIENT',price=100000),
            position_scope='SECRET_SCOPE')
        for stage, reason, allowed in (('position','position_nonzero',True),
                                      ('position','position_quantity_invalid',False),
                                      ('orderBook','order_unresolved',False),('login','login_failed',False)):
            receipt.update(stage=stage, reason=reason)
            result = types.SimpleNamespace(returncode=0,stdout='NOTE_BROKER '+json.dumps(receipt))
            output = io.StringIO()
            with self.subTest(stage=stage,reason=reason), contextlib.redirect_stdout(output), self.assertRaises(ValueError):
                module.broker_receipt(result, NOW, 'check_only')
            self.assertNotIn('SECRET', output.getvalue())
            diagnostic = json.loads(output.getvalue().split('NOTE_BROKER_DIAGNOSTIC ')[-1])
            self.assertEqual('position' in diagnostic, allowed)
            if allowed:
                self.assertEqual(diagnostic['position'], module.safe_position(receipt['position']))
                self.assertEqual(diagnostic['position_scope'], 'first_nonzero_blocker')

    def test_known_broker_codes_only_and_null_data_semantics_preserved(self):
        for key in ('errorcode','errorCode'):
            for code in ('AB1010','AG8001','AB1004'):
                with self.subTest(key=key, code=code):
                    result = self.child(position={'status':False,key:code,'message':'SECRET_MESSAGE'})
                    self.assertEqual(result['broker_code'], code)
        for code in ('AB9999','SECRET_CODE',['AB1010'], {'value':'AB1010'}):
            with self.subTest(code=code):
                result = self.child(position={'status':False,'errorcode':code})
                self.assertNotIn('broker_code', result)
        self.assertTrue(self.child(position={'status':True,'data':None}, orders={'status':True,'data':None})['flat'])

    def test_parent_resanitizes_failure_fields_and_never_echoes_captured_output(self):
        receipt = dict(flat=False, stage='SECRET_STAGE', reason='SECRET_REASON', error='SECRET_ERROR',
                       broker_code='SECRET_CODE', message='SECRET_MESSAGE', token='SECRET_TOKEN')
        result = types.SimpleNamespace(returncode=0, stdout='SECRET_LOG\nNOTE_BROKER '+json.dumps(receipt),
                                       stderr='SECRET_STDERR')
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaisesRegex(ValueError, 'fresh_broker_flat_check_failed'):
            module.broker_receipt(result, NOW)
        self.assertNotIn('SECRET', output.getvalue())
        diagnostic = json.loads(output.getvalue().split('NOTE_BROKER_DIAGNOSTIC ')[-1])
        self.assertEqual(diagnostic, dict(stage='transport', reason='diagnostic_invalid', error='OtherError'))
        for code in ('AB1010', 'SECRET_CODE'):
            receipt.update(stage='position', reason='response_unsuccessful', error='ValueError', broker_code=code)
            result.stdout = 'NOTE_BROKER '+json.dumps(receipt)
            output = io.StringIO()
            with contextlib.redirect_stdout(output), self.assertRaises(ValueError):
                module.broker_receipt(result, NOW)
            self.assertNotIn('SECRET', output.getvalue())
            self.assertEqual('broker_code' in json.loads(output.getvalue().split('NOTE_BROKER_DIAGNOSTIC ')[-1]),
                             code == 'AB1010')

    def test_invalid_child_receipts_and_failure_exit_remain_blocked(self):
        for output, returncode in (('SECRET_NO_RECEIPT',0), ('NOTE_BROKER SECRET_JSON',0),
                                  ('NOTE_BROKER []',0), ('NOTE_BROKER {}\nNOTE_BROKER {}',0),
                                  ('NOTE_BROKER {"flat":true}',0), ('SECRET_FAILURE',1)):
            result = types.SimpleNamespace(stdout=output, returncode=returncode)
            captured = io.StringIO()
            with self.subTest(output=output), contextlib.redirect_stdout(captured), self.assertRaises(ValueError):
                module.broker_receipt(result, NOW)
            self.assertNotIn('SECRET', captured.getvalue())

    def test_modes_are_mutually_exclusive_and_local_plan_never_reads_broker(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            module.parse_arguments(['--evidence','synthetic.json','--apply','--broker-check-only'])
        read = Mock(return_value={'flat':True})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(module.initial_broker_check(False, False, read))
            read.assert_not_called()
            self.assertIsNone(module.initial_broker_check(False, True, read))
            read.assert_called_once_with()
            self.assertEqual(module.initial_broker_check(True, False, read), {'flat':True})

    def test_failure_checkpoint_is_explicit_and_sanitized(self):
        result = types.SimpleNamespace(returncode=0, stdout='NOTE_BROKER '+json.dumps(
            dict(flat=False,stage='login',reason='login_failed',error='RuntimeError')))
        for checkpoint in (*module.BROKER_CHECKPOINTS, 'SECRET_CHECKPOINT'):
            output = io.StringIO()
            with self.subTest(checkpoint=checkpoint), contextlib.redirect_stdout(output), self.assertRaises(ValueError):
                module.broker_receipt(result, NOW, checkpoint)
            self.assertNotIn('SECRET', output.getvalue())
            diagnostic = json.loads(output.getvalue().split('NOTE_BROKER_DIAGNOSTIC ')[-1])
            self.assertEqual(diagnostic['checkpoint'], checkpoint if checkpoint in module.BROKER_CHECKPOINTS else 'unknown')

    def test_main_check_only_and_first_failure_return_before_any_repair_mutation(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryFile() as lock:
            root = Path(directory)
            evidence_path = root/'synthetic.json'
            evidence_path.write_text(json.dumps(self.e))
            db = sqlite3.connect(root/'kadima.db')
            self.c.backup(db); db.close()
            base = types.SimpleNamespace(ENGINE='synthetic-engine', HELPER='synthetic', TOKEN_FILE=root/'token',
                backend_plan=Mock(return_value=([],{})),
                process=Mock(return_value=('321',[],{'KADIMA_DATA_DIR':directory})),
                load_helper=Mock(return_value=types.SimpleNamespace(engine_interpreter=lambda *args:'/synthetic/python')),
                read_file=Mock(return_value=b'{"api_token":"SECRET_TOKEN"}'),
                local_api=Mock(return_value=dict(ts=NOW.isoformat(),positions=[{'signal_id':1}],
                                config={'auto_trade_enabled':False,'kill_latched':False})),
                check_fingerprints=Mock(), command=Mock())
            loader = types.SimpleNamespace(exec_module=Mock())
            spec = types.SimpleNamespace(loader=loader)
            original_connect = sqlite3.connect
            original_fdopen = module.os.fdopen
            opened_locks = []
            def tracked_fdopen(*args, **kwargs):
                handle = original_fdopen(*args, **kwargs)
                opened_locks.append(handle)
                return handle
            for apply, check_only, success in ((False,True,True),(False,True,False),(True,False,False)):
                receipt = (dict(flat=True,started_at=NOW.isoformat(),verified_at=NOW.isoformat(),
                                position_rows=0,orderBook_rows=0) if success else
                           dict(flat=False,stage='login',reason='login_failed',error='RuntimeError'))
                process_result = types.SimpleNamespace(returncode=0,
                    stdout='SECRET_SDK_LOG\nNOTE_BROKER '+json.dumps(receipt),stderr='SECRET_STDERR')
                args = types.SimpleNamespace(evidence=evidence_path, apply=apply,
                    broker_check_only=check_only, off_session_unrestored=False)
                captured = io.StringIO()
                with self.subTest(apply=apply, check_only=check_only, success=success), \
                     patch.object(module,'parse_arguments',return_value=args), \
                     patch.object(module.os,'geteuid',return_value=0), \
                     patch.object(module.os,'open',side_effect=lambda *args,**kwargs:module.os.dup(lock.fileno())), \
                     patch.object(module.os,'fdopen',side_effect=tracked_fdopen), \
                     patch.object(module.fcntl,'flock'), \
                     patch.object(module.pwd,'getpwnam',return_value=types.SimpleNamespace(pw_uid=123,pw_gid=123)), \
                     patch.object(module.importlib.util,'spec_from_file_location',return_value=spec), \
                     patch.object(module.importlib.util,'module_from_spec',return_value=types.SimpleNamespace(base=base)), \
                     patch.object(module,'datetime',wraps=datetime) as clock, \
                     patch.object(module.subprocess,'run',return_value=process_result) as run, \
                     patch.object(module,'pause_stock') as pause, patch.object(module,'repair') as repair, \
                     patch.object(module.sqlite3,'connect',wraps=original_connect) as connect, \
                     contextlib.redirect_stdout(captured):
                    clock.now.return_value=NOW
                    if success:
                        module.main()
                    else:
                        with self.assertRaisesRegex(ValueError,'fresh_broker_flat_check_failed'): module.main()
                    run.assert_called_once()
                    pause.assert_not_called(); repair.assert_not_called(); base.command.assert_not_called()
                    base.check_fingerprints.assert_not_called()
                    self.assertTrue(all('?mode=ro' in call.args[0] for call in connect.call_args_list))
                for handle in opened_locks: handle.close()
                opened_locks.clear()
                self.assertNotIn('SECRET', captured.getvalue())
                with original_connect(root/'kadima.db') as persisted:
                    self.assertEqual(persisted.execute('SELECT status FROM positions').fetchone()[0], 'exit_pending')
                    self.assertEqual(persisted.execute('SELECT state FROM execution_orders').fetchone()[0], 'unknown')


if __name__ == '__main__': unittest.main()
