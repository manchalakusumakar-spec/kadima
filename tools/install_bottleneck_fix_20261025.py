#!/usr/bin/env python3
"""BOTTLENECK FIX v1 (trader 2026-10-25 22:12 IST "fix them immediately").

Replay of 21-23 Oct: Sniper and the old alignment check would have refused every
CONFIRMED gamma trade on 21 and 22 Oct (incl. SENSEX +75%). For CONFIRMED trades only
(auto, not manual; gamma_confirm says the Celestial row passes the v2 rule):

auto_executor.py  gamma: skip trade_policy.eligible (VWAP+EMA+RSI all aligned) - the v2
                  rule's Shield-direction check replaces it.
sniper.py         gamma: skip day-trend (sideways / counter-trend), A+B filter and the
                  second-signal rule; trend_rules' counter-trend-day block is overridden like
                  the pullback blocks (spot_beyond_band_stop still blocks).
                  Switch: sniper.json "confirmed_bypass" (default true).
scalp_v2.py       CONFIRMED scalps (score 80-90, 09:45-10:30) are not blocked as counter-trend.
gamma_confirm.py  + is_confirmed() helper.

Unchanged: 35% stop, trail, daily loss limit, Sniper stop-after-loss / side stopped / max 2
trades / 1 lot / 30% cash risk / 15% chase / spot_beyond_band_stop, clean spread, tradeable
contract, capital checks, 15:38 square-off. Every bypass is written to the audit log.

Anchor-based; nothing written unless every anchor matches once. Backups *.bak-bottleneck-20261025.
Usage: --root ~/kadima-py [--check | --rollback]
"""
import argparse, hashlib, os, shutil, sys

MARK = 'BOTTLENECK FIX v1'
SUFFIX = '.bak-bottleneck-20261025'
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GC_V2_SHA = '97407da601c7'

AE = [(
"""        if fast is None and gap is None and not trade_policy.eligible(kind, tier, score, direction, snap, spot,minimum=minimum,""",
"""        _confirmed_bypass = _confirmed_gamma(kind, manual_request, selected, minimum,  # BOTTLENECK FIX v1
                                             index_name, direction)
        if fast is None and gap is None and not _confirmed_bypass and not trade_policy.eligible(kind, tier, score, direction, snap, spot,minimum=minimum,""")]
AE_TAIL = '''

def _confirmed_gamma(kind, manual, selected, minimum, index_name=None, direction=None):
    """BOTTLENECK FIX v1: auto gamma entry whose Celestial row is CONFIRMED (v2 rule)."""
    if kind != 'gamma_blast' or manual or not selected:
        return False
    try:
        from . import gamma_confirm
        ok = gamma_confirm.is_confirmed(selected, now_ist(), minimum)
    except Exception:  # noqa: BLE001 - no bypass on any error
        return False
    if ok:
        try:
            audit.log('confirmed_bypass', check='trade_policy_eligible', index=index_name,
                      direction=direction, symbol=selected.get('symbol'))
        except Exception:  # noqa: BLE001
            pass
    return ok
'''

