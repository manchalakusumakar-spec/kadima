#!/usr/bin/env python3
"""Offline normalized JSONL replay. No order calls, quote fetches or assumed fills.

Usage: python3 tools/replay_stops.py recorded-session.jsonl > decisions.jsonl
See docs/dynamic_stops_v1.md. A replay is not a backtest of executable profits.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kadima import regime_stops as stops


def replay(records):
    states = {}
    for number, row in enumerate(records, 1):
        sid, kind = row['signal_id'], row['event']
        out = dict(record=number, signal_id=sid, mode='OFFLINE_DRAFT', event=kind)
        if kind == 'entry':
            if sid in states:
                raise ValueError('duplicate_replay_entry')
            plan = stops.initial_stop(row['signal'], row['chain_row'], row['market'],
                                      regimes=row.get('draft_regimes'), modifiers=row.get('draft_modifiers'))
            if isinstance(plan, stops.Reject):
                out['rejected'] = plan.reason
            else:
                states[sid] = stops.open_position(plan, sid, row.get('confirmed_quantity'))
                out['plan'] = asdict(plan)
        elif kind in ('tick', 'thesis', 'fill'):
            if sid not in states:
                raise ValueError('replay_event_without_accepted_entry')
            state = states[sid]
            if kind == 'tick':
                decision = stops.update_stop(state, row['tick'], row['market'])
                out['decision'] = asdict(decision)
            elif kind == 'thesis':
                out['decision'] = asdict(stops.thesis_check(state, row['signals']))
            else:
                # Must be an explicit fill for THIS draft decision/intent.
                # A legacy trade's fill cannot be assumed to fill another policy.
                if row.get('policy') != 'DRAFT' or not state.pending or row.get('reason') != state.pending['reason']:
                    raise ValueError('draft_decision_fill_identity_required')
                stops.acknowledge_fill(state, row['cumulative_quantity'], terminal=row['terminal'])
            out.update(phase=state.phase, stop=state.active_stop,
                       peak=state.peak_premium, remaining_qty=state.remaining_qty,
                       protected_stop=state.protected_stop,
                       hard_stop_premium=state.plan.hard_stop_premium,
                       structure_guard=asdict(state)['structure_guard'])
        elif kind == 'legacy_exit':
            # Preserve evidence for side-by-side inspection, without simulating a fill.
            out['legacy_exit'] = row['details']
        else:
            raise ValueError('unsupported_replay_event:' + kind)
        yield out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    args = parser.parse_args()
    with args.input.open() as handle:
        records = (json.loads(line) for line in handle if line.strip())
        for row in replay(records):
            print(json.dumps(row, allow_nan=False))


if __name__ == '__main__':
    main()
