"""Offline transactional repair tests; no broker credentials or account data."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import unittest
from datetime import datetime
import contextlib
import io
import sys
import types
from unittest.mock import patch

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


if __name__ == '__main__': unittest.main()
