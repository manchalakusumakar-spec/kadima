#!/usr/bin/env python3
"""Stored-data day replay. No broker calls; source databases always mode=ro.

Only confirmed sampled fills and fully reconstructible exits contribute P&L.
Missing evidence is reported rather than silently treating a gate as passed.
"""
import argparse
from collections import Counter
from contextlib import ExitStack
import csv
from datetime import date, datetime, time, timedelta
import json
import math
from pathlib import Path
import socket
import sys
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kadima import audit, celestial, config, fast_momentum, portfolio_capacity, trade_policy
from kadima import auto_executor, capital_guard, market_session_gate, storage
from kadima.session import SESSION
from tools.replay_scalp_gates import open_readonly, price_window

FIELDS = ('status', 'time_in', 'time_out', 'contract', 'strike', 'direction', 'lane', 'score',
          'lots', 'lot_size', 'entry', 'exit', 'exit_reason', 'gross_pnl', 'costs', 'net_pnl',
          'capital_after', 'decision_time', 'raw_limit', 'limit', 'gate_blocks', 'reason',
          'fill_delay_seconds', 'next_quote_delay_seconds', 'evidence_gaps', 't1_first_60m',
          'best_60m_pct', 'worst_60m_pct')


def forbidden(*args, **kwargs):
    raise RuntimeError('Replay forbids broker/network calls and live storage access')


def in_session(at):
    return time(9, 15) <= at.time().replace(tzinfo=None) < time(15, 40)


def load_candidates(connection, audit_path, day):
    candidates, seen, counts, gamma_max = [], set(), Counter(), {}
    rows = []
    for line in audit_path.open():
        try:
            record = json.loads(line)
        except ValueError:
            counts['invalid_audit_lines'] += 1
            continue
        rows.append(record)
        event = record.get('event', '')
        counts[event] += 1
        name = record.get('underlying') or record.get('index') or record.get('name')
        if name not in config.GAMMA_INDICES:
            continue
        if event in ('gamma_score', 'gamma_tier_change'):
            gamma_max[name] = max(gamma_max.get(name, 0), record.get('score', 0))
        lane = None
        if event == 'FAST_CANDIDATE' and record.get('stage') == 'SETUP_READY':
            lane = 'scalp'
        elif event == 'CONFIRMATION_GATE' and record.get('passed') is True:
            lane = 'shield'
        elif event == 'tier_change' and record.get('tier') == 'CONFIRMED':
            lane = 'shield'
        elif event in ('gamma_score', 'gamma_tier_change') and record.get('tier') == 'CONFIRMED':
            lane = 'gamma'
        elif record.get('eligible') is True and any(x in event.lower() for x in ('celestial', 'gamma')):
            lane = 'gamma'
        if lane:
            candidate = record | {'underlying': name, 'lane': lane}
            key = (lane, name, record.get('direction'), record.get('episode') or record['ts'])
            if key not in seen:
                seen.add(key)
                candidates.append(candidate)
    # Archived suggestions preserve original candidate identity, not live API snapshots.
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'trade_audit_plans' in tables:
        for record in connection.execute('SELECT * FROM trade_audit_plans WHERE day=?', (day,)):
            record = dict(record)
            if record['underlying'] not in config.GAMMA_INDICES:
                continue
            if record['stage'] not in ('SETUP_READY', 'READY', 'CONFIRMED'):
                continue
            lane = 'scalp' if record['strategy'] == 'SCALP' else 'gamma'
            key = (lane, record['underlying'], record['direction'], record['episode'] or record['suggested_at'])
            if key in seen:
                continue
            seen.add(key)
            plan = json.loads(record['plan'])
            counts['additional_archived_suggestions'] += 1
            candidates.append(plan | dict(underlying=record['underlying'], direction=record['direction'],
                ts=record['suggested_at'], token=record['token'], symbol=record['symbol'],
                exchange=record['exchange'], score=record['score'], lane=lane,
                blocked_reasons=json.loads(record['blockers']), stage=record['stage']))
    # A current, replaced Top 20 table is not historical candidate coverage.
    if 'celestial_candidates' in tables:
        counts['current_celestial_rows'] = connection.execute('SELECT COUNT(*) FROM celestial_candidates').fetchone()[0]
        counts['current_celestial_index_rows'] = sum(
            json.loads(r[0]).get('underlying') in config.GAMMA_INDICES
            for r in connection.execute('SELECT payload FROM celestial_candidates'))
    start, end = day + 'T00:00:00+05:30', (date.fromisoformat(day) + timedelta(days=1)).isoformat() + 'T00:00:00+05:30'
    for signal in connection.execute('SELECT * FROM signals WHERE ts>=? AND ts<? ORDER BY ts', (start, end)):
        signal = dict(signal)
        if signal['index_name'] not in config.GAMMA_INDICES or signal['tier'] != 'CONFIRMED':
            continue
        lane = 'shield' if signal['kind'] in ('bull_shield', 'bear_shield') else 'gamma'
        payload = json.loads(signal['payload'])
        candidate = payload | dict(ts=signal['ts'], underlying=signal['index_name'],
            direction=signal['direction'], score=signal['score'], lane=lane, signal_id=signal['id'])
        key = (lane, signal['index_name'], signal['direction'], signal['ts'])
        if key not in seen:
            seen.add(key)
            candidates.append(candidate)
    candidates = [r for r in candidates if in_session(datetime.fromisoformat(r['ts']).astimezone(config.IST))]
    return sorted(candidates, key=lambda r: r['ts']), counts, gamma_max, rows


