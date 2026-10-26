#!/usr/bin/env python3
"""BEST ENTRY v1 installer (trader 2026-10-26). See kadima/best_entry.py for the rules.

auto_executor.py
  entry   : before sizing, best_entry.decide() may hold the order (wait for a held
            pullback), widen the stop to below the pullback low (35% cap), or ask for
            half size after 3 min without a dip.
  sizing  : half size applied after Sniper sizing (1 lot stays 1 lot).
  exit    : loss-side exits pass best_entry.filter_exit() (10-min grace, 1-min close
            confirmation, 60 s persistence); a band-stop reason is not latched while held.
best_entry.py copied in; DATA_DIR/best_entry.json written with the chosen mode.

Anchor-based; nothing written unless every anchor matches once and the result compiles.
Backup: auto_executor.py.bak-best-entry-20261026.
Usage: --root ~/kadima-py --data /var/lib/kadima [--mode shadow|live] [--check | --rollback]
"""
import argparse, json, os, shutil, sys

MARK = 'BEST ENTRY v1'
SUFFIX = '.bak-best-entry-20261026'
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

AE = [
("""        stop = verdict.candidate.stop_price
        allowed_lots = trade_policy.lots_within_allocation(""",
"""        stop = verdict.candidate.stop_price
        # BEST ENTRY v1: wait for a held pullback; stop below the pullback low (35% cap).
        _be = _be_decide(kind, manual_request, source_signal_id, chosen_quote, pricing_ltp,
                         stop, self.state.premium_tracker)
        if not _be.get('go'):
            return self._reject(_be['block'], index_name, direction)
        if _be.get('stop') and 0 < _be['stop'] < pricing_ltp:
            stop = _be['stop']
        allowed_lots = trade_policy.lots_within_allocation("""),
("""            return self._reject(_sn_block or 'sniper_no_lots', index_name, direction)
        quantity = allowed_lots * lot_size""",
"""            return self._reject(_sn_block or 'sniper_no_lots', index_name, direction)
        from . import best_entry  # BEST ENTRY v1: half size on a no-pullback entry
        allowed_lots = best_entry.scale_lots(allowed_lots, _be)
        quantity = allowed_lots * lot_size"""),
("""        sniper.after_submit(kind, manual_request, pos)
""",
"""        sniper.after_submit(kind, manual_request, pos)
        best_entry.after_submit(source_signal_id)  # BEST ENTRY v1
"""),
("""                if band_reason and not pos.exit_reason:
                    pos.exit_reason = band_reason""",
"""                if band_reason and not pos.exit_reason and _be_filter(  # BEST ENTRY v1
                        pos, ltp, band_reason, now, self.state.premium_tracker):
                    pos.exit_reason = band_reason"""),
("""            if reason:
                self._close_position(pos, ltp, reason)

    def _exit_fallback_price""",
"""            if reason and reason != pos.exit_reason:   # BEST ENTRY v1: loss-side hold rules
                reason = _be_filter(pos, ltp, reason, now, self.state.premium_tracker)
            if reason:
                self._close_position(pos, ltp, reason)

    def _exit_fallback_price"""),
]
AE_TAIL = '''

def _be_samples(tracker, token):  # BEST ENTRY v1
    from . import trend_rules
    return trend_rules.tracker_samples(tracker, token)


def _be_decide(kind, manual, sid, quote, ltp, stop, tracker):  # BEST ENTRY v1
    from . import best_entry
    return best_entry.decide(kind=kind, manual=manual, signal_id=sid, token=quote.token,
                             ltp=ltp, stop=stop, delta=getattr(quote, 'delta', None),
                             samples=_be_samples(tracker, quote.token))


def _be_filter(pos, ltp, reason, now, tracker):  # BEST ENTRY v1
    from . import best_entry
    return best_entry.filter_exit(pos, ltp, reason, now, _be_samples(tracker, pos.token))
'''


def patch(path):
    src = open(path).read()
    if MARK in src:
        sys.exit(f'{os.path.basename(path)}: already contains {MARK}; nothing changed')
    for old, new in AE:
        n = src.count(old)
        if n != 1:
            sys.exit(f'auto_executor.py: anchor found {n} times: {old.strip()[:70]!r}; nothing changed')
        src = src.replace(old, new)
    src += AE_TAIL
    compile(src, path, 'exec')
    return src


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--data', default='/var/lib/kadima')
    ap.add_argument('--mode', choices=('shadow', 'live'), default='shadow')
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--rollback', action='store_true')
    a = ap.parse_args()
    k = os.path.join(os.path.expanduser(a.root), 'kadima')
    ae = os.path.join(k, 'auto_executor.py')
    be = os.path.join(k, 'best_entry.py')
    cfg = os.path.join(a.data, 'best_entry.json')
    if a.rollback:
        if os.path.exists(ae + SUFFIX):
            shutil.copy2(ae + SUFFIX, ae); print('restored', ae)
        if os.path.exists(be):
            os.remove(be); print('removed', be)
        if os.path.exists(cfg):
            json.dump({'mode': 'off'}, open(cfg, 'w')); print('mode off', cfg)
        return
    for need in ('trend_rules.py', 'sniper.py'):
        if not os.path.exists(os.path.join(k, need)):
            sys.exit(f'{need} missing on this box; nothing changed')
    new_be = open(os.path.join(HERE, 'kadima', 'best_entry.py')).read()
    compile(new_be, be, 'exec')
    text = patch(ae)
    if a.check:
        print('check ok: auto_executor.py anchors 5/5, best_entry.py compiles'); return
    shutil.copy2(ae, ae + SUFFIX)
    open(be, 'w').write(new_be); print('written', be)
    open(ae, 'w').write(text); print('written', ae)
    old = {}
    if os.path.exists(cfg):
        try:
            old = json.load(open(cfg))
        except ValueError:
            old = {}
    old['mode'] = a.mode
    json.dump(old, open(cfg, 'w'), indent=1); print('config', cfg, old)


if __name__ == '__main__':
    main()
