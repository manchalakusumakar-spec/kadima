"""Authenticated stock-fast intent/status routes. GET never calls the broker."""
from typing import Optional
from fastapi import Header, HTTPException
from . import stock_fast


def register(control_api, worker):
    app = control_api.app

    @app.get('/api/kadima/stock-fast')
    def read(authorization: Optional[str] = Header(None)):
        control_api._check_auth(authorization)
        from . import celestial, fast_momentum, config
        now = stock_fast._now()
        cfg = stock_fast.controls()
        global_cfg = celestial.read_config()
        global_blocks = []
        if not cfg['live_enabled']: global_blocks.append('stock_fast_live_disabled')
        if not fast_momentum.live_requested(): global_blocks.append('fast_observation_only')
        if not celestial.enabled(): global_blocks.append('rollout_disabled')
        if not global_cfg['auto_trade_enabled']: global_blocks.append('auto_paused')
        if global_cfg['kill_latched']: global_blocks.append('kill_latched')
        if not config.ORDER_PLACEMENT_ENABLED: global_blocks.append('broker_orders_disabled')
        rows = []
        for original in list(worker.executor.fast_rows.values()):
            if original.get('asset_scope') != 'STOCK':
                continue
            row = dict(original)
            if not stock_fast._fresh(row.get('quote_at'), now):
                row.update(eligible=False, stage='STALE', blocked_reasons=['stock_fast_stale_snapshot'])
            if global_blocks:
                row['eligible'] = False
                row['blocked_reasons'] = list(dict.fromkeys(row.get('blocked_reasons', []) + global_blocks))
            rows.append(row)
        rows.sort(key=lambda r: (r.get('eligible', False), r.get('score', 0)), reverse=True)
        with worker.executor._lock:
            held = list(worker.executor.state.positions.values())
        positions = []
        c = stock_fast._connection()
        for pos in held:
            policy = c.execute('SELECT hold_minutes,session_exit FROM stock_fast_policies WHERE signal_id=?',
                               (pos.signal_id,)).fetchone()
            if policy is None:
                continue
            positions.append(dict(signal_id=pos.signal_id, underlying=pos.index, symbol=pos.symbol,
                status=pos.status, filled_owned_quantity=pos.remaining_qty, entry_price=pos.entry_price,
                effective_stop=pos.effective_stop, holding_minutes=policy[0], session_exit=policy[1],
                execution_stage=('PARTIAL_FILL' if pos.status == 'entry_pending' and pos.remaining_qty > 0
                                 else 'FILLED' if pos.status == 'open' and pos.remaining_qty > 0
                                 else 'PENDING_NOT_FILLED')))
        return dict(ts=now.isoformat(), controls=cfg,
            worker_alive=bool(worker.thread and worker.thread.is_alive()),
            fast_master_permission=fast_momentum.live_requested(),
            broker_order_permission=config.ORDER_PLACEMENT_ENABLED,
            celestial_rollout=celestial.enabled(), global_auto_requested=global_cfg['auto_trade_enabled'],
            kill_latched=global_cfg['kill_latched'], worker=celestial.health('stock_fast'),
            move_capture_worker=celestial.health('move_capture_execution'),
            move_capture_feed=celestial.health('move_capture_feed'),
            universe=celestial.health('stock_universe'), positions=positions,
            rows=rows[:20], total_observed_rows=len(rows), display_limit=20,
            warning='Live requested is not readiness, an order, a fill, or a profit guarantee.')

    @app.post('/api/kadima/stock-fast/config')
    def update(body: dict, authorization: Optional[str] = Header(None)):
        control_api._check_auth(authorization)
        if set(body) != {'patch', 'expected_revision'}:
            raise HTTPException(400, 'stock_fast_invalid_control_patch')
        try:
            result = stock_fast.update_controls(body['patch'], body['expected_revision'])
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        from . import audit
        audit.log('STOCK_FAST_CONTROLS_UPDATED', **result)
        return result
