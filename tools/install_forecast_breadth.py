"""Install NIFTY 50 constituent breadth into the LIVE hourly forecast, by context, not line numbers.

Run from a SEPARATE clone of branch `forecast-constituent-breadth` (never from, or over,
the live checkout). Each change is computed from this branch against BASE (the
hourly-forecast tip the 9 Oct install came from) and applied to the live file only
where its surrounding original lines appear exactly once. If any change cannot be
placed, nothing is written. Backups, an exact diff and a rollback script are written
first; compilation failure restores everything automatically. Safe to run twice.

What it changes (radar stays OFF unless KADIMA_NIFTY_RADAR_ENABLED=1 is set later):
  kadima/hourly_forecast_model.py  breadth input, gated learning, deviation reason
  kadima/hourly_forecast.py        live breadth reading, 5-min storage, audit, view
  kadima/engine.py                 radar init, guarded subscription, guarded tick hook
  kadima/feed.py                   exchange timestamp on ticks; radar ticks not stored
  kadima/constituent_radar.py      new file (PR #31)
  forecast.html (web)              breadth line per slot, live reading, tuning row
Not touched: forecast_observer / checklist_forecast / api.py / rest-of-day mode, so the
existing GPT forecast mode is preserved exactly. No restart is done here.

    python3 tools/install_forecast_breadth.py --live ~/kadima-py --check
    python3 tools/install_forecast_breadth.py --live ~/kadima-py
"""
import argparse
import difflib
import hashlib
import json
import os
import py_compile
import shutil
import subprocess
import sys
import time

BASE = 'acc54c82cf9be5ecbba48522add566cd969a15e6'   # hourly-forecast tip
SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATCHED = ('kadima/hourly_forecast_model.py', 'kadima/hourly_forecast.py',
           'kadima/engine.py', 'kadima/feed.py')
NEW_FILES = ('kadima/constituent_radar.py',)
WEB_SRC = 'web/hourly_forecast.html'


def git_show(rev, path):
    return subprocess.run(['git', '-C', SRC, 'show', f'{rev}:{path}'], check=True,
                          capture_output=True, text=True).stdout


def head_text(path):
    with open(os.path.join(SRC, path)) as f:
        return f.read()


def hunks(old, new, context=3):
    """(old_block, new_block) pairs, each with up to `context` unchanged lines around it."""
    a, b = old.splitlines(keepends=True), new.splitlines(keepends=True)
    out = []
    for group in difflib.SequenceMatcher(None, a, b, autojunk=False).get_grouped_opcodes(context):
        i1, i2 = group[0][1], group[-1][2]
        j1, j2 = group[0][3], group[-1][4]
        out.append((''.join(a[i1:i2]), ''.join(b[j1:j2])))
    return out


