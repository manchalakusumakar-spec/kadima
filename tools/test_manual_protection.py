"""Offline manual-option adoption, persistence and exit tests; no broker calls."""
import os
import sys
import tempfile
import ast
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ['KADIMA_DATA_DIR'] = tempfile.mkdtemp(prefix='kadima-manual-protection-')
os.environ['KADIMA_CELESTIAL_ENABLED'] = '1'
sys.path.insert(0, '.')

from kadima import (alerts, angel_orders, auto_executor as ae, celestial,
                    celestial_adoption as adoption, config, execution_ledger,
                    manual_protection, owned_watchdog, storage)

AT = datetime(2026,10,9,15,20,tzinfo=config.IST)
MASTER = SimpleNamespace(rows=[dict(exch_seg='NFO',instrumenttype='OPTIDX',
    expiry='13OCT2026',lotsize='65',token='44597',symbol='NIFTY13OCT2622250PE',name='NIFTY')])


def broker(product='CARRYFORWARD', **changes):
    row = dict(exchange='NFO',symboltoken='44597',producttype=product,netqty='65',
               tradingsymbol='NIFTY13OCT2622250PE',buyavgprice='100')
    row.update(changes)
    return row


def pending(token):
    return SimpleNamespace(order_id='external',status='open',raw=dict(
        exchange='NFO',symboltoken=token,producttype='INTRADAY'))


def inspect(ex, rows, book=()):
    return adoption.inspect(ex,rows,book,set(),set(),MASTER)


def new_position(product='CARRYFORWARD'):
    ex = ae.AutoExecutor()
    result = inspect(ex,[broker(product)])
    assert result[2][0]['reason']=='adoption_verified'
    adoption.adopt(ex,result[3])
    pos = next(iter(ex.state.positions.values()))
    ex._positions_verified = True
    ex._celestial_verified.add(str(pos.signal_id))
    return ex,pos


def check_exits(ex, pos, price, time=AT):
    submitted = []
    def submit(**kwargs):
        submitted.append(kwargs)
        return angel_orders.OrderResult(True,'mock-order','mock',dry_run=False)
    with patch.object(ae,'now_ist',return_value=time), \
         patch.object(ae,'_poll_ltp',return_value={pos.token:price}), \
         patch.object(ae.exit_manager,'realized_pnl_today',return_value=0), \
         patch.object(angel_orders,'place_limit_order',side_effect=submit):
        ex.check_exits()
    return submitted


