"""Observational capture regressions; sockets blocked and transports mocked."""
from contextlib import ExitStack
from copy import deepcopy
import json
import hashlib
import os
import socket
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

os.environ.setdefault('KADIMA_DATA_DIR', tempfile.mkdtemp(prefix='kadima-replay-'))
from kadima import replay_capture as capture, fast_momentum as fast, stock_fast
from kadima import option_chain, execution_ledger as ledger, angel_orders, auto_executor
from test_fast_entry_quality import fixture, chopped, NOW


def market():
    one, five, spot = fixture()
    q = option_chain.OptionQuote(token='opt', symbol='TESTCE', strike=100., opt_type='CE',
        ltp=10., bid=10., ask=10.01, oi=200000, volume=10000, iv=20., delta=.5)
    chain = NS(index=NS(name='NIFTY', token='index', exchange='NSE', opt_exchange='NFO'),
        _last_poll=100., _expiry='08OCT2026', quotes={'opt': q})
    return one, five, spot, q, chain


def row(**kwargs):
    out = dict(ts=NOW.isoformat(), strategy='fast_momentum', episode='breakout:1',
        exchange='NFO', token='opt', symbol='TESTCE', expiry='2026-10-08',
        underlying='NIFTY', direction='CE', entry=10., stop=9., t1=12., t2=14.,
        scenarios=[dict(minutes=15, central=14.)], eligible=False, blocked_reasons=['paused'])
    out.update(kwargs)
    return out


