"""Offline execution regression suite. Broker calls are replaced at the boundary."""
import importlib
import sys
import tempfile
import threading
import time
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
PKG = '_kadima_execution_test'
pkg = types.ModuleType(PKG)
pkg.__path__ = [str(ROOT / 'kadima')]
sys.modules[PKG] = pkg

def stub(name, **attrs):
    mod = types.ModuleType(PKG + '.' + name)
    mod.__dict__.update(attrs)
    sys.modules[mod.__name__] = mod
    setattr(pkg, name, mod)
    return mod

NOW = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
config = stub('config', DAILY_LOSS_LIMIT_INR=500, IST=timezone.utc,
              MAX_CONCURRENT_POSITIONS=1, MAX_LOTS_PER_SIGNAL=1,
              ORDER_PLACEMENT_ENABLED=True, in_no_auto_exit_window=lambda:False,
              DATA_DIR='', DB_PATH='')
audit = stub('audit', log=Mock())
alerts = stub('alerts', enqueue=Mock())
capital = stub('capital_guard', get_capital_for_signal=Mock(return_value=10000),
               enforce=Mock(return_value=(1, 'ok')), record_realized_pnl=Mock())
entry = stub('entry_quality', EntryContext=lambda **kw:types.SimpleNamespace(**kw),
             evaluate_entry=lambda _:types.SimpleNamespace(allowed=True))
stub('risk_budget', StrikeCandidate=lambda **kw:types.SimpleNamespace(**kw))
stub('strike_selector')
stub('killswitch', engaged=lambda:False)
stub('instrument_master', get_lot_size=lambda *a:10)
stub('utils', now_ist=lambda:NOW, in_market_hours=lambda:True, past_signal_cutoff=lambda:False)
session = stub('session', SESSION=types.SimpleNamespace(ensure=Mock(), api=Mock()))
limiter = lambda:types.SimpleNamespace(wait_and_allow=lambda **kw:True, allow=lambda:True)
stub('rate_limiter', ORDER_STATUS=limiter(), ORDER_WRITE=limiter(), QUOTE=limiter())
exit_manager = stub('exit_manager', GAIN_ACTIVATION_PCT=.05, GIVEBACK_PCT=.5,
                    INITIAL_SL_PCT=.3, realized_pnl_today=lambda:0)
