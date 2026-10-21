#!/usr/bin/env python3
"""Install the hold-winners / cut-losers rule into the LIVE ~/kadima-py checkout,
SHADOW ONLY: it logs 'hold_or_cut_shadow' audit events and never changes an exit.

Anchor-based and fail-closed: the anchor must match exactly once or NOTHING is
written. Backup: kadima/auto_executor.py.bak-holdcut-20261021. Files are
py_compiled first; a second run changes nothing; --rollback restores the backup
and removes kadima/hold_or_cut.py. Run after market hours, then restart kadima.

  ~/kadima-py/venv/bin/python install_hold_or_cut_20261021.py ~/kadima-py
  ~/kadima-py/venv/bin/python install_hold_or_cut_20261021.py ~/kadima-py --rollback
"""
import os, py_compile, shutil, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
TARGET = 'kadima/auto_executor.py'
MODULE = 'kadima/hold_or_cut.py'
SUFFIX = '.bak-holdcut-20261021'
MARKER = '# 2026-10-21 shadow: hold winners, cut losers'
CAS_ANCHOR = ('                reason = cas_executor.exit_reason(pos, ltp, daily_loss_breached, force_close)\n'
              '                if reason:\n')
CAS_PATCH = ('                reason = cas_executor.exit_reason(pos, ltp, daily_loss_breached, force_close)\n'
             '                try:\n'
             '                    from . import hold_or_cut  ' + MARKER + ' (CAS)\n'
             '                    hold_or_cut.shadow(pos, ltp, reason, now)\n'
             '                except Exception as exc:  # noqa: BLE001 - shadow logging only\n'
             "                    audit.log('hold_or_cut_shadow_error', error=str(exc)[:200])\n"
             '                if reason:\n')
ANCHOR = ('\n            if reason:\n                self._close_position(pos, ltp, reason)\n\n'
          '    def _exit_fallback_price')
PATCH = ('\n            try:\n'
         '                from . import hold_or_cut  ' + MARKER + '\n'
         '                hold_or_cut.shadow(pos, ltp, reason, now)\n'
         '            except Exception as exc:  # noqa: BLE001 - shadow logging only\n'
         "                audit.log('hold_or_cut_shadow_error', error=str(exc)[:200])\n" + ANCHOR)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    root = Path(args[0] if args else '.').expanduser()
    if '--rollback' in sys.argv:
        bak = root / (TARGET + SUFFIX)
        if bak.exists():
            shutil.copy2(bak, root / TARGET); print('restored', TARGET)
        if (root / MODULE).exists():
            (root / MODULE).unlink(); print('removed', MODULE)
        return
    text = (root / TARGET).read_text()
    if 'SOFT_REASONS' not in text or 'trend_rules.managed_stop' not in text:
        sys.exit('ABORT: the 19-Oct exit-fix and trend-rules installs are not present; nothing written')
    module = (HERE / MODULE).read_text()
    if MARKER not in text:
        if text.count(ANCHOR) != 1:
            sys.exit('ABORT: anchor not found exactly once in %s; nothing written' % TARGET)
        text = text.replace(ANCHOR, PATCH, 1)
        if text.count(CAS_ANCHOR) == 1:      # CAS positions leave check_exits early
            text = text.replace(CAS_ANCHOR, CAS_PATCH, 1)
        else:
            print('note: CAS exit anchor not found; CAS positions are not shadowed')
    for body in (text, module):
        fd, tmp = tempfile.mkstemp(suffix='.py'); os.close(fd)
        Path(tmp).write_text(body); py_compile.compile(tmp, doraise=True); os.unlink(tmp)
    if (root / TARGET).read_text() != text:
        if not (root / (TARGET + SUFFIX)).exists():
            shutil.copy2(root / TARGET, root / (TARGET + SUFFIX))
        (root / TARGET).write_text(text); print('wrote', TARGET)
    if not (root / MODULE).exists() or (root / MODULE).read_text() != module:
        (root / MODULE).write_text(module); print('wrote', MODULE)
    print('OK (shadow only) - after the close: sudo systemctl restart kadima && journalctl -u kadima -n 30')


if __name__ == '__main__':
    main()
