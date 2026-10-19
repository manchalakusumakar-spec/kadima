#!/usr/bin/env python3
"""Install the 2026-10-19 early-exit fix into the LIVE ~/kadima-py checkout.

Anchor-based (the live tree is not a clean git checkout). Fail-closed: every
anchor must match exactly once in every file or NOTHING is written. Backups
go to <file>.bak-20261019 and every patched file is py_compiled before swap.

Usage (on EC2):  ~/kadima-py/bin/python install_exit_fix_20261019.py ~/kadima-py
Rollback:        python install_exit_fix_20261019.py ~/kadima-py --rollback
"""
import py_compile, shutil, sys, tempfile
from pathlib import Path

PATCHES = [('kadima/trade_policy.py', 'RISK_FRACTION = 0.15\nACTIVATION = 0.05\nRETAIN_PROFIT = 0.50\nENTRY_BUFFER = 0.025\n', 'RISK_FRACTION = 0.15\nACTIVATION = 0.15     # 2026-10-19: was 0.05 - trail armed on noise and exited near breakeven\nRETAIN_PROFIT = 0.50  # base retain; widens with gain via retain_for_gain()\nENTRY_BUFFER = 0.025\n'), ('kadima/trade_policy.py', '\ndef ratchet(entry, peak, previous, structural):\n', '\ndef retain_for_gain(gain):\n    """Fraction of peak gain kept once the trail is armed (2026-10-19 ladder)."""\n    return 0.75 if gain >= 1.0 else 0.65 if gain >= 0.35 else RETAIN_PROFIT\n\n\ndef ratchet(entry, peak, previous, structural):\n'), ('kadima/trade_policy.py', '    active = peak >= entry * (1 + ACTIVATION)\n    trail = entry + (peak - entry) * RETAIN_PROFIT if active else structural\n    return max(previous or 0, structural, trail), active\n', '    active = peak >= entry * (1 + ACTIVATION)\n    trail = entry + (peak - entry) * retain_for_gain(peak / entry - 1) if active else structural\n    return max(previous or 0, structural, trail), active\n'), ('kadima/celestial.py', "        raise ValueError('Missing protection inputs')\n    gain = peak / entry - 1\n    active = gain >= .05 - 1e-12\n    fraction = max(retain, .8 if gain >= .75 else .75 if gain >= .35 else\n                   .7 if gain >= .2 else .6 if gain >= .1 else .5)\n    stop = entry + (peak-entry) * fraction if active else structural\n", "        raise ValueError('Missing protection inputs')\n    from .trade_policy import ACTIVATION, retain_for_gain\n    gain = peak / entry - 1\n    active = gain >= ACTIVATION - 1e-12   # 2026-10-19: was .05 (shared with trade_policy)\n    fraction = max(retain, retain_for_gain(gain))\n    stop = entry + (peak-entry) * fraction if active else structural\n"), ('kadima/celestial_exit.py', 'from .utils import now_ist\n\n', "from .utils import now_ist\n\n# 2026-10-19 early-exit fix: EMA/VWAP breaks need this many consecutive\n# completed one-minute candles; while a trade is in profit these signals\n# only lock breakeven (see auto_executor) instead of exiting outright.\nCONFIRM_CANDLES = 2\nSOFT_REASONS = frozenset({\n    'premium_below_completed_1m_ema9', 'underlying_lost_vwap',\n    'opposite_gamma_confirmed', 'opposite_shield_active',\n    'directional_oi_reversed', 'fresh_catalyst_risk_block'})\n\n"), ('kadima/celestial_exit.py', "    previous_minute = datetime.fromisoformat(state['minute']) if state else None\n    # Gaps reset the indicator, never fill missing candles with invented prices.\n    if last is None or not 0 <= (now-last).total_seconds() <= 90 or last.date() != now.date():\n        state = dict(minute=minute.isoformat(), ts=now.isoformat(), close=ltp, count=0, ema=None)\n    elif minute > previous_minute:\n        if (minute-previous_minute).total_seconds() != 60:\n            state.update(count=0, ema=None)\n        close = state['close']\n", "    previous_minute = datetime.fromisoformat(state['minute']) if state else None\n    evidence = celestial.health('structure:' + pos.index)\n    live = bool(evidence and celestial.fresh(evidence['ts'], now)\n                and celestial.fresh(evidence.get('spot_at'), now))\n    beyond = None\n    if live:\n        vwap, spot = evidence.get('vwap'), evidence.get('spot')\n        if celestial.finite(vwap, .000001) and celestial.finite(spot, .000001):\n            beyond = (pos.direction == 'CE' and spot < vwap) or (pos.direction == 'PE' and spot > vwap)\n    # Gaps reset the indicator, never fill missing candles with invented prices.\n    if last is None or not 0 <= (now-last).total_seconds() <= 90 or last.date() != now.date():\n        state = dict(minute=minute.isoformat(), ts=now.isoformat(), close=ltp, count=0, ema=None,\n                     below=0, vwap_breaks=0, beyond=None)\n    elif minute > previous_minute:\n        if (minute-previous_minute).total_seconds() != 60:\n            state.update(count=0, ema=None, below=0, vwap_breaks=0, beyond=None)\n        close = state['close']\n"), ('kadima/celestial_exit.py', "        state['count'] += 1\n        state['minute'] = minute.isoformat()\n    state.update(ts=now.isoformat(), close=ltp)\n    with c:\n", "        state['count'] += 1\n        # 2026-10-19: count consecutive COMPLETED 1m closes, not a single tick.\n        state['below'] = state.get('below', 0) + 1 if close < state['ema'] else 0\n        state['vwap_breaks'] = state.get('vwap_breaks', 0) + 1 if state.get('beyond') else 0\n        state['minute'] = minute.isoformat()\n    state.update(ts=now.isoformat(), close=ltp, beyond=beyond)\n    with c:\n"), ('kadima/celestial_exit.py', "                  (pos.signal_id, json.dumps(state, allow_nan=False)))\n    quote = celestial.health('quote:' + pos.exchange + ':' + pos.token)\n    if quote and celestial.fresh(quote['ts'], now):\n        bid, ask = quote.get('bid'), quote.get('ask')\n        if bid and ask and 200*(ask-bid)/(ask+bid) > 3:\n            return 'spread_widened_above_3pct'\n    if state['count'] >= 9 and ltp < state['ema']:\n        return 'premium_below_completed_1m_ema9'\n    evidence = celestial.health('structure:' + pos.index)\n    if evidence and celestial.fresh(evidence['ts'], now) and celestial.fresh(evidence.get('spot_at'), now):\n        vwap, spot = evidence.get('vwap'), evidence.get('spot')\n        if celestial.finite(vwap, .000001) and celestial.finite(spot, .000001):\n            if (pos.direction == 'CE' and spot < vwap) or (pos.direction == 'PE' and spot > vwap):\n                return 'underlying_lost_vwap'\n        opposite = 'PE' if pos.direction == 'CE' else 'CE'\n", "                  (pos.signal_id, json.dumps(state, allow_nan=False)))\n    # 2026-10-19: spread width is an exit-pricing concern only (cheap expiry\n    # strikes routinely quote >3% wide) - it no longer forces an exit.\n    if state['count'] >= 9 and state.get('below', 0) >= CONFIRM_CANDLES and ltp < state['ema']:\n        return 'premium_below_completed_1m_ema9'\n    if live:\n        if beyond and state.get('vwap_breaks', 0) >= CONFIRM_CANDLES:\n            return 'underlying_lost_vwap'\n        opposite = 'PE' if pos.direction == 'CE' else 'CE'\n"), ('kadima/celestial_exit.py', "            return 'opposite_shield_active'\n        if evidence.get('oi_direction') == opposite:\n            return 'directional_oi_reversed'\n", "            return 'opposite_shield_active'\n        if evidence.get('oi_direction') == opposite and evidence.get('score', 0) >= 70:\n            return 'directional_oi_reversed'\n"), ('kadima/auto_executor.py', '                from .celestial_exit import evaluate\n                reason = evaluate(pos, ltp, now)\n', "                from .celestial_exit import evaluate, SOFT_REASONS\n                reason = evaluate(pos, ltp, now)\n                # 2026-10-19: in profit, an adverse signal only locks breakeven;\n                # the trail / structural stop decides the actual exit.\n                if reason in SOFT_REASONS and ltp > pos.entry_price:\n                    lock = pos.entry_price * (1 + trade_policy.COST_RESERVE)\n                    if lock < ltp and lock > (pos.effective_stop or 0):\n                        pos.effective_stop = lock\n                        storage.upsert_position(pos)\n                        audit.log('celestial_exit_softened', signal_id=pos.signal_id,\n                                  signal=reason, stop=round(lock, 2), ltp=ltp)\n                    reason = None\n")]
MARK = '2026-10-19'

def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else '.').expanduser()
    files = sorted({f for f, _, _ in PATCHES})
    if '--rollback' in sys.argv:
        for f in files:
            bak = root / (f + '.bak-20261019')
            if bak.exists():
                shutil.copy2(bak, root / f); print('restored', f)
        return
    staged = {}
    for f in files:
        text = (root / f).read_text()
        for pf, old, new in PATCHES:
            if pf != f: continue
            if new in text:
                continue  # already applied
            if text.count(old) != 1:
                sys.exit(f'ABORT: anchor not found exactly once in {f}; nothing written:\n{old[:300]}')
            text = text.replace(old, new, 1)
        staged[f] = text
    for f, text in staged.items():
        tmp = Path(tempfile.mkstemp(suffix='.py')[1]); tmp.write_text(text)
        py_compile.compile(str(tmp), doraise=True); tmp.unlink()
    for f, text in staged.items():
        target = root / f
        bak = root / (f + '.bak-20261019')
        if not bak.exists(): shutil.copy2(target, bak)
        target.write_text(text); print('patched', f)
    print('OK - now: sudo systemctl restart kadima && journalctl -u kadima -n 30')

if __name__ == '__main__':
    main()
