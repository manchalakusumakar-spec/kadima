"""Complete stopped-account broker reads; fake SDK, no network or credentials."""
import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('stopped_broker_reader', ROOT/'momentum_recovery/stopped_broker_read.py')
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)
ACCOUNT = 'TESTACCOUNT'
ACCOUNT_HASH = hashlib.sha256(ACCOUNT.encode()).hexdigest()
EVIDENCE = dict(account_sha256=ACCOUNT_HASH, exchange='BFO', token='123', symbol='TESTBANKEXPE')


def position(**changes):
    return dict(m.ALLOWED_HOLDING, **changes)


def order(**changes):
    return dict(dict(exchange='NFO', tradingsymbol='NIFTY29SEP2623250CE', symboltoken='73921',
                     orderid='order1', orderstatus='complete', quantity='130',
                     filledshares='130', unfilledshares='0'), **changes)


class ChildReaderTests(unittest.TestCase):
    def child(self, positions=None, orders=None, *, account=ACCOUNT, position_response=None,
              order_response=None, login_error=None, position_error=None, order_error=None):
        calls = []
        def login():
            calls.append('login')
            if login_error:
                raise login_error
        def read_position():
            calls.append('position')
            if position_error:
                raise position_error
            return position_response if position_response is not None else dict(status=True, data=positions or [])
        def read_orders():
            calls.append('orderBook')
            if order_error:
                raise order_error
            return order_response if order_response is not None else dict(status=True, data=orders or [])
        api = SimpleNamespace(position=read_position, orderBook=read_orders)
        config, session = ModuleType('kadima.config'), ModuleType('kadima.session')
        config.ANGEL_CLIENT_ID = account
        session.SESSION = SimpleNamespace(ensure=login, api=api)
        env = dict(KADIMA_NOTE_ACCOUNT_SHA256=ACCOUNT_HASH, KADIMA_HISTORY_EXCHANGE='BFO',
                   KADIMA_HISTORY_TOKEN='123', KADIMA_HISTORY_SYMBOL='TESTBANKEXPE')
        output = io.StringIO()
        with patch.dict(sys.modules, {'kadima.config': config, 'kadima.session': session}), \
             patch.dict(os.environ, env), contextlib.redirect_stdout(output), \
             patch('socket.create_connection', side_effect=AssertionError('network prohibited')):
            exec(m.CHILD_CODE, {})
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        receipt = json.loads(output.getvalue().split(m.PREFIX, 1)[1])
        m.validate_receipt(receipt, datetime.now(timezone.utc))
        return receipt, calls, output.getvalue()

    def test_exact_external_holding_is_ready_for_review_but_never_flat(self):
        value, calls, _ = self.child([position()], [order()])
        self.assertEqual(calls, ['login', 'position', 'orderBook'])
        self.assertTrue(value['books_complete'])
        self.assertTrue(value['readiness'])
        self.assertFalse(value['flat'])
        self.assertTrue(value['historical_contract_absent'])
        self.assertTrue(value['permitted_manual_holding_observed'])
        self.assertEqual(value['nonzero_positions'], [m.ALLOWED_HOLDING])

    def test_successful_empty_books_are_distinguished_from_failed_books(self):
        value, _, _ = self.child()
        self.assertTrue(value['flat'])
        self.assertTrue(value['readiness'])
        value, _, _ = self.child(position_response=dict(status=True, data=None),
                                 order_response=dict(status=True, data=None))
        self.assertTrue(value['flat'])
        self.assertEqual((value['position_rows'], value['order_rows']), (0, 0))

    def test_unknown_position_does_not_short_circuit_pending_order_book(self):
        value, calls, _ = self.child([position(netqty=65)], [order(orderstatus='open')])
        self.assertEqual(calls, ['login', 'position', 'orderBook'])
        self.assertTrue(value['books_complete'])
        self.assertFalse(value['readiness'])
        self.assertFalse(value['flat'])
        self.assertEqual(value['pending_orders'], 1)

    def test_allowed_holding_plus_other_exposure_is_blocked(self):
        value, _, _ = self.child([position(), position(symboltoken='999', tradingsymbol='OTHERCE')])
        self.assertTrue(value['permitted_manual_holding_observed'])
        self.assertFalse(value['readiness'])
        self.assertEqual(value['error']['reason'], 'unexpected_exposure')

    def test_historical_nonzero_position_is_not_repair_admission(self):
        historical = position(exchange='BFO', producttype='INTRADAY', symboltoken='123',
                              tradingsymbol='TESTBANKEXPE', netqty=60)
        value, _, _ = self.child([position(), historical])
        self.assertFalse(value['historical_contract_absent'])
        self.assertFalse(value['readiness'])
        self.assertEqual(value['error']['reason'], 'historical_exposure_present')
        value, _, _ = self.child([dict(historical, netqty=0)])
        self.assertTrue(value['historical_contract_absent'])
        self.assertTrue(value['flat'])

    def test_mismatched_historical_symbol_blocks_even_zero_quantity(self):
        value, calls, _ = self.child([position(exchange='BFO', symboltoken='123', netqty=0)])
        self.assertFalse(value['books_complete'])
        self.assertIsNone(value['flat'])
        self.assertEqual(value['error']['reason'], 'historical_identity_mismatch')
        self.assertIn('orderBook', calls)

    def test_account_mismatch_fails_before_login_and_books(self):
        value, calls, raw = self.child(account='OTHERACCOUNT')
        self.assertEqual(calls, [])
        self.assertFalse(value['books_complete'])
        self.assertEqual(value['error']['reason'], 'account_mismatch')
        self.assertNotIn('OTHERACCOUNT', raw)
        self.assertNotIn(ACCOUNT_HASH, raw)

    def test_login_failure_suppresses_secret_exception_message(self):
        value, calls, raw = self.child(login_error=RuntimeError('SECRET_TOKEN_UNSAFE'))
        self.assertEqual(calls, ['login'])
        self.assertEqual(value['error'], dict(stage='login', reason='login_failed', type='RuntimeError'))
        self.assertNotIn('SECRET_TOKEN_UNSAFE', raw)

    def test_transport_error_in_first_book_still_reads_second(self):
        value, calls, raw = self.child(position_error=ConnectionError('PRIVATE_RESPONSE'))
        self.assertEqual(calls, ['login', 'position', 'orderBook'])
        self.assertFalse(value['books_complete'])
        self.assertIsNone(value['flat'])
        self.assertEqual(value['order_rows'], 0)
        self.assertNotIn('PRIVATE_RESPONSE', raw)

    def test_invalid_or_missing_response_cannot_look_like_flatness(self):
        for response in ({'status': False, 'data': []}, {'status': True},
                         {'status': 1, 'data': []}, {'status': True, 'data': {}},
                         {'status': True, 'data': [None]}):
            with self.subTest(response=response):
                value, calls, _ = self.child(position_response=response)
                self.assertFalse(value['books_complete'])
                self.assertFalse(value['readiness'])
                self.assertIsNone(value['flat'])
                self.assertEqual(calls[-1], 'orderBook')

    def test_invalid_quantity_and_position_identity_rows_block(self):
        variants = [position(netqty=x) for x in (None, True, 'NaN', 'Infinity', '', '130.5', 130.5, 10**20)]
        variants += [position(exchange={}), position(producttype=[]), position(symboltoken=True),
                     position(tradingsymbol='SECRET\nRAW')]
        for row in variants:
            with self.subTest(row=row):
                value, calls, raw = self.child([row])
                self.assertFalse(value['books_complete'])
                self.assertIsNone(value['flat'])
                self.assertEqual(value['nonzero_positions'], [])
                self.assertEqual(calls[-1], 'orderBook')
                self.assertNotIn('SECRET', raw)

    def test_duplicate_position_identity_is_ambiguous(self):
        value, _, _ = self.child([position(), position()])
        self.assertFalse(value['books_complete'])
        self.assertEqual(value['error']['reason'], 'position_duplicate')

    def test_order_statuses_and_conflicting_status_fields_are_not_terminal(self):
        for status in ('open', 'pending', 'unknown', 'trigger pending'):
            with self.subTest(status=status):
                value, _, _ = self.child(orders=[order(orderstatus=status)])
                self.assertTrue(value['books_complete'])
                self.assertEqual(value['pending_orders'], 1)
                self.assertFalse(value['readiness'])
        for row in (order(orderstatus=None), order(orderstatus=True),
                    order(status='open'), order(orderstatus='SECRET\nDATA')):
            with self.subTest(row=row):
                value, _, _ = self.child(orders=[row])
                self.assertFalse(value['books_complete'])
                self.assertIsNone(value['flat'])

    def test_each_supported_terminal_status_is_read_as_terminal(self):
        for status in ('complete', 'completed', 'filled', 'cancelled', 'canceled', 'rejected'):
            with self.subTest(status=status):
                value, _, _ = self.child(orders=[order(orderstatus=status, status=status)])
                self.assertTrue(value['readiness'])
                self.assertEqual(value['pending_orders'], 0)

    def test_duplicate_or_invalid_order_ids_and_quantities_block(self):
        groups = [[order(), order()], [order(orderid=None)], [order(orderid=True)],
                  [order(quantity='NaN')], [order(filledshares='131')],
                  [order(filledshares='100', unfilledshares='100')],
                  [order(symboltoken=None)]]
        for rows in groups:
            with self.subTest(rows=rows):
                value, _, _ = self.child(orders=rows)
                self.assertFalse(value['books_complete'])
                self.assertFalse(value['readiness'])

    def test_each_book_bound_is_enforced_and_summary_is_capped(self):
        for keyword, row in (('positions', position(netqty=0)), ('orders', order())):
            with self.subTest(book=keyword):
                value, calls, _ = self.child(**{keyword: [row]*2001})
                self.assertFalse(value['books_complete'])
                self.assertEqual(value['error']['reason'], 'book_too_large')
                self.assertEqual(calls[-1], 'orderBook')
        rows = [position(symboltoken=str(100+i), tradingsymbol='OTHER'+str(i)) for i in range(9)]
        rows.append(position())
        value, _, _ = self.child(rows)
        self.assertTrue(value['books_complete'])
        self.assertEqual(value['nonzero_position_count'], 10)
        self.assertEqual(len(value['nonzero_positions']), 8)
        self.assertTrue(value['permitted_manual_holding_observed'])
        self.assertFalse(value['readiness'])


class ParentReaderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.target = Path(self.temporary.name).resolve()
        self.python = Path(sys.executable)
        self.user = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
        self.now = datetime(2026, 9, 28, 0, 0, tzinfo=timezone.utc)
        self.receipt = dict(started_at=self.now.isoformat(), checked_at=self.now.isoformat(),
            books_complete=True, flat=False, readiness=True, historical_contract_absent=True,
            permitted_manual_holding_observed=True, position_rows=1, order_rows=0,
            pending_orders=0, nonzero_positions=[m.ALLOWED_HOLDING.copy()], nonzero_position_count=1, error=None)
        self.calls = []

    def invoke(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout='SDK private output ignored\n'+m.PREFIX+json.dumps(self.receipt)+'\n',
                               stderr='PRIVATE_PROVIDER_ERROR')

    def read(self, **overrides):
        kwargs = dict(invoke=self.invoke, now=self.now)
        kwargs.update(overrides)
        return m.read_broker(self.python, dict(ANGEL_CLIENT_ID=ACCOUNT, ANGEL_PASSWORD='PRIVATE'),
                             self.user, self.target, EVIDENCE, **kwargs)

    def test_parent_uses_verified_context_and_returns_only_validated_receipt(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            value = self.read()
        self.assertEqual(value, self.receipt)
        self.assertEqual(output.getvalue(), '')
        command, kwargs = self.calls[0]
        self.assertEqual(command, [str(self.python), '-B', '-c', m.CHILD_CODE])
        self.assertEqual(kwargs['cwd'], self.target)
        self.assertEqual((kwargs['user'], kwargs['group'], kwargs['extra_groups']),
                         (self.user.pw_uid, self.user.pw_gid, []))
        self.assertEqual(kwargs['timeout'], 60)
        self.assertEqual(kwargs['env']['ORDER_PLACEMENT_ENABLED'], '0')
        self.assertEqual(kwargs['env']['KADIMA_NOTE_ACCOUNT_SHA256'], ACCOUNT_HASH)
        self.assertEqual(kwargs['env']['KADIMA_HISTORY_TOKEN'], '123')
        self.assertNotIn('PRIVATE', json.dumps(value))

    def test_parent_account_mismatch_never_launches_child(self):
        with self.assertRaisesRegex(m.Blocked, 'account_binding_mismatch'):
            m.read_broker(self.python, dict(ANGEL_CLIENT_ID='OTHER'), self.user, self.target,
                          EVIDENCE, invoke=self.invoke, now=self.now)
        self.assertEqual(self.calls, [])

    def test_failed_or_timed_out_child_never_forwards_provider_text(self):
        def fail(*args, **kwargs):
            raise subprocess.TimeoutExpired('SECRET_COMMAND', 60, output='PRIVATE')
        with self.assertRaisesRegex(m.Blocked, '^broker_read_timed_out$'):
            self.read(invoke=fail)
        def child_failure(*args, **kwargs):
            return SimpleNamespace(returncode=1, stdout='PRIVATE', stderr='SECRET')
        with self.assertRaisesRegex(m.Blocked, '^broker_reader_failed$'):
            self.read(invoke=child_failure)

    def test_oversize_duplicate_or_malformed_receipts_are_refused(self):
        marker = m.PREFIX+json.dumps(self.receipt)+'\n'
        for output in ('x'*65_537, marker+marker, 'no receipt', m.PREFIX+'{bad json',
                       m.PREFIX+json.dumps(dict(self.receipt, raw_provider='SECRET'))):
            with self.subTest(length=len(output)):
                invoke = lambda *a, **k: SimpleNamespace(returncode=0, stdout=output, stderr='PRIVATE')
                with self.assertRaises(m.Blocked):
                    self.read(invoke=invoke)

    def test_stale_future_or_overlong_book_window_is_refused(self):
        cases = [dict(started_at=(self.now-timedelta(seconds=61)).isoformat()),
                 dict(started_at=(self.now-timedelta(seconds=45)).isoformat(),
                      checked_at=(self.now-timedelta(seconds=31)).isoformat()),
                 dict(checked_at=(self.now+timedelta(seconds=1)).isoformat()),
                 dict(started_at='2026-09-28T00:00:00')]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(m.Blocked):
                m.validate_receipt(dict(self.receipt, **changes), self.now)

    def test_contradictory_or_unsafe_receipt_fields_are_refused(self):
        cases = [dict(flat=True), dict(books_complete=1), dict(readiness=False),
                 dict(pending_orders=True), dict(nonzero_position_count=0),
                 dict(order_rows=0, pending_orders=1), dict(nonzero_positions=[]),
                 dict(nonzero_positions=[dict(m.ALLOWED_HOLDING, secret='PRIVATE')]),
                 dict(error=dict(stage='login', reason='PRIVATE', type='ValueError'))]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(m.Blocked):
                m.validate_receipt(dict(self.receipt, **changes), self.now)


if __name__ == '__main__':
    unittest.main()