def recorded_quotes(connection, row, start, end):
    return [dict(r) for r in connection.execute(
        'SELECT * FROM oi_snapshots WHERE index_name=? AND ts>=? AND ts<=? '
        'AND token=? AND symbol=? AND ltp>0 ORDER BY ts,id',
        (row['underlying'], start.isoformat(), end.isoformat(), str(row['token']), row['symbol']))]


def lookup_lot(cache, symbol, token):
    if cache.get('sym', {}).get(str(token)) != symbol:
        return None
    lot = cache.get('lot', {}).get(str(token))
    return lot if type(lot) is int and lot > 0 else None


def replay_gate(row, at, controls, flags):
    """Real current gate, with frozen replay inputs instead of live DB calls."""
    news_blocked = any(r in row.get('blocked_reasons', []) for r in ('news_risk_block', 'fresh_catalyst_risk_block'))
    health = dict(ok=True, ts=at.isoformat(), covered_symbols=[])
    impact = dict(news_available=False, news_blocked=news_blocked, news_score=0, news_sources=[])
    replay_config = controls | {'auto_trade_enabled': True, 'kill_latched': False}
    with patch.dict('os.environ', flags), patch.object(celestial, 'read_config', return_value=replay_config), \
            patch.object(celestial, 'health', return_value=health), \
            patch.object(celestial, 'news_impact', return_value=impact):
        if row['lane'] == 'scalp':
            return fast_momentum.live_blocks(row['underlying'], row['direction'], at, row['score'])
        if row.get('id') and all(k in row for k in ('last_updated', 'premium_move_pct', 'gamma_score')):
            evaluated = celestial.ranked_candidates(at, candidates=[row])
            return evaluated[0]['blocked_reasons'] if evaluated else ['celestial_candidate_below_display_floor']
        if flags.get('KADIMA_CELESTIAL_ENABLED') == '1':
            return ['legacy_entry_disabled_in_celestial_mode', 'historical_top20_candidate_evidence_missing']
        return ['historical_entry_evidence_missing']