def apply(live, pairs, label, problems):
    """Apply every pair to `live` or report why not. Already-applied pairs are skipped."""
    text, done, skipped = live, 0, 0
    for n, (old, new) in enumerate(pairs, 1):
        if text.count(new) == 1:
            skipped += 1                               # already installed (checked first: an
                                                       # insertion's new text contains its old text)
        elif text.count(old) == 1:
            text = text.replace(old, new)
            done += 1
        else:
            first = next((ln.strip() for ln in old.splitlines() if ln.strip()), '')[:70]
            problems.append(f'{label}: change {n}/{len(pairs)} - original text found '
                            f'{text.count(old)} times near "{first}" (need exactly 1)')
    return text, done, skipped


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--live', default=os.path.expanduser('~/kadima-py'))
    ap.add_argument('--web', default='/var/www/html/forecast.html')
    ap.add_argument('--check', action='store_true')
    args = ap.parse_args()
    live = os.path.abspath(os.path.expanduser(args.live))
    if os.path.samefile(live, SRC):
        print('STOPPED - run this from a separate clone, not from the live checkout.')
        return 1

    problems, writes = [], {}          # writes: absolute path -> (before or None, after)
    for rel in PATCHED:
        path = os.path.join(live, rel)
        if not os.path.exists(path):
            problems.append(f'{rel}: missing in live checkout')
            continue
        with open(path) as f:
            before = f.read()
        after, done, skipped = apply(before, hunks(git_show(BASE, rel), head_text(rel)), rel, problems)
        print(f'  {rel}: {done} change(s) to apply, {skipped} already present')
        if after != before:
            writes[path] = (before, after)
    for rel in NEW_FILES:
        path, new = os.path.join(live, rel), head_text(rel)
        if os.path.exists(path):
            with open(path) as f:
                cur = f.read()
            if cur == new:
                print(f'  {rel}: already present')
            else:
                problems.append(f'{rel}: exists live with different content - review by hand')
        else:
            print(f'  {rel}: new file')
            writes[path] = (None, new)

    web_staged = None
    if os.path.exists(args.web):
        with open(args.web) as f:
            before = f.read()
        after, done, skipped = apply(before, hunks(git_show(BASE, WEB_SRC), head_text(WEB_SRC)),
                                     'forecast.html', problems)
        print(f'  forecast.html: {done} change(s) to apply, {skipped} already present')
        if after != before:
            web_staged = (before, after)
    else:
        problems.append(f'{args.web}: not found (pass --web with the served forecast page)')

    if problems:
        print('\nSTOPPED - nothing changed:')
        for p in problems:
            print('  -', p)
        return 1
    if not writes and not web_staged:
        print('\nALREADY_INSTALLED - nothing to do')
        return 0
    if args.check:
        print('\nCHECK_OK - would change:', ', '.join(
            sorted([os.path.relpath(p, live) for p in writes] + (['forecast.html'] if web_staged else []))))
        return 0

    stamp = time.strftime('%Y%m%d-%H%M%S')
    bdir = os.path.expanduser(f'~/kadima-backups/forecast-breadth-{stamp}-{os.getpid()}')
    os.makedirs(bdir)
    diff, manifest, rollback = [], [], ['#!/bin/sh', 'set -e']
    for path, (before, after) in sorted(writes.items()):
        rel = os.path.relpath(path, live)
        diff += difflib.unified_diff((before or '').splitlines(keepends=True), after.splitlines(keepends=True),
                                     'a/' + rel, 'b/' + rel)
        manifest.append(dict(file=rel, before=sha(before) if before else None, after=sha(after)))
        if before is None:
            rollback.append(f'rm -f "{path}"')
        else:
            os.makedirs(os.path.dirname(os.path.join(bdir, rel)), exist_ok=True)
            shutil.copy2(path, os.path.join(bdir, rel))
            rollback.append(f'cp "{os.path.join(bdir, rel)}" "{path}"')
    if web_staged:
        before, after = web_staged
        diff += difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                     'a/forecast.html', 'b/forecast.html')
        with open(os.path.join(bdir, 'forecast.html.orig'), 'w') as f:
            f.write(before)
        with open(os.path.join(bdir, 'forecast.html.new'), 'w') as f:
            f.write(after)
        manifest.append(dict(file=args.web, before=sha(before), after=sha(after)))
        rollback.append(f'sudo cp "{os.path.join(bdir, "forecast.html.orig")}" "{args.web}"')
    rollback.append('echo "Files restored. Restart only through your normal safe-restart check: '
                    'sudo systemctl restart kadima"')
    with open(os.path.join(bdir, 'deploy.diff'), 'w') as f:
        f.writelines(diff)
    with open(os.path.join(bdir, 'manifest.json'), 'w') as f:
        json.dump(dict(base=BASE, source=subprocess.run(['git', '-C', SRC, 'rev-parse', 'HEAD'],
                                                        capture_output=True, text=True).stdout.strip(),
                       live=live, files=manifest), f, indent=2)
    with open(os.path.join(bdir, 'rollback.sh'), 'w') as f:
        f.write('\n'.join(rollback) + '\n')
    os.chmod(os.path.join(bdir, 'rollback.sh'), 0o755)

    for path, (_, after) in writes.items():
        with open(path, 'w') as f:
            f.write(after)
    try:
        for path in writes:
            if path.endswith('.py'):
                py_compile.compile(path, doraise=True)
    except py_compile.PyCompileError as exc:
        for path, (before, _) in writes.items():
            if before is None:
                os.remove(path)
            else:
                with open(path, 'w') as f:
                    f.write(before)
        print('COMPILE FAILED - all files restored. Backups in', bdir)
        print(exc)
        return 1

    print('\nINSTALL_OK - engine files written (not yet loaded; the running engine is unchanged).')
    print('Backups, deploy.diff, manifest.json and rollback.sh in', bdir)
    if web_staged:
        if os.access(args.web, os.W_OK):
            with open(args.web, 'w') as f:
                f.write(web_staged[1])
            print('forecast.html updated.')
        else:
            print('Panel page needs root. Apply with:')
            print(f'  sudo cp {os.path.join(bdir, "forecast.html.new")} {args.web}')
    print('Rollback any time:', os.path.join(bdir, 'rollback.sh'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
