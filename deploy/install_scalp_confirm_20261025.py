#!/usr/bin/env python3
"""Scalp v2 CONFIRMED gate (trader decision 2026-10-25).

A Scalp v2 row is CONFIRMED only when its score is 70-90 AND the time is
09:45-10:30 IST. Only CONFIRMED rows may auto-trade; every other row gets the
blocker `scalp_not_confirmed_score` and/or `scalp_not_confirmed_window`.
Switch: DATA_DIR/scalp_v2.json "confirm_gate": off|shadow|live (shadow = tag
and audit only, never blocks). Existing gates (trend, OI, news, threshold,
entries switch) are untouched.

Anchor-based: aborts with no change unless every anchor is found exactly once
and the file hash matches. Usage: install_scalp_confirm_20261025.py [--check|--rollback]
"""
import hashlib, os, shutil, sys

PATH = '/home/ubuntu/kadima-py/kadima/scalp_v2.py'
EXPECT = '7ac887bc0ee1aa7b'          # sha256 prefix of the live file on 2026-10-25
BACKUP = PATH + '.bak-scalpconfirm-20261025'
MARK = 'SCALP CONFIRM v1'

PATCHES = [
    ("DEFAULTS = {'mode': 'shadow', 'priority_poll': 'shadow', 'partial': 'shadow', 'thesis_exits': 'shadow', 'entries': 'live'}  # entries: trader switch 2026-10-22\n",
     "DEFAULTS = {'mode': 'shadow', 'priority_poll': 'shadow', 'partial': 'shadow', 'thesis_exits': 'shadow', 'entries': 'live',\n"
     "            'confirm_gate': 'live'}  # entries: trader switch 2026-10-22; confirm_gate: SCALP CONFIRM v1 (2026-10-25)\n"
     "# SCALP CONFIRM v1 (trader 2026-10-25): only score 70-90 between 09:45 and 10:30 IST may auto-trade.\n"
     "# 21-23 Oct replay: 25 such suggestions, 21 T2 / 4 SL (vs 30% T2 for all Scalp v2).\n"
     "CONFIRM_SCORE = (70.0, 90.0)\n"
     "CONFIRM_WINDOW = ((9, 45), (10, 30))   # inclusive, IST hour/minute\n"),
    ("def in_window(t):\n    return WINDOW[0] <= (t.hour, t.minute) < WINDOW[1]\n",
     "def in_window(t):\n    return WINDOW[0] <= (t.hour, t.minute) < WINDOW[1]\n\n\n"
     "def confirm_reasons(score, now):\n"
     "    \"\"\"SCALP CONFIRM v1: [] when the row is CONFIRMED, else the reasons it is not.\"\"\"\n"
     "    reasons = []\n"
     "    try:\n"
     "        ok = score is not None and CONFIRM_SCORE[0] <= float(score) <= CONFIRM_SCORE[1]\n"
     "    except (TypeError, ValueError):\n"
     "        ok = False\n"
     "    if not ok:\n"
     "        reasons.append('scalp_not_confirmed_score')\n"
     "    if not CONFIRM_WINDOW[0] <= (now.hour, now.minute) <= CONFIRM_WINDOW[1]:\n"
     "        reasons.append('scalp_not_confirmed_window')\n"
     "    return reasons\n"),
    ("    if config()['entries'] != 'live':   # trader 2026-10-22: scalping disabled\n"
     "        row['blocked_reasons'].append('scalp_entries_off')\n",
     "    if config()['entries'] != 'live':   # trader 2026-10-22: scalping disabled\n"
     "        row['blocked_reasons'].append('scalp_entries_off')\n"
     "    # SCALP CONFIRM v1: tier + auto-trade gate. Fails closed (blocks) on any error when live.\n"
     "    try:\n"
     "        not_confirmed = confirm_reasons(row.get('score'), now)\n"
     "    except Exception:  # noqa: BLE001\n"
     "        not_confirmed = ['scalp_confirm_error']\n"
     "    row['tier'] = 'CONFIRMED' if not not_confirmed else 'WATCH'\n"
     "    row['confirm_gate'] = config().get('confirm_gate', 'live')\n"
     "    if not_confirmed:\n"
     "        row['confirm_would_block'] = not_confirmed\n"
     "        if row['confirm_gate'] == 'live':\n"
     "            row['blocked_reasons'].extend(not_confirmed)\n"),
]


def sha(p):
    return hashlib.sha256(open(p, 'rb').read()).hexdigest()


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ''
    if arg == '--rollback':
        if not os.path.exists(BACKUP):
            sys.exit('no backup to restore')
        shutil.copy2(BACKUP, PATH); print('ROLLED BACK', sha(PATH)[:16]); return
    src = open(PATH).read()
    if MARK in src:
        print('ALREADY INSTALLED', sha(PATH)[:16]); return
    if not sha(PATH).startswith(EXPECT):
        sys.exit(f'hash mismatch: live {sha(PATH)[:16]} != expected {EXPECT}; no change')
    out = src
    for old, new in PATCHES:
        n = out.count(old)
        if n != 1:
            sys.exit(f'anchor found {n} times (need 1): {old[:60]!r}; no change')
        out = out.replace(old, new)
    compile(out, PATH, 'exec')
    if arg == '--check':
        print('CHECK OK: anchors found once, patched file compiles; nothing written'); return
    shutil.copy2(PATH, BACKUP)
    open(PATH, 'w').write(out)
    print('INSTALLED', sha(PATH)[:16], 'backup', BACKUP)


if __name__ == '__main__':
    main()