def size_trade(row, limit, lot_size, capital, positions, realized):
    if realized <= -config.DAILY_LOSS_LIMIT_INR:
        return 0, 'daily_loss_limit_breached'
    if len(positions) >= config.MAX_CONCURRENT_POSITIONS:
        return 0, 'skipped - concurrency'
    cash_lots = math.floor(capital / (limit * lot_size * 1.01))
    if cash_lots < 1:
        return 0, 'skipped - capital'
    if not trade_policy.positive(row.get('stop')):
        return 0, 'recorded_structural_stop_missing'
    allocated = min(capital, 30000) if row['lane'] in ('scalp', 'shield') else min(capital, 62000)
    requested = trade_policy.lots_within_allocation(allocated, limit, row['stop'], lot_size,
                                                   min(cash_lots, config.MAX_LOTS_PER_SIGNAL))
    if requested < 1:
        return 0, 'skipped - structural risk allocation'
    capacity = portfolio_capacity.affordable_lots(positions, [], exchange=row['exchange'],
        token=str(row['token']), limit_price=limit, stop=row['stop'], lot_size=lot_size,
        requested_lots=requested, broker_funds=capital,
        capital_buffer_pct=capital_guard.CAPITAL_SAFETY_BUFFER_PCT,
        daily_limit=config.DAILY_LOSS_LIMIT_INR, realized_pnl=realized,
        cost_reserve=trade_policy.COST_RESERVE, exit_buffer=trade_policy.EXIT_BUFFER)
    return capacity.lots, capacity.reason


def protective_exit(position, quote, realized):
    """Pure portions of auto_executor.check_exits, in the same priority order."""
    at, ltp = datetime.fromisoformat(quote['ts']), quote['ltp']
    position.peak_price = max(position.peak_price, ltp)
    position.effective_stop, active = trade_policy.ratchet(position.entry_price,
        position.peak_price, position.effective_stop, position.structural_stop)
    position.effective_stop, active = celestial.trail_stop(position.entry_price,
        position.peak_price, position.effective_stop, position.structural_stop)
    position.trail_active = position.trail_active or active
    if realized <= -config.DAILY_LOSS_LIMIT_INR:
        return 'daily_loss_limit_breached'
    if (at.hour, at.minute) >= auto_executor.EOD_FORCE_CLOSE_HHMM:
        return 'eod_force_close'
    reason = fast_momentum.timeout_reason(position, at)
    if reason:
        return reason
    if ltp <= position.structural_stop:
        return 'structural_stop_breached'
    if ltp <= position.effective_stop:
        return 'dynamic_profit_trail_breached'
    return None


def markdown_table(rows, fields):
    lines = ['| ' + ' | '.join(fields) + ' |', '| ' + ' | '.join('---' for _ in fields) + ' |']
    for row in rows:
        values = [str(row.get(k, '')).replace('|', '/').replace('\n', ' ') for k in fields]
        lines.append('| ' + ' | '.join(values) + ' |')
    return '\n'.join(lines)


