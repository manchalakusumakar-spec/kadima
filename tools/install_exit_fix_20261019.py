#!/usr/bin/env python3
"""Install the 2026-10-19 early-exit fix (trail +15%, retain ladder, confirmed EMA/VWAP exits,
no spread exit, OI flip needs score 70, in-profit signals lock breakeven) into ~/kadima-py.

Anchor-based and fail-closed: every anchor must match exactly once or NOTHING is
written. Backups go to <file>.bak-20261019; every file is py_compiled first; a second
run changes nothing; --rollback restores the backups and removes new files.
Mode switches live in $KADIMA_DATA_DIR/trend_rules.json and are re-read every 30 s.

Usage (on EC2, after market hours):
  ~/kadima-py/bin/python install_exit_fix_20261019.py ~/kadima-py            # features installed in shadow
  ~/kadima-py/bin/python install_exit_fix_20261019.py ~/kadima-py --live     # switch  to live
  ~/kadima-py/bin/python install_exit_fix_20261019.py ~/kadima-py --rollback
"""
import json, os, py_compile, shutil, sys, tempfile
from pathlib import Path

PATCHES = [('kadima/trade_policy.py', 'RISK_FRACTION = 0.15\nACTIVATION = 0.05\nRETAIN_PROFIT = 0.50\nENTRY_BUFFER = 0.025\n', 'RISK_FRACTION = 0.15\nACTIVATION = 0.15     # 2026-10-19: was 0.05 - trail armed on noise and exited near breakeven\nRETAIN_PROFIT = 0.50  # base retain; widens with gain via retain_for_gain()\nENTRY_BUFFER = 0.025\n', 'ACTIVATION = 0.15     # 2026-10-19: was 0.05 - trail armed on noise and exited near breakeven'), ('kadima/trade_policy.py', '\ndef ratchet(entry, peak, previous, structural):\n', '\ndef retain_for_gain(gain):\n    """Fraction of peak gain kept once the trail is armed (2026-10-19 ladder)."""\n    return 0.75 if gain >= 1.0 else 0.65 if gain >= 0.35 else RETAIN_PROFIT\n\n\ndef ratchet(entry, peak, previous, structural):\n', 'def retain_for_gain(gain):'), ('kadima/trade_policy.py', '    active = peak >= entry * (1 + ACTIVATION)\n    trail = entry + (peak - entry) * RETAIN_PROFIT if active else structural\n    return max(previous or 0, structural, trail), active\n', '    active = peak >= entry * (1 + ACTIVATION)\n    trail = entry + (peak - entry) * retain_for_gain(peak / entry - 1) if active else structural\n    return max(previous or 0, structural, trail), active\n', 'trail = entry + (peak - entry) * retain_for_gain(peak / entry - 1) if active else structural'), ('kadima/celestial.py', "        raise ValueError('Missing protection inputs')\n    gain = peak / entry - 1\n    active = gain >= .05 - 1e-12\n    fraction = max(retain, .8 if gain >= .75 else .75 if gain >= .35 else\n                   .7 if gain >= .2 else .6 if gain >= .1 else .5)\n    stop = entry + (peak-entry) * fraction if active else structural\n", "        raise ValueError('Missing protection inputs')\n    from .trade_policy import ACTIVATION, retain_for_gain\n    gain = peak / entry - 1\n    active = gain >= ACTIVATION - 1e-12   # 2026-10-19: was .05 (shared with trade_policy)\n    fraction = max(retain, retain_for_gain(gain))\n    stop = entry + (peak-entry) * fraction if active else structural\n", 'from .trade_policy import ACTIVATION, retain_for_gain'), ('kadima/celestial_exit.py', 'from .utils import now_ist\n\n', "from .utils import now_ist\n\n# 2026-10-19 early-exit fix: EMA/VWAP breaks need this many consecutive\n# completed one-minute candles; while a trade is in profit these signals\n# only lock breakeven (see auto_executor) instead of exiting outright.\nCONFIRM_CANDLES = 2\nSOFT_REASONS = frozenset({\n    'premium_below_completed_1m_ema9', 'underlying_lost_vwap',\n    'opposite_gamma_confirmed', 'opposite_shield_active',\n    'directional_oi_reversed', 'fresh_catalyst_risk_block'})\n\n", '# 2026-10-19 early-exit fix: EMA/VWAP breaks need this many consecutive'), ('kadima/celestial_exit.py', "    previous_minute = datetime.fromisoformat(state['minute']) if state else None\n    # Gaps reset the indicator, never fill missing candles with invented prices.\n    if last is None or not 0 <= (now-last).total_seconds() <= 90 or last.date() != now.date():\n        state = dict(minute=minute.isoformat(), ts=now.isoformat(), close=ltp, count=0, ema=None)\n    elif minute > previous_minute:\n        if (minute-previous_minute).total_seconds() != 60:\n            state.update(count=0, ema=None)\n        close = state['close']\n", "    previous_minute = datetime.fromisoformat(state['minute']) if state else None\n    evidence = celestial.health('structure:' + pos.index)\n    live = bool(evidence and celestial.fresh(evidence['ts'], now)\n                and celestial.fresh(evidence.get('spot_at'), now))\n    beyond = None\n    if live:\n        vwap, spot = evidence.get('vwap'), evidence.get('spot')\n        if celestial.finite(vwap, .000001) and celestial.finite(spot, .000001):\n            beyond = (pos.direction == 'CE' and spot < vwap) or (pos.direction == 'PE' and spot > vwap)\n    # Gaps reset the indicator, never fill missing candles with invented prices.\n    if last is None or not 0 <= (now-last).total_seconds() <= 90 or last.date() != now.date():\n        state = dict(minute=minute.isoformat(), ts=now.isoformat(), close=ltp, count=0, ema=None,\n                     below=0, vwap_breaks=0, beyond=None)\n    elif minute > previous_minute:\n        if (minute-previous_minute).total_seconds() != 60:\n            state.update(count=0, ema=None, below=0, vwap_breaks=0, beyond=None)\n        close = state['close']\n", "live = bool(evidence and celestial.fresh(evidence['ts'], now)"), ('kadima/celestial_exit.py', "        state['count'] += 1\n        state['minute'] = minute.isoformat()\n    state.update(ts=now.isoformat(), close=ltp)\n    with c:\n", "        state['count'] += 1\n        # 2026-10-19: count consecutive COMPLETED 1m closes, not a single tick.\n        state['below'] = state.get('below', 0) + 1 if close < state['ema'] else 0\n        state['vwap_breaks'] = state.get('vwap_breaks', 0) + 1 if state.get('beyond') else 0\n        state['minute'] = minute.isoformat()\n    state.update(ts=now.isoformat(), close=ltp, beyond=beyond)\n    with c:\n", '# 2026-10-19: count consecutive COMPLETED 1m closes, not a single tick.'), ('kadima/celestial_exit.py', "                  (pos.signal_id, json.dumps(state, allow_nan=False)))\n    quote = celestial.health('quote:' + pos.exchange + ':' + pos.token)\n    if quote and celestial.fresh(quote['ts'], now):\n        bid, ask = quote.get('bid'), quote.get('ask')\n        if bid and ask and 200*(ask-bid)/(ask+bid) > 3:\n            return 'spread_widened_above_3pct'\n    if state['count'] >= 9 and ltp < state['ema']:\n        return 'premium_below_completed_1m_ema9'\n    evidence = celestial.health('structure:' + pos.index)\n    if evidence and celestial.fresh(evidence['ts'], now) and celestial.fresh(evidence.get('spot_at'), now):\n        vwap, spot = evidence.get('vwap'), evidence.get('spot')\n        if celestial.finite(vwap, .000001) and celestial.finite(spot, .000001):\n            if (pos.direction == 'CE' and spot < vwap) or (pos.direction == 'PE' and spot > vwap):\n                return 'underlying_lost_vwap'\n        opposite = 'PE' if pos.direction == 'CE' else 'CE'\n", "                  (pos.signal_id, json.dumps(state, allow_nan=False)))\n    # 2026-10-19: spread width is an exit-pricing concern only (cheap expiry\n    # strikes routinely quote >3% wide) - it no longer forces an exit.\n    if state['count'] >= 9 and state.get('below', 0) >= CONFIRM_CANDLES and ltp < state['ema']:\n        return 'premium_below_completed_1m_ema9'\n    if live:\n        if beyond and state.get('vwap_breaks', 0) >= CONFIRM_CANDLES:\n            return 'underlying_lost_vwap'\n        opposite = 'PE' if pos.direction == 'CE' else 'CE'\n", '# 2026-10-19: spread width is an exit-pricing concern only (cheap expiry'), ('kadima/celestial_exit.py', "            return 'opposite_shield_active'\n        if evidence.get('oi_direction') == opposite:\n            return 'directional_oi_reversed'\n", "            return 'opposite_shield_active'\n        if evidence.get('oi_direction') == opposite and evidence.get('score', 0) >= 70:\n            return 'directional_oi_reversed'\n", "if evidence.get('oi_direction') == opposite and evidence.get('score', 0) >= 70:"), ('kadima/auto_executor.py', '                from .celestial_exit import evaluate\n                reason = evaluate(pos, ltp, now)\n', "                from .celestial_exit import evaluate, SOFT_REASONS\n                reason = evaluate(pos, ltp, now)\n                # 2026-10-19: in profit, an adverse signal only locks breakeven;\n                # the trail / structural stop decides the actual exit.\n                if reason in SOFT_REASONS and ltp > pos.entry_price:\n                    lock = pos.entry_price * (1 + trade_policy.COST_RESERVE)\n                    if lock < ltp and lock > (pos.effective_stop or 0):\n                        pos.effective_stop = lock\n                        storage.upsert_position(pos)\n                        audit.log('celestial_exit_softened', signal_id=pos.signal_id,\n                                  signal=reason, stop=round(lock, 2), ltp=ltp)\n                    reason = None\n", 'from .celestial_exit import evaluate, SOFT_REASONS')]
NEW_FILES = {}
SUFFIX = '.bak-20261019'
REQUIRES = []          # (file, text that must already be present, message)
MODE_KEYS = {}        # key -> value written on install (shadow) unless already set
LIVE_KEYS = []   # keys set to "live" with --live


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    root = Path(args[0] if args else '.').expanduser()
    files = sorted({p[0] for p in PATCHES})
    if '--rollback' in sys.argv:
        for f in files:
            bak = root / (f + SUFFIX)
            if bak.exists():
                shutil.copy2(bak, root / f); print('restored', f)
        for f in NEW_FILES:
            if (root / f).exists():
                (root / f).unlink(); print('removed', f)
        return
    for f, needle, message in REQUIRES:
        if not (root / f).exists() or needle not in (root / f).read_text():
            sys.exit('ABORT: ' + message + '; nothing written')
    staged = {}
    for f in files:
        text = (root / f).read_text()
        for pf, old, new, marker in PATCHES:
            if pf != f:
                continue
            if new in text or marker in text:
                continue  # already applied (possibly extended by a later installer)
            if text.count(old) != 1:
                sys.exit(f'ABORT: anchor not found exactly once in {f}; nothing written:\n{old[:300]}')
            text = text.replace(old, new, 1)
        staged[f] = text
    staged.update(NEW_FILES)
    for f, text in staged.items():
        fd, tmp = tempfile.mkstemp(suffix='.py'); os.close(fd)
        Path(tmp).write_text(text); py_compile.compile(tmp, doraise=True); os.unlink(tmp)
    for f, text in staged.items():
        target = root / f
        if f not in NEW_FILES and target.exists() and not (root / (f + SUFFIX)).exists():
            shutil.copy2(target, root / (f + SUFFIX))
        target.write_text(text); print('wrote', f)
    data_dir = Path(os.environ.get('KADIMA_DATA_DIR', '/var/lib/kadima'))
    path = data_dir / 'trend_rules.json'
    try:
        cfg = json.loads(path.read_text()) if path.exists() else {}
    except ValueError:
        cfg = {}
    for k, v in MODE_KEYS.items():
        cfg.setdefault(k, v)
    if '--live' in sys.argv:
        for k in LIVE_KEYS:
            cfg[k] = 'live'
    try:
        path.write_text(json.dumps(cfg, indent=1))
        print('modes', json.dumps(cfg), '->', path)
    except OSError as exc:
        print('WARNING: could not write mode file (%s); defaults apply' % exc)
    print('OK - now: sudo systemctl restart kadima && journalctl -u kadima -n 30')


if __name__ == '__main__':
    main()