class ReplayCapture(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')))
        self.stack.enter_context(patch.dict(os.environ, {'KADIMA_REPLAY_CAPTURE': '1'}))
        for key, value in (('_manifest', None), ('_seq', 0), ('_failures', 0)):
            self.stack.enter_context(patch.object(capture, key, value))
        self.events = []
        self.sink = self.stack.enter_context(patch.object(capture.audit, 'log', side_effect=self.save))
        self.stack.enter_context(patch.object(capture, 'source_manifest', return_value={'disk_sha256': {'x': 'hash'}}))

    def save(self, event, **data):
        self.events.append((event, json.loads(json.dumps(data))))

    def evidence(self, kind):
        return [v['evidence'] for k, v in self.events if k == 'REPLAY_'+kind]

    def test_identity_matches_pinned_pr20_audit_key(self):
        proposal = row()
        key = '|'.join((NOW.date().isoformat(), 'SCALP', 'breakout:1', 'NFO', 'opt'))
        expected = hashlib.sha256(key.encode()).hexdigest()[:32]
        self.assertEqual(capture.identity(proposal, 'index_fast'), expected)

    def test_captured_candles_reproduce_quality_result_without_future_data(self):
        one, five, spot, q, chain = market()
        expected = fast.setup(one, five, spot, NOW)
        capture.decision('index_fast', row(), chain, {1:one,5:five}, spot)
        e = self.evidence('DECISION')[0]
        actual = fast.setup(e['candle_inputs']['1'], e['candle_inputs']['5'], e['spot'], NOW)
        self.assertEqual(actual, expected)
        one[-1]['closed'] = False
        capture.decision('index_fast', row(), chain, {1:one,5:five}, spot)
        e = self.evidence('DECISION')[-1]
        with self.assertRaisesRegex(ValueError, 'invalid_or_unclosed_bars'):
            fast.setup(e['candle_inputs']['1'], e['candle_inputs']['5'], e['spot'], NOW)

    def test_disabled_does_not_read_source_or_write(self):
        with patch.dict(os.environ, {'KADIMA_REPLAY_CAPTURE': '0'}):
            capture.decision('stock_fast', None, None, None, None)
        self.sink.assert_not_called()
        capture.source_manifest.assert_not_called()

    def test_immutable_suggestion_distinct_decisions_no_mutation(self):
        one, five, spot, q, chain = market()
        proposal = row(holding_minutes=15)
        before = deepcopy(proposal)
        for _ in range(2):
            capture.decision('stock_fast', proposal, chain, {1: one, 5: five}, spot)
        self.assertEqual(proposal, before)
        events = self.evidence('DECISION')
        self.assertEqual(events[0]['suggestion_id'], events[1]['suggestion_id'])
        self.assertNotEqual(events[0]['decision_id'], events[1]['decision_id'])
        for changes in (dict(episode='breakout:2'), dict(exchange='BFO')):
            self.assertNotEqual(capture.identity(proposal, 'stock_fast'), capture.identity(dict(proposal, **changes), 'stock_fast'))
        one[0]['close'] = 999
        q.bid = 999
        self.assertNotEqual(events[0]['candle_inputs']['1'][0]['close'], 999)
        self.assertEqual(events[0]['quotes'][0]['bid'], 10.)

    def test_max_hold_not_inferred_from_projection(self):
        one, five, spot, q, chain = market()
        capture.decision('index_fast', row(), chain, {1: one, 5: five}, spot)
        e = self.evidence('DECISION')[0]
        self.assertIsNone(e['maximum_hold_minutes'])
        self.assertEqual(e['projection_horizon_minutes'], 15)
        self.assertFalse(e['full_replay_ready'])

    def test_signal_link_without_mutation_or_signal_allocation(self):
        proposal = row(asset_scope='STOCK')
        capture.link(proposal, 17)
        e = self.evidence('SIGNAL_LINK')[0]
        self.assertEqual(e['suggestion_id'], capture.identity(proposal, 'stock_fast'))
        self.assertEqual(e['signal_id'], 17)
        self.assertNotIn('signal_id', proposal)

    def test_sink_failure_counted_and_recovery_visible(self):
        with patch.object(capture.audit, 'log', side_effect=OSError('disk full')):
            capture.link(row(), 1)
        self.assertEqual(capture.health()['failures'], 1)
        capture.link(row(), 1)
        self.assertEqual(self.events[-1][1]['capture_failures'], 1)

    def test_partial_invalid_state_is_observational_failure(self):
        capture.index_decision(row(), NS(), {}, False)
        self.assertEqual(capture.health()['failures'], 1)

    def test_policy_allowlist_and_changed_identity(self):
        from kadima import config
        with patch.dict(os.environ, {'ANGEL_PASSWORD': 'must-not-log'}):
            before = capture.policy_snapshot()
            with patch.object(config, 'FAST_MIN_DIRECTIONAL_EFFICIENCY', .4):
                after = capture.policy_snapshot()
        self.assertNotEqual(capture.digest(before), capture.digest(after))
        self.assertNotIn('must-not-log', json.dumps(before))
        self.assertNotIn('ANGEL_PASSWORD', json.dumps(before))

    def test_index_rejection_retains_closed_inputs(self):
        one, five, spot = chopped()
        _, _, _, q, chain = market()
        st = NS(name='NIFTY', chain=chain, spot=spot, last_updated=NOW)
        with patch.object(fast.time, 'monotonic', return_value=100.), \
             patch.object(fast.storage, 'recent_candles', side_effect=[one, five]):
            result = fast.Lane().scan(st, now=NOW, record=False)
        e = self.evidence('DECISION')[0]
        self.assertEqual(e['result'], result)
        self.assertEqual(e['phase'], 'REVALIDATE')
        self.assertEqual(e['candle_inputs']['1'], one)
        self.assertEqual(e['candle_inputs']['5'], five)
        self.assertEqual(result['blocked_reasons'], ['fast_choppy_five_minute_context'])
        self.assertIsNone(e['suggestion_id'])

    def test_stock_rejection_inputs_and_no_execution_effect(self):
        one, five, spot = chopped()
        _, _, _, q, chain = market()
        cfg = dict(holding_minutes=15, revision=1, live_enabled=False)
        data = dict(chain_at=NOW.isoformat(), spot_at=NOW.isoformat(), spot=spot, fast_metrics={})
        result = stock_fast.plan(data, chain, {1:one, 5:five}, cfg, NOW)
        e = self.evidence('DECISION')[0]
        self.assertEqual(e['result'], result)
        self.assertEqual(e['context']['controls'], cfg)
        self.assertFalse(result['eligible'])

    def test_accepted_quote_preserves_bid_ask(self):
        from kadima.config import INDICES
        chain = option_chain.OptionChain(INDICES['NIFTY'], None)
        chain.quotes = {'opt': market()[3]}
        chain._expiry = '08OCT2026'
        sample = dict(symbolToken='opt', exchange='NFO', ltp=10., opnInterest=200000,
                      tradeVolume=10000, depth={'buy':[{'price':9.95}], 'sell':[{'price':10.05}]})
        with patch.object(option_chain.storage, 'insert_oi_snapshot'):
            self.assertTrue(chain.accept_full_sample([sample]))
            self.assertFalse(chain.accept_full_sample([]))
        e = self.evidence('QUOTES')[0]
        self.assertEqual((e['quotes'][0]['bid'], e['quotes'][0]['ask']), (9.95, 10.05))
        self.assertIn('CURRENT_LADDER', e['coverage'])
        self.assertEqual(len(self.evidence('QUOTES')), 1)

    def test_disk_failure_leaves_real_ledger_results_unchanged(self):
        original = self.sink.side_effect
        def failing(event, **data):
            if event.startswith('REPLAY_'):
                raise OSError('disk full')
            return original(event, **data)
        self.sink.side_effect = failing
        # Use the same real accounting path; its checks do not require replay events.
        pos = auto_executor.Position(signal_id=9902, kind='fast_momentum', index='NIFTY',
            direction='CE', symbol='TESTCE', token='opt', exchange='NFO', entry_price=0.,
            entry_ts=NOW, lots=1, lot_size=10, remaining_qty=0, status='entry_pending')
        c = ledger.connection()
        with c:
            c.execute('DELETE FROM execution_fills')
            c.execute('DELETE FROM execution_orders')
            c.execute('INSERT INTO execution_orders (intent,signal_id,side,quantity,state,created_at) VALUES (?,?,?,?,?,?)',
                      ('FAILTEST',9902,'BUY',10,'pending',NOW.isoformat()))
        order = dict(c.execute('SELECT * FROM execution_orders').fetchone())
        with patch.object(ledger.alerts, 'enqueue'):
            ledger.apply_fill(pos, order, angel_orders.OrderStatus('O2','complete',10,11.,{}))
        self.assertEqual((pos.remaining_qty, pos.status, pos.entry_price), (10, 'open', 11.))
        self.assertEqual(c.execute('SELECT SUM(quantity) FROM execution_fills').fetchone()[0], 10)
        self.assertEqual(capture.health()['failures'], 2)

    def test_held_full_quote_captures_only_known_fields(self):
        capture.held_quote('NFO','opt',dict(ltp=12., tradingSymbol='TESTCE',
            depth={'buy':[{'price':11.95}], 'sell':[{'price':12.05}]}, clientcode='secret'))
        e = self.evidence('HELD_QUOTE')[0]
        self.assertEqual(e['quote']['depth']['buy'][0]['price'], 11.95)
        self.assertNotIn('clientcode', e['quote'])

    def test_partial_duplicate_terminal_regressing_fills_preserve_ledger(self):
        pos = auto_executor.Position(signal_id=9901, kind='fast_momentum', index='NIFTY',
            direction='CE', symbol='TESTCE', token='opt', exchange='NFO', entry_price=0.,
            entry_ts=NOW, lots=1, lot_size=10, remaining_qty=0, status='entry_pending')
        c = ledger.connection()
        with c:
            c.execute('DELETE FROM execution_fills')
            c.execute('DELETE FROM execution_orders')
            c.execute('INSERT INTO execution_orders (intent,signal_id,side,quantity,state,created_at) VALUES (?,?,?,?,?,?)',
                      ('INTENT',9901,'BUY',10,'pending',NOW.isoformat()))
        order = dict(c.execute('SELECT * FROM execution_orders').fetchone())
        raw = dict(orderid='O1', fillid='F1', exchtime='exchange-time', secret='never-log')
        status = angel_orders.OrderStatus('O1','open',4,10.,raw)
        with patch.object(ledger.alerts, 'enqueue'):
            ledger.apply_fill(pos, order, status)
            ledger.apply_fill(pos, order, status)
            ledger.apply_fill(pos, order, angel_orders.OrderStatus('O1','complete',10,11.,raw))
        self.assertEqual(pos.remaining_qty, 10)
        self.assertEqual(c.execute('SELECT SUM(quantity) FROM execution_fills').fetchone()[0], 10)
        committed = [e for e in self.evidence('BROKER') if e['phase']=='COMMITTED']
        self.assertEqual([e['delta']['quantity'] for e in committed], [4,0,6])
        self.assertTrue(all(e['intent']=='INTENT' and e['order_id']=='O1' for e in committed))
        self.assertIsNone(committed[0]['charges'])
        self.assertNotIn('secret', committed[0]['broker_fields'])
        with self.assertRaises(ValueError):
            ledger.apply_fill(pos, order, status)


if __name__ == '__main__':
    unittest.main()