def run_replay(connection, day, capital, audit_path, flags, lot_cache):
    candidates, counts, gamma_max, audit_rows = load_candidates(connection, audit_path, day)
    record = connection.execute('SELECT payload FROM celestial_config WHERE id=1').fetchone()
    controls = json.loads(record[0]) if record else celestial.DEFAULTS.copy()
    initial, realized, drawdown = capital, 0.0, 0.0
    results, positions = [], {}
    gaps = [
        'oi_snapshots has no bid/ask fields. LTP is the requested fill proxy; an LTP is not proof of an executable ask/bid.',
        'A fill requires a new stored sample strictly after decision and no later than 30 seconds. The decision quote itself is not a subsequent fill.',
        'Historical broker reconciliation, session-lease monotonic timestamps, RMS equity, spread and external positions were not fully archived. They are not silently marked passed.',
        'Market-news risk at each decision is reconstructed from that candidate\'s recorded blockers; expired full-news history is unavailable.',
        'Current switches/thresholds are used with only auto_trade_enabled=True and kill_latched=False overridden in memory.',
        'Scalp LIMIT uses the engine\'s extra entry cap: min(buffered LTP, floor(recorded ask * 1.005 * 20)/20). Raw buffered LIMIT is also shown.',
        'MAX_CONCURRENT_POSITIONS is enforced at the configured value, although the current live executor uses portfolio_capacity rather than a count cap.',
        'No intrapoll extrema, synthetic quotes, stale EOD fills, or missing-ladder prices are interpolated.',
        'A fully exact fill/exit is not claimed when quality gates or historical Celestial exit-health evidence are missing.',
    ]
    for row in candidates:
        at = datetime.fromisoformat(row['ts']).astimezone(config.IST)
        out = dict(status='skipped', decision_time=at.isoformat(), contract=row.get('symbol', ''),
                   strike=row.get('strike', row.get('best_strike', '')), direction=row['direction'],
                   lane=row['lane'], score=row.get('score', ''), capital_after=round(capital, 2))
        blockers = replay_gate(row, at, controls, flags)
        if not config.ORDER_PLACEMENT_ENABLED:
            blockers.append('broker_orders_disabled')
        if flags.get('KADIMA_SESSION_AUTOTRADE') == '1':
            try:
                schedule = market_session_gate.calendar_reason(
                    market_session_gate.read_json(market_session_gate.POLICY), at, entries=True)
                if schedule:
                    blockers.append(schedule)
            except (OSError, ValueError, KeyError, TypeError):
                blockers.append('session_schedule_unavailable')
        out['gate_blocks'] = '; '.join(blockers)
        if blockers:
            out['reason'] = 'skipped - gate: ' + '; '.join(blockers)
            results.append(out)
            continue
        if not all(row.get(k) for k in ('token', 'symbol', 'exchange')):
            out['reason'] = 'contract identity missing from historical event'
            results.append(out)
            continue
        quotes = recorded_quotes(connection, row, at - timedelta(seconds=30), at.replace(hour=15, minute=40, second=0, microsecond=0))
        before = [q for q in quotes if datetime.fromisoformat(q['ts']) <= at]
        after = [q for q in quotes if datetime.fromisoformat(q['ts']) > at]
        if not before:
            out['reason'] = 'recorded decision LTP missing or older than 30 seconds'
            results.append(out)
            continue
        decision = before[-1]
        raw_limit = round(decision['ltp'] * (1 + auto_executor.LIMIT_ORDER_BUFFER_PCT / 100), 1)
        limit = raw_limit
        if row['lane'] == 'scalp' and trade_policy.positive(row.get('entry')):
            limit = min(limit, math.floor(row['entry'] * 1.005 * 20) / 20)
        out.update(raw_limit=raw_limit, limit=limit)
        if after:
            out['next_quote_delay_seconds'] = round((datetime.fromisoformat(after[0]['ts']) - at).total_seconds(), 3)
        lot = lookup_lot(lot_cache, row['symbol'], row['token'])
        if lot is None:
            out['reason'] = 'lot size unavailable in verified local scrip cache'
            results.append(out)
            continue
        out['lot_size'] = lot
        lots, sizing_reason = size_trade(row, limit, lot, capital, positions, realized)
        out['lots'] = lots
        if lots < 1:
            out['reason'] = sizing_reason
            results.append(out)
            continue
        outcome = price_window(connection, row, at)
        out.update(t1_first_60m=outcome['outcome'], best_60m_pct=round(outcome['best'], 3) if outcome['best'] is not None else '',
                   worst_60m_pct=round(outcome['worst'], 3) if outcome['worst'] is not None else '')
        fill = next((q for q in after if (datetime.fromisoformat(q['ts']) - at).total_seconds() <= 30
                     and (q.get('ask') or q['ltp']) <= limit), None)
        if fill is None:
            out['reason'] = ('skipped - no fill: no stored quote within 30 seconds' if not any(
                (datetime.fromisoformat(q['ts']) - at).total_seconds() <= 30 for q in after)
                else 'skipped - no fill: subsequent quotes exceed LIMIT')
            out['evidence_gaps'] = 'bid/ask; historical session lease/reconciliation/RMS/exit health'
            results.append(out)
            continue
        # Do not fabricate a complete live strategy when downstream quality/
        # exit evidence was not archived. Report this filled-price opportunity
        # separately rather than inventing a realised cash amount.
        out.update(status='unreplayable', time_in=fill['ts'], entry=fill.get('ask') or fill['ltp'],
                   fill_delay_seconds=round((datetime.fromisoformat(fill['ts']) - at).total_seconds(), 3),
                   reason='sampled fill exists, but full quality gates / historical Celestial exit health are unavailable',
                   evidence_gaps='risk_budget bid/ask; structure/news health and verified executor exit state')
        results.append(out)
    totals = dict(trades=0, wins=0, losses=0, gross_pnl=0.0, costs=0.0, net_pnl=0.0,
                  starting_capital=initial, ending_capital=capital, max_drawdown=drawdown,
                  unreplayable=sum(r['status'] == 'unreplayable' for r in results),
                  setups=len(results), skipped=sum(r['status'] == 'skipped' for r in results))
    if totals['unreplayable']:
        # Unknown fill/gate/exit paths cannot be reported as zero-P&L trades.
        for key in ('wins', 'losses', 'gross_pnl', 'costs', 'net_pnl', 'ending_capital', 'max_drawdown'):
            totals[key] = None
    latch = [dict(ts=r['ts'], event=r['event']) for r in audit_rows if any(
        x in r.get('event', '').lower() for x in ('kill', 'squareoff', 'loss_limit'))]
    latch_note = ('Exact latch time/cause is not logged. Current revision is ' + str(controls['revision']) +
                  '; no recorded kill/squareoff/loss-limit event proves the transition.' if not latch else str(latch))
    unresolved = [r for r in audit_rows if r.get('event') == 'execution_order_unresolved']
    if unresolved:
        gaps.append(f"Historical unresolved-order evidence: {len(unresolved)} audit records, "
                    f"{unresolved[0]['ts']} through {unresolved[-1]['ts']}. "
                    'Such an order also blocks new live submissions through portfolio_order_pending_wait; it is not assumed cleared by this replay.')
    return results, totals, counts, gamma_max, gaps, latch_note


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('day', type=date.fromisoformat)
    parser.add_argument('--capital', type=float, default=30000)
    parser.add_argument('--db', default=config.DB_PATH)
    parser.add_argument('--audit-dir', default='/var/lib/kadima/audit')
    parser.add_argument('--output-dir', default=str(Path.home() / 'replay_reports'))
    parser.add_argument('--lot-cache', default=str(Path(__file__).resolve().parents[1] / 'cache/lotsize_map.json'))
    parser.add_argument('--service-flags', default=str(Path.home() / 'replay_reports/kadima-live-switches.json'))
    args = parser.parse_args()
    if not math.isfinite(args.capital) or args.capital <= 0:
        parser.error('capital must be positive and finite')
    day = args.day.isoformat()
    flags = json.loads(Path(args.service_flags).read_text())['values']
    lot_cache = json.loads(Path(args.lot_cache).read_text())
    for key in ('KADIMA_FAST_MOMENTUM_LIVE', 'KADIMA_CELESTIAL_ENABLED', 'KADIMA_SESSION_AUTOTRADE'):
        if flags.get(key) not in ('0', '1'):
            parser.error('verified non-secret service flags required: ' + key)
    connection = open_readonly(args.db)
    try:
        connection.execute('BEGIN')
        with ExitStack() as stack:
            stack.enter_context(patch.object(socket, 'socket', side_effect=forbidden))
            stack.enter_context(patch.object(SESSION, 'ensure', side_effect=forbidden))
            stack.enter_context(patch.object(storage, 'conn', side_effect=forbidden))
            stack.enter_context(patch.object(celestial, 'connection', side_effect=forbidden))
            stack.enter_context(patch.object(audit, 'log', return_value=None))
            results, totals, counts, gamma_max, gaps, latch_note = run_replay(connection, day, args.capital,
                Path(args.audit_dir) / (day + '.jsonl'), flags, lot_cache)
    finally:
        connection.close()
    outdir = Path(args.output_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    csv_path, md_path = outdir / (day + '_replay.csv'), outdir / (day + '_replay.md')
    with csv_path.open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(results)
    explicit = [r for r in results if r['contract'].startswith('BANKNIFTY') and
                r['decision_time'][11:16] in ('09:30', '09:31', '09:32', '09:33')]
    report = [f'# {day}: stored-data replay on ₹{args.capital:,.2f}',
              '\nThis is a conservative recorded-evidence simulation, not proof of actual broker fills.',
              '\n## C — Totals\n', markdown_table([totals], list(totals)),
              '\n## A — Completed simulated trades\n',
              markdown_table([r for r in results if r['status'] == 'traded'],
                  ('time_in', 'time_out', 'contract', 'direction', 'lane', 'score', 'lots', 'lot_size',
                   'entry', 'exit', 'exit_reason', 'gross_pnl', 'costs', 'net_pnl', 'capital_after')),
              '\nNo completed trades are recorded when the 30-second fill rule cannot be met.',
              '\n## B — Setups not taken / unreplayable\n', markdown_table(results,
                  ('decision_time', 'contract', 'lane', 'score', 'lots', 'lot_size', 'raw_limit', 'limit', 'reason')),
              '\n## D — BANKNIFTY 09:30–09:33\n', markdown_table(explicit,
                  ('decision_time', 'contract', 'score', 'lots', 'lot_size', 'raw_limit', 'limit',
                   'next_quote_delay_seconds', 'reason', 't1_first_60m', 'best_60m_pct', 'worst_60m_pct')),
              '\nLater +10% hits are price-path diagnostics, not filled trades or realised P&L.',
              '\n## Candidate coverage and controls\n',
              f'\nIndex Gamma score maxima: {json.dumps(gamma_max, sort_keys=True)}. '
              f'Confirmed Shield gate passes: {sum(1 for r in results if r["lane"] == "shield")}. '
              f'Archived ready index suggestions: {len(results)}. '
              f'Current Celestial rows: {counts["current_celestial_rows"]}, index rows: {counts["current_celestial_index_rows"]}.',
              f'\nService flags: {json.dumps(flags, sort_keys=True)}. Lot sizes: disk cache with exact contract identity verification.',
              '\nKill latch: ' + latch_note,
              '\nKILL file exists at replay time: ' + str(Path(config.KILL_SWITCH_FILE).exists()) +
              '. Current file presence cannot reconstruct historical file presence.',
              '\nNo audit transition establishes the exact latch time; the absence of a log event is not evidence that no kill route was called.',
              '\n## E — Data gaps and assumptions\n']
    report.extend('- ' + gap for gap in gaps)
    report += ['- Brokerage is ₹20 per order; costs would be ₹40 + COST_RESERVE × (buy turnover + sell turnover) per completed round trip.',
               '- No completed fills means no brokerage/turnover costs and no compounding on this run.',
               '- Commission, cash compounding and exact exit P&L are left unevaluated for rows marked unreplayable; they are not represented as profitable or losing trades.']
    md_path.write_text('\n'.join(report) + '\n')
    print('C — Totals: ' + json.dumps(totals))
    print('D — BANKNIFTY 09:30–09:33:')
    print(markdown_table(explicit, ('decision_time', 'contract', 'score', 'lots', 'lot_size', 'limit',
                                 'next_quote_delay_seconds', 'reason', 't1_first_60m')))
    print('Reports: ' + str(csv_path) + ' ; ' + str(md_path))


if __name__ == '__main__':
    main()