SN = [
("""    'stop_after_loss': True,   # LOSS CONTROLS v1 (2026-10-25): no Sniper entry after a Sniper loss today
""",
"""    'stop_after_loss': True,   # LOSS CONTROLS v1 (2026-10-25): no Sniper entry after a Sniper loss today
    'confirmed_bypass': True,  # BOTTLENECK FIX v1: CONFIRMED rows skip day-trend, A+B, second signal
"""),
("""def check(index_name, direction, token, option_ltp, trend, now, entry_mode='live',
          index_token=None, spot=None):""",
"""def check(index_name, direction, token, option_ltp, trend, now, entry_mode='live',
          index_token=None, spot=None, confirmed=False):"""),
("""    if trend not in ('UP', 'DOWN'):
        return 'sniper_sideways_day'
    if (trend == 'DOWN') != (direction == 'PE'):
        return 'sniper_counter_trend'
    ab = ab_check(index_name, index_token, direction, spot, now)
    if ab:
        return ab
""",
"""    if not confirmed:   # BOTTLENECK FIX v1: the CONFIRMED v2 rule already requires Shield direction
        if trend not in ('UP', 'DOWN'):
            return 'sniper_sideways_day'
        if (trend == 'DOWN') != (direction == 'PE'):
            return 'sniper_counter_trend'
        ab = ab_check(index_name, index_token, direction, spot, now)
        if ab:
            return ab
"""),
("""    if not earlier and cfg['require_second_signal']:""",
"""    if not earlier and cfg['require_second_signal'] and not confirmed:"""),
("""    ctoken = selected.get('token')
    trend = _trend(index_name, token, spot, vwap, now)
    _ab_last.pop((index_name, direction), None)
    block = check(index_name, direction, ctoken, selected.get('option_ltp'), trend, now,
                  'live' if m == 'live' else 'shadow', index_token=token, spot=spot)""",
"""    ctoken = selected.get('token')
    trend = _trend(index_name, token, spot, vwap, now)
    _ab_last.pop((index_name, direction), None)
    confirmed = False
    if cfg['confirmed_bypass']:   # BOTTLENECK FIX v1
        try:
            from . import gamma_confirm
            confirmed = gamma_confirm.is_confirmed(selected, now)
        except Exception:  # noqa: BLE001 - no bypass on error
            confirmed = False
    block = check(index_name, direction, ctoken, selected.get('option_ltp'), trend, now,
                  'live' if m == 'live' else 'shadow', index_token=token, spot=spot,
                  confirmed=confirmed)"""),
("""        pull = old in PULLBACK_BLOCKS and cfg['exempt_pullback']""",
"""        pull = (old in PULLBACK_BLOCKS or (confirmed and old.startswith('counter_trend_'))) \\
            and cfg['exempt_pullback']   # BOTTLENECK FIX v1: counter-trend-day too, for CONFIRMED"""),
("""         spot=spot, away_pct=ab_info.get('away_pct'), move15_pct=ab_info.get('move15_pct'))""",
"""         spot=spot, away_pct=ab_info.get('away_pct'), move15_pct=ab_info.get('move15_pct'),
         confirmed_bypass=confirmed)"""),
]

SC = [(
"""        if trend_rules.against_trend(label, trigger['direction']):
            row['blocked_reasons'].append('counter_trend_scalp')
""",
"""        if trend_rules.against_trend(label, trigger['direction']):
            if confirm_reasons(row.get('score'), now):   # BOTTLENECK FIX v1: CONFIRMED scalps exempt
                row['blocked_reasons'].append('counter_trend_scalp')
            else:
                row['counter_trend_bypassed'] = True
""")]


def sha(p):
    return hashlib.sha256(open(p, 'rb').read()).hexdigest()[:12]


def patch(path, items, tail=''):
    src = open(path).read()
    if MARK in src:
        sys.exit(f'{os.path.basename(path)}: already contains {MARK}; nothing changed')
    for old, new in items:
        if src.count(old) != 1:
            sys.exit(f'{os.path.basename(path)}: anchor found {src.count(old)} times: {old.strip()[:70]!r}; nothing changed')
        src = src.replace(old, new)
    src += tail
    compile(src, path, 'exec')
    return src


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--rollback', action='store_true')
    a = ap.parse_args()
    k = os.path.join(os.path.expanduser(a.root), 'kadima')
    files = {n: os.path.join(k, n) for n in ('auto_executor.py', 'sniper.py', 'scalp_v2.py', 'gamma_confirm.py')}
    if a.rollback:
        for p in files.values():
            if os.path.exists(p + SUFFIX):
                shutil.copy2(p + SUFFIX, p); print('restored', p)
        return
    if sha(files['gamma_confirm.py']) != GC_V2_SHA:
        sys.exit(f"gamma_confirm.py is not v2 (sha {sha(files['gamma_confirm.py'])}); nothing changed")
    new_gc = open(os.path.join(HERE, 'kadima', 'gamma_confirm.py')).read()
    assert 'def is_confirmed' in new_gc
    compile(new_gc, files['gamma_confirm.py'], 'exec')
    out = {files['auto_executor.py']: patch(files['auto_executor.py'], AE, AE_TAIL),
           files['sniper.py']: patch(files['sniper.py'], SN),
           files['scalp_v2.py']: patch(files['scalp_v2.py'], SC),
           files['gamma_confirm.py']: new_gc}
    if a.check:
        print('check ok:', ', '.join(os.path.basename(p) for p in out)); return
    for p, text in out.items():
        shutil.copy2(p, p + SUFFIX)
        open(p, 'w').write(text); print('written', p)


if __name__ == '__main__':
    main()
