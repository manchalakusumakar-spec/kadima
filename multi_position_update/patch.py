"""Exact additive patch for the reviewed, session-guarded executor.

The repository baseline remains a source fixture for prior pinned installers.
This deployable overlay removes the single-position check WITHOUT removing the
already-installed calendar/permit guards. Only the specified anchors may change.
"""
import ast

SINGLE_LEGACY = '''        with self._lock:
            open_count = len(self.state.positions)
        if open_count >= min(1, MAX_CONCURRENT_POSITIONS):
            return self._reject("max_concurrent_positions", index_name, direction)
'''
SINGLE = SINGLE_LEGACY  # compatibility for existing offline fixtures
SINGLE_GAP = '''        with self._lock:
            open_count = len(self.state.positions)
            same_underlying = any(p.index == index_name and p.status != 'closed'
                                  for p in self.state.positions.values())
        if same_underlying:
            return self._reject("same_underlying_position_exists", index_name, direction)
        if open_count >= min(1, MAX_CONCURRENT_POSITIONS):
            return self._reject("max_concurrent_positions", index_name, direction)
'''
MULTI = '''        # KADIMA CAPITAL-BASED MULTI-POSITION v1
        from . import portfolio_capacity
        portfolio_block = portfolio_capacity.entry_block(
            self.state.positions, execution_ledger.pending())
        if portfolio_block:
            return self._reject(portfolio_block, index_name, direction)
'''
MULTI_GAP = '''        with self._lock:
            same_underlying = any(
                getattr(p, 'index', None) == index_name
                and getattr(p, 'status', None) != 'closed'
                and (kind == 'gap_and_go' or getattr(p, 'kind', None) == 'gap_and_go')
                for p in self.state.positions.values())
        if same_underlying:
            return self._reject("same_underlying_position_exists", index_name, direction)
        # KADIMA CAPITAL-BASED MULTI-POSITION v1
        from . import portfolio_capacity
        portfolio_block = portfolio_capacity.entry_block(
            self.state.positions, execution_ledger.pending())
        if portfolio_block:
            return self._reject(portfolio_block, index_name, direction)
'''
BEFORE_CLOCK = '''        if allowed_lots < 1 or pricing_ltp <= stop:
            return self._reject('fresh_quote_exceeds_risk_or_invalidates_stop', index_name, direction)
        clock = time.monotonic()
'''
AFTER_CLOCK = '''        if allowed_lots < 1 or pricing_ltp <= stop:
            return self._reject('fresh_quote_exceeds_risk_or_invalidates_stop', index_name, direction)
        # Final LIMIT-price sizing uses all confirmed open exposure, not a slot count.
        capacity = portfolio_capacity.affordable_lots(
            self.state.positions, execution_ledger.pending(),
            exchange=chain.index.opt_exchange, token=chosen_quote.token,
            limit_price=limit_price, stop=stop, lot_size=lot_size,
            requested_lots=allowed_lots,
            broker_funds=capital_guard.broker_available_funds(),
            capital_buffer_pct=capital_guard.CAPITAL_SAFETY_BUFFER_PCT,
            daily_limit=DAILY_LOSS_LIMIT_INR,
            realized_pnl=exit_manager.realized_pnl_today(),
            cost_reserve=trade_policy.COST_RESERVE, exit_buffer=trade_policy.EXIT_BUFFER)
        if capacity.lots < 1:
            return self._reject(capacity.reason, index_name, direction)
        allowed_lots = capacity.lots
        audit.log('portfolio_capacity_checked', index=index_name, direction=direction,
                  open_positions=capacity.open_positions, lots=allowed_lots,
                  cash_committed=capacity.cash_committed,
                  stop_risk_committed=capacity.stop_risk_committed,
                  reason=capacity.reason)
        clock = time.monotonic()
'''
POLL = '''    if not tokens:
        return {}
    mode = 'FULL' if celestial.enabled() else 'LTP'
'''
BATCHED = '''    if not tokens:
        return {}
    tokens = list(dict.fromkeys(tokens))
    if len(tokens) > 50:
        # Preserve the existing shared quote limiter on each bounded batch.
        result = {}
        for offset in range(0, len(tokens), 50):
            result.update(_poll_ltp(exchange, tokens[offset:offset + 50]))
        return result
    mode = 'FULL' if celestial.enabled() else 'LTP'
'''


def patch_executor(raw):
    text = raw.decode()
    if text.count('market_session_gate.entry_block') != 2:
        raise ValueError('session_entry_guards_required')
    single = SINGLE_GAP if text.count(SINGLE_GAP) == 1 else SINGLE_LEGACY
    multi = MULTI_GAP if single == SINGLE_GAP else MULTI
    for old, new in ((single, multi), (BEFORE_CLOCK, AFTER_CLOCK), (POLL, BATCHED)):
        if text.count(old) != 1:
            raise ValueError('multi_position_patch_anchor_changed')
        text = text.replace(old, new, 1)
    text = text.replace('DAILY_LOSS_LIMIT_INR, IST, MAX_CONCURRENT_POSITIONS,',
                        'DAILY_LOSS_LIMIT_INR, IST,', 1)
    compile(text, 'multi_position_auto_executor.py', 'exec')
    if 'min(1, MAX_CONCURRENT_POSITIONS)' in text or text.count('market_session_gate.entry_block') != 2:
        raise ValueError('multi_position_patch_verification_failed')
    before, after = ast.parse(raw), ast.parse(text)
    def untouched(tree):
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == 'config':
                node.names = [n for n in node.names if n.name != 'MAX_CONCURRENT_POSITIONS']
            if isinstance(node, ast.FunctionDef) and node.name in ('evaluate_and_enter', '_poll_ltp'):
                node.body = [ast.Pass()]
        return ast.dump(tree, include_attributes=False)
    if untouched(before) != untouched(after):
        raise ValueError('unexpected_executor_scope_change')
    return text.encode()