def main():
    with patch.object(adoption,'now_ist',return_value=AT), \
         patch.object(celestial,'read_config',return_value=dict(adopt_external_positions=True)), \
         patch.object(ae.audit,'log'), patch.object(alerts,'enqueue') as warning:
        ex,p = new_position()
        assert p.kind=='MANUAL_PROTECTED' and p.effective_stop==70 and p.product=='CARRYFORWARD'
        restored = next(r for r in storage.load_open_positions() if r['signal_id']==p.signal_id)
        assert restored['product']=='CARRYFORWARD' and restored['effective_stop']==70
        print('PASS carryforward adoption, stop and persistence')
        with patch.object(config,'in_no_auto_exit_window',return_value=True):
            sent = check_exits(ex,p,69)
        assert len(sent)==1 and sent[0]['transactiontype']=='SELL' and sent[0]['producttype']=='CARRYFORWARD'
        assert p.exit_reason=='manual_hard_stop_breached'
        print('PASS carryforward hard-stop SELL at 15:20')
        ex,p = new_position('INTRADAY')
        assert p.effective_stop==70
        sent = check_exits(ex,p,69)
        assert len(sent)==1 and sent[0]['producttype']=='INTRADAY'
        print('PASS intraday manual stop uses original product')
        ex = ae.AutoExecutor()
        assert len(inspect(ex,[broker()],[pending('different')])[3])==1
        assert not inspect(ex,[broker()],[pending('44597')])[3]
        print('PASS pending-order adoption blocks only matching contract')
        ex = ae.AutoExecutor()
        warning.reset_mock()
        for _ in range(2): inspect(ex,[broker(netqty='64')])
        assert warning.call_count==1 and 'contract_or_lot_mismatch' in warning.call_args.args[0]
        assert 'Place an SL in Angel One.' in warning.call_args.args[0]
        print('PASS one unprotected-position alert')
        ex,p = new_position()
        eod = AT.replace(minute=38)
        assert not check_exits(ex,p,100,eod)
        ledger=[dict(signal_id=p.signal_id,state='terminal',dry_run=0,order_id='x',side='BUY',filled=65)]
        assert owned_watchdog.owned_positions(ex.state.positions,ledger,[broker()],[],eod)==[]
        print('PASS carryforward survives EOD and watchdog even with a BUY ledger')
        ex,p = new_position('INTRADAY')
        p.kind='gamma_blast';p.structural_stop=p.effective_stop=70
        with patch.object(config,'in_no_auto_exit_window',return_value=True), \
             patch('kadima.celestial_exit.evaluate',return_value=None):
            assert not check_exits(ex,p,100)
        print('PASS existing KADIMA position untouched at 15:20 above stop')
        ex,p = new_position()
        assert not check_exits(ex,p,109) and p.effective_stop==70 and not p.trail_active
        assert not check_exits(ex,p,120) and p.effective_stop==110 and p.trail_active
        assert check_exits(ex,p,109)[0]['producttype']=='CARRYFORWARD'
        assert p.exit_reason=='manual_profit_trail_breached'
        print('PASS activation at 10%, retain 50% of peak gain')
        ex,p = new_position('NRML')
        assert check_exits(ex,p,69)[0]['producttype']=='NRML'
        print('PASS NRML product preserved')
        ex = ae.AutoExecutor();ex.celestial_master=MASTER
        with patch.object(storage,'load_open_positions',return_value=[]), \
             patch.object(execution_ledger,'refresh',return_value=False), \
             patch.object(execution_ledger,'pending',return_value=[]), \
             patch.object(ae,'fetch_broker_positions',return_value=[broker()]), \
             patch.object(angel_orders,'fetch_order_book',return_value=[]), \
             patch('kadima.external_reconciliation.reconcile',return_value=set()), \
             patch.object(celestial,'health'):
            ex.reconcile_on_startup()
        assert ex._reconcile_blocked and ex._positions_verified
        assert next(iter(ex.state.positions.values())).kind=='MANUAL_PROTECTED'
        print('PASS unrelated unresolved ledger blocks entries but permits manual protection')
        # Execute the actual route body without requiring the web framework
        # in the standalone offline Python environment.
        from kadima import trade_audit
        tree = ast.parse(Path('kadima/control_api.py').read_text())
        route = next(n for n in tree.body if isinstance(n,ast.FunctionDef)
                     and n.name=='celestial_top20')
        route.decorator_list=[]
        for arg in route.args.args:
            arg.annotation=None
        route.args.defaults=[ast.Constant(None)]
        namespace = dict(EXECUTOR=ex,_check_auth=lambda _:None,celestial=celestial,
                         angel_orders=angel_orders,trade_audit=trade_audit,
                         manual_protection=manual_protection,
                         now_ist=lambda:AT,celestial_config=lambda _:dict())
        exec(compile(ast.fix_missing_locations(ast.Module(body=[route],type_ignores=[])),
                     'kadima/control_api.py','exec'),namespace)
        with patch.object(celestial,'top20',return_value=[]):
            view = namespace['celestial_top20'](None)['positions'][0]
        assert view['source']=='MANUAL_PROTECTED' and view['effective_stop']==70
        assert view['product']=='CARRYFORWARD' and view['strategy']=='MANUAL'
        assert view['protection_status']=='quote_unavailable' and not view['protection_active']
        print('PASS top20 manual source, product, strategy and effective stop')
        stock = dict(exch_seg='NFO',instrumenttype='OPTSTK',expiry='27OCT2026',
                     lotsize='175',token='stock-option',symbol='TCS27OCT264000PE',name='TCS')
        MASTER.rows.append(stock)
        try:
            ex = ae.AutoExecutor()
            result = inspect(ex,[broker(symboltoken='stock-option',netqty='175',
                                        tradingsymbol=stock['symbol'])])
            adoption.adopt(ex,result[3])
            p = next(iter(ex.state.positions.values()))
            assert p.kind=='MANUAL_PROTECTED' and p.index=='TCS' and p.effective_stop==70
            print('PASS F&O stock long-option adoption')
        finally:
            MASTER.rows.pop()
        ex,p = new_position()
        p.effective_stop=95
        sent=check_exits(ex,p,94)
        assert sent and p.exit_reason=='manual_tightened_stop_breached'
        print('PASS tightened manual stop works before trail activation')
        ex,p = new_position()
        for invalid in (float('nan'),float('inf'),0,-1,True):
            assert not check_exits(ex,p,invalid)
        assert p.peak_price==100 and p.effective_stop==70
        print('PASS invalid quotes cannot poison peak/stop or submit an exit')
        # Warning state survives executor recreation and retries a full queue.
        key=manual_protection.warning_key('NFO','warning-contract','CARRYFORWARD')
        warning.reset_mock()
        assert manual_protection.warn_once(key,'WARN','missing_price')
        storage._local.conn.close()
        storage._local.conn=None
        assert not manual_protection.warn_once(key,'WARN','missing_price')
        assert warning.call_count==1
        manual_protection.clear_warning(key)
        with patch.object(alerts,'enqueue',return_value=False):
            assert not manual_protection.warn_once(key,'WARN','missing_price')
        assert manual_protection.warn_once(key,'WARN','missing_price')
        print('PASS persistent warning deduplication and full-queue retry')
        # Signal and position are one transaction; no orphan signal on failure.
        ex=ae.AutoExecutor();proposal=inspect(ex,[broker()])[3]
        count=storage.conn().execute('SELECT count(*) FROM signals').fetchone()[0]
        position_count=storage.conn().execute('SELECT count(*) FROM positions').fetchone()[0]
        real_upsert=storage.upsert_position
        def fail_after_write(*args,**kwargs):
            real_upsert(*args,**kwargs)
            raise RuntimeError('simulated write failure')
        with patch.object(storage,'upsert_position',side_effect=fail_after_write):
            try:adoption.adopt(ex,proposal)
            except RuntimeError:pass
            else:raise AssertionError('Expected adoption failure')
        assert not ex.state.positions
        assert storage.conn().execute('SELECT count(*) FROM signals').fetchone()[0]==count
        assert storage.conn().execute('SELECT count(*) FROM positions').fetchone()[0]==position_count
        print('PASS adoption failure rolls back signal and position together')
        # Manual protection works even when automatic Celestial rollout is off.
        ex=ae.AutoExecutor();ex.celestial_master=MASTER
        with patch.object(celestial,'enabled',return_value=False), \
             patch.object(storage,'load_open_positions',return_value=[]), \
             patch.object(execution_ledger,'refresh',return_value=True), \
             patch.object(execution_ledger,'pending',return_value=[]), \
             patch.object(ae,'fetch_broker_positions',return_value=[broker()]), \
             patch.object(angel_orders,'fetch_order_book',return_value=[]), \
             patch('kadima.external_reconciliation.reconcile',return_value=set()), \
             patch.object(celestial,'health'):
            ex.reconcile_on_startup()
            p=next(iter(ex.state.positions.values()))
            assert ex._manual_reconciliation and check_exits(ex,p,69)
        print('PASS manual protection independent of automatic rollout')
        # A broker-evidenced scale-in uses the correct manual 30% floor.
        from kadima import external_reconciliation as external
        ex,p=new_position();c=external.connection()
        with c:c.execute('DELETE FROM celestial_external_cursors WHERE signal_id=?',(p.signal_id,))
        clock=lambda:AT
        with patch.object(external,'now_ist',side_effect=clock):
            assert not external.reconcile(ex,[broker()],[])
            order=SimpleNamespace(order_id='external-buy',status='complete',filled_qty=65,avg_price=120,
                raw=dict(exchange='NFO',symboltoken='44597',producttype='CARRYFORWARD',transactiontype='BUY'))
            assert not external.reconcile(ex,[broker(netqty='130',buyavgprice='110')],[order])
        assert p.remaining_qty==130 and p.entry_price==110
        assert abs(p.structural_stop-77)<1e-9 and abs(p.effective_stop-77)<1e-9
        print('PASS manual scale-in keeps 30% policy instead of generic 5%')
        sell=SimpleNamespace(order_id='external-sale',status='complete',filled_qty=65,avg_price=115,
            raw=dict(exchange='NFO',symboltoken='44597',producttype='CARRYFORWARD',transactiontype='SELL'))
        with patch.object(external,'now_ist',side_effect=clock):
            assert not external.reconcile(ex,[broker(buyavgprice='110')],[order,sell])
        assert p.remaining_qty==65 and p.lots==1 and p.allocated_capital==7150
        assert abs(p.effective_stop-77)<1e-9
        print('PASS evidenced partial manual SELL updates quantity, lots and deployed basis without loosening stop')
        ex,p=new_position();p.effective_stop=95
        p.exit_reason='manual_exit_dashboard'
        assert manual_protection.evaluate(p,96)[3]=='manual_exit_dashboard'
        print('PASS queued exit remains binding after premium rebound')
        ex,p=new_position()
        assert check_exits(ex,p,69)
        order=dict(execution_ledger.connection().execute(
            'SELECT * FROM execution_orders WHERE signal_id=?',(p.signal_id,)).fetchone())
        execution_ledger.apply_fill(p,order,angel_orders.OrderStatus('mock-order','cancelled',0,None,{}))
        assert p.status=='open' and p.remaining_qty==65
        sent=check_exits(ex,p,110)
        assert sent and sent[0]['producttype']=='CARRYFORWARD'
        assert p.exit_reason=='manual_hard_stop_breached'
        print('PASS cancelled unfilled SELL retries binding exit with original product after rebound')
        ex,p=new_position();p.token='verification-warning'
        ex._celestial_verified.clear();warning.reset_mock()
        assert not check_exits(ex,p,69)
        replacement=ae.AutoExecutor();replacement.state.positions=ex.state.positions
        replacement._positions_verified=True
        assert not check_exits(replacement,p,69)
        assert warning.call_count==1
        assert manual_protection.status(ex,p,69)=='broker_unverified'
        print('PASS unverifiable position warns once across executor recovery and sends no SELL')
        ex,p=new_position()
        for ltp in (100,109,110,120,119,130,125,135):
            previous=p.effective_stop
            peak,stop,active,_=manual_protection.evaluate(p,ltp)
            assert stop>=previous
            p.peak_price,p.effective_stop,p.trail_active=peak,stop,active
        assert manual_protection.status(ex,p,140)=='monitoring'
        assert manual_protection.status(ex,p,100)=='exit_due'
        print('PASS stop never moves down and UI distinguishes monitoring from an exit due')
        ex,p=new_position()
        def audit_failure(event,**kwargs):
            if event=='manual_protection_evaluated':
                raise OSError('simulated telemetry failure')
        with patch.object(ae.audit,'log',side_effect=audit_failure):
            assert check_exits(ex,p,69)
        assert p.exit_reason=='manual_hard_stop_breached'
        print('PASS telemetry failure cannot suppress manual stop submission')
    print('ALL PASS')


if __name__=='__main__':
    main()
