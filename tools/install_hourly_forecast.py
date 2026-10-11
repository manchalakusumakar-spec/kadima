"""Install the hourly forecast into the LIVE KADIMA checkout by anchors, not line numbers.

The live files differ from GitHub main, so a git patch does not apply. This
script inserts each piece next to a stable anchor, refuses to touch anything
if an anchor is missing, backs up first, compiles everything, and restores
the backups automatically if compilation fails. Safe to run twice: it skips
files that already carry the HOURLY FORECAST v1 marker.

    python tools/install_hourly_forecast.py --check     # report only, change nothing
    python tools/install_hourly_forecast.py             # install

Nothing is loaded by the running engine until `sudo systemctl restart kadima`.
"""
import os
import py_compile
import re
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
K = os.path.join(ROOT, 'kadima')
MARK = 'HOURLY FORECAST v1'

CONFIG_BLOCK = "\n\n# HOURLY FORECAST v1\n# ---------------------------------------------------------------- hourly forecast panel\n# 2026-10-09, trader's decisions: NIFTY only to start (KADIMA_FORECAST_INDICES\n# env var, comma-separated, adds SENSEX/BANKNIFTY later), display only, plus\n# human-clicked CE/PE BUY buttons with a confirm step. Each click goes through\n# auto_executor.forecast_manual_buy(), which keeps every existing entry gate.\nFORECAST_MANUAL_TRADE_ENABLED = True\nFORECAST_MANUAL_MAX_LOTS = MAX_LOTS_PER_SIGNAL\nFORECAST_MANUAL_FALLBACK_SL_PCT = 0.30   # used only when no confirmed swing stop exists\n# Reserved for a possible future auto-trade mode from the forecast. Nothing reads it.\nFORECAST_AUTOTRADE_ENABLED = False\n"
CONTROL_BLOCK = "\n\n# HOURLY FORECAST v1\nfrom typing import Optional\nfrom fastapi import Body, Header, HTTPException\nfrom kadima import audit, config\nif '_control_result' not in globals():\n    def _control_result(result):\n        if not result or result.get('ok') is not True:\n            raise HTTPException(409, result or {'reason': 'no_execution_result'})\n        return result\n# ---------------------------------------------------------------- hourly forecast panel\n# Forecast reads are display-only. The one write is a human-clicked BUY that the\n# panel only sends after its own confirm step; it is routed through\n# auto_executor.forecast_manual_buy(), never straight to the broker.\nfrom datetime import date as _date\nfrom fastapi import Query\nfrom . import hourly_forecast\n\n\ndef _forecast_engine():\n    f = hourly_forecast.FORECASTER\n    if f is None or f.engine is None:\n        raise HTTPException(503, 'hourly_forecaster_not_running')\n    return f.engine\n\n\ndef _forecast_index(name):\n    name = (name or '').upper()\n    if name not in hourly_forecast.FORECAST_INDICES:\n        raise HTTPException(422, f'index_not_enabled_for_forecast:{name}')\n    return name\n\n\n@app.get('/api/forecast/hourly')\ndef forecast_hourly(index: str = Query('NIFTY'), day: Optional[str] = Query(None),\n                    authorization: Optional[str] = Header(None)):\n    _check_auth(authorization)\n    name = _forecast_index(index)\n    try:\n        d = _date.fromisoformat(day) if day else None\n    except ValueError:\n        raise HTTPException(422, 'day_must_be_YYYY-MM-DD')\n    engine = hourly_forecast.FORECASTER.engine if hourly_forecast.FORECASTER else None\n    out = hourly_forecast.view(name, d, engine=engine)\n    out['trade'] = dict(enabled=bool(config.FORECAST_MANUAL_TRADE_ENABLED and config.ORDER_PLACEMENT_ENABLED),\n                        max_lots=config.FORECAST_MANUAL_MAX_LOTS,\n                        strikes=hourly_forecast.strike_options(name, engine) if engine else\n                        dict(available=False, reason='engine_not_running'))\n    return out\n\n\n@app.get('/api/forecast/days')\ndef forecast_days(index: str = Query('NIFTY'), authorization: Optional[str] = Header(None)):\n    _check_auth(authorization)\n    return dict(index=_forecast_index(index), days=hourly_forecast.available_days(_forecast_index(index)))\n\n\n@app.post('/api/forecast/buy')\ndef forecast_buy(body: dict = Body(...), authorization: Optional[str] = Header(None)):\n    _check_auth(authorization)\n    if set(body) != {'index', 'direction', 'strike', 'lots'}:\n        raise HTTPException(422, 'index_direction_strike_lots_required')\n    name = _forecast_index(body['index'])\n    direction, strike, lots = body['direction'], body['strike'], body['lots']\n    if direction not in ('CE', 'PE') or type(lots) is not int or isinstance(strike, bool) \\\n            or not isinstance(strike, (int, float)):\n        raise HTTPException(422, 'direction_CE_PE_integer_lots_numeric_strike')\n    if EXECUTOR is None:\n        raise HTTPException(503, 'executor_not_registered')\n    engine = _forecast_engine()\n    # Only the panel's own choices: CE ATM/+1/+2, PE ATM/-1/-2 at the current spot.\n    if hourly_forecast.allowed_strike(name, engine, direction, float(strike)) is None:\n        raise HTTPException(409, 'strike_not_offered_at_current_spot_refresh_panel')\n    st = engine.states[name]\n    result = EXECUTOR.forecast_manual_buy(name, direction, float(strike), lots, st.chain, st.spot)\n    audit.log('forecast_panel_buy_request', index=name, direction=direction, strike=strike,\n              lots=lots, ok=result.get('ok'), reason=result.get('reason'))\n    return _control_result(result)\n"
EXECUTOR_BLOCK = '\n\n# HOURLY FORECAST v1 - manual buy from the forecast panel\ndef _hf_forecast_manual_buy(self, index_name: str, direction: str, strike: float, lots: int,\n                        chain, spot: float) -> dict:\n    """One human-clicked BUY of a strike the trader picked on the hourly-forecast panel.\n\n    The strike and lot count are the trader\'s choice; everything else is the\n    existing entry machinery: session gate, reconciliation, kill switch,\n    market hours, signal cutoff, daily loss limit, carry-forward and external\n    contract checks, portfolio cash/loss capacity, fresh LIMIT pricing, the\n    durable execution ledger, and the same software stop/trail monitoring as\n    every other position. Nothing here runs without a click.\n    """\n    import json\n    from . import market_session_gate, portfolio_capacity\n    if not getattr(config, \'FORECAST_MANUAL_TRADE_ENABLED\', False):\n        return self._reject(\'forecast_manual_trade_disabled\', index_name, direction)\n    if not ORDER_PLACEMENT_ENABLED:\n        return self._reject(\'broker_orders_disabled\', index_name, direction)\n    if direction not in (\'CE\', \'PE\'):\n        return self._reject(\'direction_must_be_CE_or_PE\', index_name, str(direction))\n    max_lots = getattr(config, \'FORECAST_MANUAL_MAX_LOTS\', MAX_LOTS_PER_SIGNAL)\n    if isinstance(lots, bool) or not isinstance(lots, int) or not 1 <= lots <= max_lots:\n        return self._reject(f\'lots_must_be_1_to_{max_lots}\', index_name, direction)\n    session_block = market_session_gate.entry_block(chain.index.opt_exchange)\n    if session_block:\n        return self._reject(session_block, index_name, direction)\n    if self._reconcile_blocked:\n        return self._reject(\'startup_reconcile_not_complete\', index_name, direction)\n    if killswitch.engaged():\n        return self._reject(\'killswitch_engaged\', index_name, direction)\n    if not in_market_hours():\n        return self._reject(\'outside_market_hours\', index_name, direction)\n    if past_signal_cutoff():\n        return self._reject(\'past_signal_cutoff\', index_name, direction)\n    if exit_manager.realized_pnl_today() <= -DAILY_LOSS_LIMIT_INR:\n        return self._reject(\'daily_loss_limit_breached\', index_name, direction)\n    if index_name in getattr(self, \'_manual_carryforward_underlyings\', set()):\n        return self._reject(\'manual_carryforward_same_underlying\', index_name, direction)\n    block = portfolio_capacity.entry_block(self.state.positions, execution_ledger.pending())\n    if block:\n        return self._reject(block, index_name, direction)\n    clock = time.monotonic()\n    if not 0 <= clock - getattr(chain, \'_spot_received_at\', 0) <= 30:\n        return self._reject(\'underlying_quote_stale\', index_name, direction)\n    if not 0 <= clock - getattr(chain, \'_last_poll\', 0) <= 30:\n        return self._reject(\'option_chain_stale\', index_name, direction)\n\n    quote = next((q for q in chain.quotes.values()\n                  if q.strike == strike and q.opt_type == direction), None)\n    if quote is None:\n        return self._reject(\'strike_not_in_tracked_ladder\', index_name, direction)\n    try:\n        valid = {str(c[\'token\']) for c in chain.scrips.strikes_around(\n            chain.index, chain.atm, config.STRIKES_EACH_SIDE)}\n    except (AttributeError, TypeError, KeyError):\n        return self._reject(\'contract_expiry_unverifiable\', index_name, direction)\n    if str(quote.token) not in valid:\n        return self._reject(\'contract_expiry_unverifiable\', index_name, direction)\n    if (chain.index.opt_exchange, str(quote.token)) in getattr(self, \'_external_contracts\', set()):\n        return self._reject(\'contract_held_outside_kadima\', index_name, direction)\n    lot_size = get_lot_size(quote.symbol, quote.token)\n    if lot_size is None:\n        return self._reject(\'lot_size_unresolvable_at_order_time\', index_name, direction)\n    pricing_ltp = _poll_ltp(chain.index.opt_exchange, [quote.token]).get(quote.token)\n    if pricing_ltp is None:\n        return self._reject(\'fresh_entry_quote_unavailable\', index_name, direction)\n    limit_price = round(pricing_ltp * (1 + LIMIT_ORDER_BUFFER_PCT / 100.0), 1)\n\n    # Stop: the same structure-based premium stop as engine entries when a\n    # confirmed swing exists, otherwise a flat fallback below the fill price.\n    swing_level, stop = None, None\n    structure = self._structure(chain, direction, spot)\n    if structure is not None:\n        swing_level, days = structure\n        stop = trade_policy.premium_stop(spot, swing_level, quote.strike, pricing_ltp,\n                                         days, direction)\n    if stop is None:\n        stop = round(pricing_ltp * (1 - getattr(config, \'FORECAST_MANUAL_FALLBACK_SL_PCT\', 0.30)), 2)\n    if pricing_ltp <= stop:\n        return self._reject(\'fresh_quote_invalidates_stop\', index_name, direction)\n\n    capacity = portfolio_capacity.affordable_lots(\n        self.state.positions, execution_ledger.pending(),\n        exchange=chain.index.opt_exchange, token=quote.token,\n        limit_price=limit_price, stop=stop, lot_size=lot_size, requested_lots=lots,\n        broker_funds=capital_guard.broker_available_funds(),\n        capital_buffer_pct=capital_guard.CAPITAL_SAFETY_BUFFER_PCT,\n        daily_limit=DAILY_LOSS_LIMIT_INR, realized_pnl=exit_manager.realized_pnl_today(),\n        cost_reserve=trade_policy.COST_RESERVE, exit_buffer=trade_policy.EXIT_BUFFER)\n    if capacity.lots < lots:\n        return self._reject(f\'only_{capacity.lots}_lots_affordable_{capacity.reason}\',\n                            index_name, direction)\n    if killswitch.engaged() or not in_market_hours() or past_signal_cutoff():\n        return self._reject(\'entry_gate_changed_before_submission\', index_name, direction)\n    session_block = market_session_gate.entry_block(chain.index.opt_exchange)\n    if session_block:\n        return self._reject(session_block, index_name, direction)\n\n    quantity = lots * lot_size\n    signal_id = storage.insert_signal(index_name, \'MANUAL\', 0.0, direction, json.dumps(dict(\n        source=\'hourly_forecast_panel\', strike=strike, lots=lots, limit_price=limit_price,\n        stop=stop, spot=spot)), kind=\'forecast_manual\')\n    pos = Position(\n        signal_id=signal_id, kind=\'forecast_manual\', index=index_name, direction=direction,\n        symbol=quote.symbol, token=quote.token, exchange=chain.index.opt_exchange,\n        entry_price=0.0, entry_ts=now_ist(), lots=lots, lot_size=lot_size,\n        remaining_qty=0, status=\'entry_pending\', swing_level=swing_level,\n        structural_stop=stop, effective_stop=stop, allocated_capital=quantity * limit_price)\n    with self._lock:\n        self.state.positions[str(signal_id)] = pos\n    result = execution_ledger.submit(pos, \'BUY\', quantity, limit_price)\n    self._last_reconcile = 0.0\n    if pos.status == \'closed\':\n        self.state.positions.pop(str(signal_id), None)\n    result = dict(result, signal_id=signal_id, symbol=quote.symbol, quantity=quantity,\n                  limit_price=limit_price, stop=stop)\n    audit.log(\'forecast_manual_entry_result\', index=index_name, direction=direction,\n              strike=strike, lots=lots, **result)\n    self.entry_diagnostics.append(dict(result, ts=now_ist().isoformat(), index=index_name,\n        direction=direction, stage=\'FORECAST_MANUAL_SUBMISSION\'))\n    alerts.enqueue(f"MANUAL forecast-panel BUY {quote.symbol} x{quantity} @ {limit_price}: "\n                   f"{result.get(\'reason\')} | signal #{signal_id}", key=f\'entry:{signal_id}\')\n    return result\n\n\n\nAutoExecutor.forecast_manual_buy = (serialized(_hf_forecast_manual_buy)\n                                     if \'serialized\' in globals() else _hf_forecast_manual_buy)\n\n\n'
ENGINE_INIT = '        # HOURLY FORECAST v1 - hourly path forecast vs actual (display only, never trades).\n        from . import hourly_forecast\n        self.hourly_forecaster = hourly_forecast.HourlyForecaster(self)\n        hourly_forecast.FORECASTER = self.hourly_forecaster\n'
ENGINE_START = '        self.hourly_forecaster.start()   # HOURLY FORECAST v1\n'
ENGINE_STOP = '        self.hourly_forecaster.stop()    # HOURLY FORECAST v1\n'