storage = importlib.import_module(PKG+'.storage')
orders = importlib.import_module(PKG+'.angel_orders')
ledger = importlib.import_module(PKG+'.execution_ledger')
executor = importlib.import_module(PKG+'.auto_executor')


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        storage.DATA_DIR = self.tmp.name
        storage.DB_PATH = str(Path(self.tmp.name)/'test.db')
        storage._local = threading.local()
        self.ex = executor.AutoExecutor()
        self.ex._reconcile_blocked = False
        self.ex._positions_verified = True
        self.ex._restored = True
        self.pos = executor.Position(1, 'gamma_blast', 'INDEX', 'CE', 'TESTCE',
                     'T', 'NFO', 100, NOW, 1, 10, 10, peak_price=100)
        self.ex.state.positions['1'] = self.pos
        storage.upsert_position(self.pos)
        self.place = patch.object(orders, 'place_limit_order',
                                 return_value=orders.OrderResult(True,'O1','ok')).start()
        self.book = patch.object(orders, 'fetch_order_book', return_value=[]).start()
        self.cancel = patch.object(orders, 'cancel_order', return_value=orders.OrderResult(True,'O1','ok')).start()
        patch.object(executor, '_poll_ltp', return_value={'T':100}).start()
        patch.object(executor, 'now_ist', return_value=NOW).start()
        patch.object(exit_manager, 'realized_pnl_today', return_value=0).start()
        config.in_no_auto_exit_window = lambda:False

    def tearDown(self):
        patch.stopall()
        if getattr(storage._local, 'conn', None):
            storage._local.conn.close()
        self.tmp.cleanup()

    def fill(self, qty, price, status='open', oid='O1'):
        self.book.return_value = [orders.OrderStatus(oid, status, qty, price, {})]
        self.ex.refresh_orders()

    def pnl(self):
        return ledger.connection().execute('SELECT COALESCE(SUM(pnl),0) FROM execution_fills').fetchone()[0]

    def test_rejected_sell_keeps_position_and_zero_pnl(self):
        self.place.return_value = orders.OrderResult(False,None,'rate_limited')
        r = self.ex._close_position(self.pos,90,'stop')
        self.assertFalse(r['ok'])
        self.assertEqual(self.pos.status,'open')
        self.assertEqual(self.pos.remaining_qty,10)
        self.assertEqual(self.pnl(),0)

    def test_acknowledgement_is_not_fill(self):
        r = self.ex._close_position(self.pos,90,'stop')
        self.assertTrue(r['pending'])
        self.assertEqual(self.pos.status,'exit_pending')
        self.assertEqual(self.pos.remaining_qty,10)
        self.assertEqual(self.pnl(),0)
        self.assertIsNone(self.pos.exit_price)

    def test_partial_sell_then_complete_exactly_once(self):
        self.ex._close_position(self.pos,90,'stop')
        self.fill(4,90)
        self.assertEqual(self.pos.remaining_qty,6)
        self.assertEqual(self.pnl(),-40)
        self.fill(4,90)
        self.assertEqual(self.pnl(),-40)
        self.fill(10,92,'complete')
        self.assertEqual(self.pos.remaining_qty,0)
        self.assertEqual(self.pnl(),-80)
        self.assertNotIn('1',self.ex.state.positions)

    def test_cancelled_partial_sell_preserves_remainder(self):
        self.ex._close_position(self.pos,90,'stop')
        self.fill(4,90,'cancelled')
        self.assertEqual((self.pos.status,self.pos.remaining_qty),('open',6))
        self.place.return_value = orders.OrderResult(True,'O2','ok')
        self.ex._close_position(self.pos,89,'stop')
        self.assertEqual(self.place.call_args.kwargs['quantity'],6)
        self.fill(6,89,'complete','O2')
        self.assertEqual(self.pnl(),-106)

    def test_unknown_submission_blocks_duplicate(self):
        self.place.return_value = orders.OrderResult(False,None,'timeout',uncertain=True)
        self.ex._close_position(self.pos,90,'stop')
        self.ex._close_position(self.pos,90,'stop')
        self.assertEqual(self.place.call_count,1)
        self.assertEqual(self.pos.status,'exit_pending')
        self.assertEqual(len(ledger.pending()),1)

    def test_unknown_submission_recovers_by_tag(self):
        self.place.return_value = orders.OrderResult(False,None,'timeout',uncertain=True)
        self.ex._close_position(self.pos,90,'stop')
        tag = ledger.pending()[0]['intent']
        self.book.return_value = [orders.OrderStatus('LATE','complete',10,91,{'ordertag':tag})]
        self.ex.refresh_orders()
        self.assertEqual(self.pnl(),-90)
        self.assertEqual(self.pos.status,'closed')

    def test_duplicate_manual_and_automatic_exit(self):
        with ThreadPoolExecutor(4) as pool:
            result=list(pool.map(lambda _:self.ex._close_position(self.pos,90,'stop'),range(8)))
        self.assertEqual(self.place.call_count,1)
        self.assertEqual(sum(bool(r.get('pending')) for r in result),1)

    def test_buy_tracks_only_filled_quantity_and_average(self):
        self.pos.entry_price=0
        self.pos.remaining_qty=0
        ledger.submit(self.pos,'BUY',10,105)
        self.assertEqual(self.pos.remaining_qty,0)
        self.fill(3,101)
        self.assertEqual((self.pos.entry_price,self.pos.remaining_qty),(101,3))
        self.cancel.assert_called_once_with('O1')
        self.fill(3,101,'cancelled')
        self.assertEqual((self.pos.status,self.pos.remaining_qty),('open',3))
        row=storage.load_open_positions()[0]
        self.assertEqual((row['entry_price'],row['remaining_qty']),(101,3))

    def test_restart_recovers_pending_and_does_not_reapply_fills(self):
        self.ex._close_position(self.pos,90,'stop')
        self.fill(4,90)
        row=storage.load_open_positions()[0]
        row['entry_ts']=datetime.fromisoformat(row['entry_ts'])
        row['exit_ts']=datetime.fromisoformat(row['exit_ts'])
        restored=executor.Position(**row)
        self.ex.state.positions={'1':restored}
        self.fill(4,90)
        self.assertEqual(restored.remaining_qty,6)
        self.assertEqual(self.pnl(),-40)
        self.fill(10,92,'complete')
        self.assertEqual(self.pnl(),-80)

    def test_unavailable_book_preserves_pending(self):
        self.ex._close_position(self.pos,90,'stop')
        self.book.return_value=None
        self.ex.refresh_orders()
        self.assertEqual(self.pos.remaining_qty,10)
        self.assertTrue(self.ex._reconcile_blocked)

    def test_invalid_fill_does_not_change_position(self):
        self.ex._close_position(self.pos,90,'stop')
        self.fill(11,90,'complete')
        self.assertEqual(self.pos.remaining_qty,10)
        self.assertEqual(self.pnl(),0)

    def test_eod_overrides_suppression_window(self):
        config.in_no_auto_exit_window=lambda:True
        executor.now_ist.return_value=NOW.replace(hour=15,minute=38)
        self.ex.check_exits()
        self.place.assert_called_once()
        self.assertEqual(self.pos.exit_reason,'eod_force_close')

    def test_daily_loss_overrides_suppression_and_missing_quote(self):
        config.in_no_auto_exit_window=lambda:True
        exit_manager.realized_pnl_today.return_value=-600
        executor._poll_ltp.return_value={}
        self.ex.check_exits()
        self.place.assert_called_once()
        self.assertEqual(self.pos.exit_reason,'daily_loss_limit_breached')

    def test_peak_persists_before_restart(self):
        executor._poll_ltp.return_value={'T':140}
        self.ex.check_exits()
        self.assertEqual(storage.load_open_positions()[0]['peak_price'],140)

    def test_external_position_blocks_startup(self):
        with patch.object(executor,'fetch_broker_positions',return_value=[
            {'exchange':'NFO','symboltoken':'UNKNOWN','producttype':'INTRADAY','netqty':'10'}]):
            self.ex.reconcile_on_startup()
        self.assertTrue(self.ex._reconcile_blocked)
        self.place.assert_not_called()

    def test_startup_recovers_after_transient_failure(self):
        with patch.object(executor,'fetch_broker_positions',return_value=None):
            self.ex.reconcile_on_startup()
        self.assertTrue(self.ex._reconcile_blocked)
        with patch.object(executor,'fetch_broker_positions',return_value=[
            {'exchange':'NFO','symboltoken':'T','producttype':'INTRADAY','netqty':'10'}]):
            self.ex.reconcile_on_startup()
        self.assertFalse(self.ex._reconcile_blocked)

    def test_dry_run_fills_are_marked_and_not_live_pnl(self):
        self.place.return_value=orders.OrderResult(True,'DRY','dry_run',dry_run=True)
        self.ex._close_position(self.pos,90,'stop')
        c=ledger.connection()
        self.assertEqual(c.execute('SELECT SUM(pnl) FROM execution_fills WHERE dry_run=0').fetchone()[0],None)
        self.assertEqual(self.pos.status,'closed')

    def test_simultaneous_entries_reserve_one_slot(self):
        self.ex.state.positions.clear()
        self.pos.status='closed'
        storage.upsert_position(self.pos)
        q=types.SimpleNamespace(symbol='TESTCE',token='T',strike=10000,opt_type='CE',
            ltp=100,bid=99.5,ask=100.5,delta=.5,iv=80,volume=1000,oi=2000,spread_pct=1)
        chain=types.SimpleNamespace(quotes={'T':q},index=types.SimpleNamespace(opt_exchange='NFO'),
                                   _last_poll=time.monotonic())
        pkg.strike_selector.rank=lambda *args:[types.SimpleNamespace(rejected=False,quote=q)]
        pkg.risk_budget.select_within_budget=lambda *args,**kw:types.SimpleNamespace(candidate=q,lots=1)
        q.stop_price=90
        config.STRIKES_EACH_SIDE=5
        chain.atm=10000
        chain.scrips=types.SimpleNamespace(strikes_around=lambda *a:[{'token':'T'}])
        self.ex._structure=lambda *args:(9980,1)
        chain._spot_received_at=chain._alignment_at=time.monotonic()
        snap=types.SimpleNamespace(vwap=9990,ema_fast=10000,ema_slow=9990,rsi=60)
        def enter(sid):
            self.ex.evaluate_and_enter('INDEX','CE',chain,10000,snap,1,'gamma_blast',sid,'CONFIRMED',90)
        with ThreadPoolExecutor(4) as pool:
            list(pool.map(enter,range(20,28)))
        self.assertEqual(self.place.call_count,1)
        self.assertEqual(len(self.ex.state.positions),1)
        pos=next(iter(self.ex.state.positions.values()))
        self.assertEqual((pos.status,pos.remaining_qty,pos.entry_price),('entry_pending',0,0))

    def test_gap_live_uses_shared_executor_one_lot_and_revalidation(self):
        self.ex.state.positions.clear()
        q=types.SimpleNamespace(symbol='STOCKCE',token='T',strike=100,opt_type='CE',
            ltp=100,bid=99.5,ask=100.5,delta=.5,iv=40,volume=5000,oi=10000,
            spread_pct=1,stop_price=90)
        chain=types.SimpleNamespace(quotes={'T':q},
            index=types.SimpleNamespace(opt_exchange='NFO'),_last_poll=time.monotonic())
        chain._spot_received_at=time.monotonic()
        chain.atm=100
        chain.scrips=types.SimpleNamespace(strikes_around=lambda *a:[{'token':'T'}])
        chain._entry_guard=lambda:True
        config.STRIKES_EACH_SIDE=1
        pkg.strike_selector.rank=lambda *args:[types.SimpleNamespace(rejected=False,quote=q)]
        pkg.risk_budget.select_within_budget=lambda *args,**kw:types.SimpleNamespace(candidate=q,lots=1)
        context=dict(eligible=True,strategy='gap_and_go',underlying='STOCK',direction='CE',
            ts=NOW.isoformat(),episode='gap:STOCK:1',token='T',contract_symbol='STOCKCE',
            exchange='NFO',spot_stop=98,days=3,entry=100.5,score=80)
        with patch.object(executor.gap_and_go,'live_blocks',return_value=[]), \
             patch.object(executor.celestial,'fresh',return_value=True), \
             patch.object(executor.trade_policy,'premium_stop',return_value=90), \
             patch.object(executor.trade_policy,'lots_within_allocation',return_value=1):
            result=self.ex.evaluate_and_enter('STOCK','CE',chain,100,
                types.SimpleNamespace(vwap=99),3,'gap_and_go',77,'CONFIRMED',80,
                gap_context=lambda:dict(context))
        self.assertTrue(result['ok'])
        self.assertEqual(self.place.call_count,1)
        pos=self.ex.state.positions['77']
        self.assertEqual((pos.kind,pos.lots,pos.status),('gap_and_go',1,'entry_pending'))

    def test_gap_live_rejects_second_position_same_underlying(self):
        self.ex.state.positions={'1':executor.Position(
            1,'gap_and_go','STOCK','CE','OLDCE','OLD','NFO',10,NOW,1,10,10)}
        chain=types.SimpleNamespace(_entry_guard=lambda:True)
        context=dict(eligible=True,strategy='gap_and_go',underlying='STOCK',direction='CE',
            ts=NOW.isoformat())
        with patch.object(executor.gap_and_go,'live_blocks',return_value=[]), \
             patch.object(executor.celestial,'fresh',return_value=True):
            result=self.ex.evaluate_and_enter('STOCK','CE',chain,100,None,3,
                'gap_and_go',78,'CONFIRMED',80,gap_context=lambda:context)
        self.assertEqual(result['reason'],'same_underlying_position_exists')

    def test_realized_pnl_includes_partial_and_excludes_paper(self):
        import ast
        tree=ast.parse((ROOT/'kadima/exit_manager.py').read_text())
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='realized_pnl_today')
        ns={'__package__':PKG,'storage':storage,'now_ist':lambda:NOW}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'pnl','exec'),ns)
        self.ex._close_position(self.pos,90,'stop')
        self.fill(4,90)
        self.assertEqual(ns['realized_pnl_today'](),-40)
        self.fill(10,92,'complete')
        self.assertEqual(ns['realized_pnl_today'](),-80)

    def test_storage_failure_does_not_apply_fill_in_memory(self):
        self.ex._close_position(self.pos,90,'stop')
        row=ledger.pending()[0]
        with patch.object(storage,'upsert_position',side_effect=RuntimeError('disk error')):
            with self.assertRaises(RuntimeError):
                ledger.apply_fill(self.pos,row,orders.OrderStatus('O1','open',4,90,{}))
        self.assertEqual(self.pos.remaining_qty,10)
        self.assertEqual(self.pnl(),0)


class BrokerParsing(unittest.TestCase):
    def test_false_status_never_accepts_nested_id(self):
        self.assertIsNone(orders._extract_order_id({'status':False,'data':{'orderid':'X'}}))

    def test_unavailable_and_empty_are_distinct(self):
        api=session.SESSION.api
        with patch.object(api,'orderBook',return_value={'status':False,'data':[]}):
            self.assertIsNone(orders.fetch_order_book())
        with patch.object(api,'orderBook',return_value={'status':True,'data':[]}):
            self.assertEqual(orders.fetch_order_book(),[])

    def test_session_failure_stays_inside_quote_boundary(self):
        with patch.object(session.SESSION,'ensure',side_effect=RuntimeError('unavailable')):
            self.assertEqual(executor._poll_ltp('NFO',['T']),{})

if __name__=='__main__':
    unittest.main()
