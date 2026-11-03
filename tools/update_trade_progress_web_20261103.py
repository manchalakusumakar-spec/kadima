#!/usr/bin/env python3
"""TRADE PROGRESS v1.1 web update: replace the installed v1 snippet block in dashboard.html with the
current web/trade_progress_snippet.html (adds the 'No live trade' idle line). Dashboard only, no restart.
Backup dashboard.html.bak-trade-progress-v11. Usage: --web /var/www/html [--check | --rollback]
"""
import argparse, os, shutil, sys
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUFFIX = '.bak-trade-progress-v11'
START = '<!-- TRADE PROGRESS v1 '


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--web', default='/var/www/html')
    ap.add_argument('--check', action='store_true'); ap.add_argument('--rollback', action='store_true')
    a = ap.parse_args(); dash = os.path.join(a.web, 'dashboard.html')
    if a.rollback:
        shutil.copy2(dash + SUFFIX, dash); print('restored', dash); return
    src = open(dash).read()
    if src.count(START) != 1:
        sys.exit('installed v1 snippet not found exactly once; nothing changed')
    i = src.index(START)
    j = src.index('</script>', src.index('<script id="tp-script">', i)) + len('</script>')
    new = open(os.path.join(HERE, 'web', 'trade_progress_snippet.html')).read().rstrip('\n')
    out = src[:i] + new + src[j:]
    if out.count('renderTradeProgress') < 2 or out.count('</body>') != 1 or 'tp-idle' not in out:
        sys.exit('result failed sanity checks; nothing changed')
    if a.check:
        print('check ok: snippet block replaced in memory', len(src), '->', len(out)); return
    shutil.copy2(dash, dash + SUFFIX); open(dash, 'w').write(out); print('written', dash)


if __name__ == '__main__':
    main()