def read(name):
    with open(os.path.join(K, name)) as f:
        return f.read()


def line_end(text, idx):
    """End of the statement that starts on the line containing idx (handles wrapped calls)."""
    depth, i = 0, text.rfind('\n', 0, idx) + 1
    while i < len(text):
        ch = text[i]
        if ch in '([{': depth += 1
        elif ch in ')]}': depth -= 1
        elif ch == '\n' and depth <= 0:
            return i + 1
        i += 1
    return len(text)


def plan():
    edits, problems = {}, []
    # config.py and control_api.py: append at the end.
    for name, block in (('config.py', CONFIG_BLOCK), ('control_api.py', CONTROL_BLOCK)):
        t = read(name)
        if MARK in t:
            print(f'  {name}: already installed, skipping')
            continue
        edits[name] = t.rstrip('\n') + '\n' + block
    t = read('control_api.py')
    for need in ('_check_auth', 'EXECUTOR', 'app = FastAPI'):
        if need not in t:
            problems.append(f'control_api.py has no `{need}`')
    if 'MAX_LOTS_PER_SIGNAL' not in read('config.py'):
        problems.append('config.py has no MAX_LOTS_PER_SIGNAL')

    # auto_executor.py: insert just before the module-level EXECUTOR singleton.
    t = read('auto_executor.py')
    if MARK in t:
        print('  auto_executor.py: already installed, skipping')
    else:
        m = list(re.finditer(r'^EXECUTOR\s*=\s*AutoExecutor\(\)', t, re.M))
        if len(m) != 1:
            problems.append(f'auto_executor.py: expected one `EXECUTOR = AutoExecutor()` line, found {len(m)}')
        else:
            edits['auto_executor.py'] = t[:m[0].start()] + EXECUTOR_BLOCK.lstrip('\n') + t[m[0].start():]
        for need in ('def _structure', 'def _reject', '_poll_ltp', 'get_lot_size', 'LIMIT_ORDER_BUFFER_PCT',
                     'execution_ledger', 'trade_policy', 'capital_guard', 'class Position'):
            if need not in t:
                problems.append(f'auto_executor.py has no `{need}`')

    # engine.py: three anchored inserts in Engine.__init__/start/stop.
    t = read('engine.py')
    if MARK in t:
        print('  engine.py: already installed, skipping')
    else:
        anchors = (
            (r'^ {8}self\.forecaster\s*=\s*ForecastObserver\(', ENGINE_INIT, 'self.forecaster = ForecastObserver(...) in __init__'),
            (r'^ {8}self\.forecaster\.start\(\)', ENGINE_START, 'self.forecaster.start() in start()'),
            (r'^ {8}self\.forecaster\.stop\(\)', ENGINE_STOP, 'self.forecaster.stop() in stop()'),
        )
        spots = []
        for pat, block, label in anchors:
            m = list(re.finditer(pat, t, re.M))
            if len(m) != 1:
                problems.append(f'engine.py: anchor `{label}` found {len(m)} times (need exactly 1)')
            else:
                spots.append((line_end(t, m[0].start()), block))
        if len(spots) == 3:
            for pos, block in sorted(spots, reverse=True):
                t = t[:pos] + block + t[pos:]
            edits['engine.py'] = t
    for name in ('hourly_forecast.py', 'hourly_forecast_model.py'):
        if not os.path.exists(os.path.join(K, name)):
            problems.append(f'kadima/{name} missing - run the git checkout step first')
    return edits, problems


