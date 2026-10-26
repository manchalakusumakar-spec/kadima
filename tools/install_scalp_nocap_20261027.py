#!/usr/bin/env python3
"""SCALP NO-CAP v1 installer (trader 2026-10-27 00:25 IST: "there is no cap on capital for such
trades"; approved 00:26: scalps only, daily loss limit kept).

auto_executor.py, Scalping v2 (fast_momentum) entries only, when DATA_DIR/scalp_capital.json
mode is live:
  - capital = all available broker cash minus the 5% safety buffer (was the Shield band, <= 30,000)
  - no 2-lot limit (was MAX_LOTS_PER_SIGNAL)
  - no 15%-of-capital stop-risk lot limit (trade_policy.lots_within_allocation skipped)
  Kept: cash actually available (capital_guard + portfolio_capacity), the daily loss limit as a cap
  on total stop risk (portfolio_capacity), Best Entry half size on a no-pullback entry.
Gamma / Sniper / Shield / CAS / manual: unchanged.
Anchor-based; backup *.bak-scalp-nocap-20261027. Usage: --root ~/kadima-py --data /var/lib/kadima
[--mode shadow|live] [--check | --rollback]
"""
import argparse, json, os, shutil, sys

MARK = 'SCALP NO-CAP v1'
SUFFIX = '.bak-scalp-nocap-20261027'

AE = [
("""        verdict = risk_budget.select_within_budget(
            candidates, spot, allocated_capital=allocation,
            max_lots=min(1, MAX_LOTS_PER_SIGNAL) if gap else MAX_LOTS_PER_SIGNAL)""",
"""        _nocap = fast is not None and _scalp_nocap()   # SCALP NO-CAP v1
        if _nocap:
            allocation = _scalp_nocap_allocation(allocation)
        verdict = risk_budget.select_within_budget(
            candidates, spot, allocated_capital=allocation,
            max_lots=min(1, MAX_LOTS_PER_SIGNAL) if gap else (NOCAP_MAX_LOTS if _nocap else MAX_LOTS_PER_SIGNAL))"""),
("""        allowed_lots, cap_reason = capital_guard.enforce(
            self, SimpleNamespace(quote=chosen_quote), direction, verdict.lots)""",
"""        allowed_lots, cap_reason = capital_guard.enforce(
            self, SimpleNamespace(quote=chosen_quote), direction,
            NOCAP_MAX_LOTS if _nocap else verdict.lots)   # SCALP NO-CAP v1: cash decides"""),
("""        allowed_lots = trade_policy.lots_within_allocation(
            allocation, limit_price, stop, lot_size, allowed_lots)""",
"""        if not _nocap:   # SCALP NO-CAP v1: scalps skip the 15% stop-risk lot limit
            allowed_lots = trade_policy.lots_within_allocation(
                allocation, limit_price, stop, lot_size, allowed_lots)
        else:
            allowed_lots = min(allowed_lots, int(allocation // (limit_price * (1 + trade_policy.COST_RESERVE) * lot_size)))
            audit.log('scalp_nocap_sizing', index=index_name, direction=direction, lots=allowed_lots,
                      allocation=round(allocation, 2), limit=limit_price, lot_size=lot_size)"""),
]
AE_TAIL = '''

NOCAP_MAX_LOTS = 100   # SCALP NO-CAP v1: effectively "as many as the cash buys"


def _scalp_nocap():  # SCALP NO-CAP v1
    import json, os
    from .config import DATA_DIR
    try:
        with open(os.path.join(DATA_DIR, 'scalp_capital.json')) as f:
            return json.load(f).get('mode') == 'live'
    except (OSError, ValueError, AttributeError):
        return False


def _scalp_nocap_allocation(fallback):  # SCALP NO-CAP v1
    funds = capital_guard.broker_available_funds()
    if funds is None or not funds > 0:
        return fallback
    return funds * (1 - capital_guard.CAPITAL_SAFETY_BUFFER_PCT / 100)
'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--data', default='/var/lib/kadima')
    ap.add_argument('--mode', choices=('shadow', 'live'), default='shadow')
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--rollback', action='store_true')
    a = ap.parse_args()
    ae = os.path.join(os.path.expanduser(a.root), 'kadima', 'auto_executor.py')
    cfg = os.path.join(a.data, 'scalp_capital.json')
    if a.rollback:
        if os.path.exists(ae + SUFFIX):
            shutil.copy2(ae + SUFFIX, ae); print('restored', ae)
        json.dump({'mode': 'off'}, open(cfg, 'w')); print('mode off', cfg)
        return
    src = open(ae).read()
    if MARK in src:
        sys.exit(f'auto_executor.py: already contains {MARK}; nothing changed')
    for old, new in AE:
        n = src.count(old)
        if n != 1:
            sys.exit(f'auto_executor.py: anchor found {n} times: {old.strip()[:70]!r}; nothing changed')
        src = src.replace(old, new)
    src += AE_TAIL
    compile(src, ae, 'exec')
    if a.check:
        print(f'check ok: auto_executor.py {len(AE)}/{len(AE)}'); return
    shutil.copy2(ae, ae + SUFFIX)
    open(ae, 'w').write(src); print('written', ae)
    json.dump({'mode': a.mode}, open(cfg, 'w')); print('config', cfg, a.mode)


if __name__ == '__main__':
    main()