def main():
    check = '--check' in sys.argv
    edits, problems = plan()
    if problems:
        print('STOPPED - nothing changed:')
        for p in problems:
            print('  -', p)
        return 1
    if not edits:
        print('ALREADY_INSTALLED - nothing to do')
        return 0
    if check:
        print('CHECK_OK - would edit:', ', '.join(sorted(edits)) or 'nothing (already installed)')
        return 0
    stamp = time.strftime('%Y%m%d-%H%M%S')
    bdir = os.path.expanduser(f'~/backup-hf-{stamp}')
    os.makedirs(bdir, exist_ok=True)
    for name in edits:
        shutil.copy2(os.path.join(K, name), os.path.join(bdir, name))
    for name, text in edits.items():
        with open(os.path.join(K, name), 'w') as f:
            f.write(text)
    try:
        for name in sorted(set(edits) | {'hourly_forecast.py', 'hourly_forecast_model.py'}):
            py_compile.compile(os.path.join(K, name), doraise=True)
    except py_compile.PyCompileError as exc:
        for name in edits:
            shutil.copy2(os.path.join(bdir, name), os.path.join(K, name))
        print('COMPILE FAILED - all files restored from', bdir)
        print(exc)
        return 1
    print('INSTALL_OK - edited:', ', '.join(sorted(edits)))
    print('Backups in', bdir)
    print('Rollback: cp', bdir + '/*.py', K + '/ && sudo systemctl restart kadima')
    return 0


if __name__ == '__main__':
    sys.exit(main())
