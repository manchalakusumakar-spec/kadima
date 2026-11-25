"""Apply the 26-Nov NSE-VIX change to kadima/hourly_forecast.py and tools/forecast_nightly.py.

The live hourly_forecast.py on the server carries changes that are in no repository branch
(open band 21-Nov, shield 09:15 11-Nov, ...), so this change ships as exact, anchored text
replacements instead of a whole file. Each anchor must occur exactly once; otherwise nothing is
written. Running it twice is a no-op (the marker is detected).

    python3 tools/patch_nse_vix_20261126.py ROOT            # patch ROOT/kadima/hourly_forecast.py ...
    python3 tools/patch_nse_vix_20261126.py ROOT --check    # report only

What changes (hourly 09:15 slot only):
  * if no ATM option IV exists when the 09:15 slot is made (always, at 09:15:04), 0.9 x India VIX is
    used as the implied volatility (kadima.nse_daily.vix_for_forecast: live broker VIX, else the
    previous session's close), and the fixed 1.4x open-band widening is then not applied
    (VIX sizes the band). If no VIX is available the slot is made exactly as before (1.4x).
  * inputs record iv_source ('india_vix_live' / 'india_vix_last' / 'india_vix_close_<day>').
  * switch: KADIMA_VIX_OPEN=0 restores the old behaviour without a code change.
  * nightly (16:05): stores NSE archive data (VIX, participant OI, advances/declines) in nse_daily.
"""
import os
import sys

MARK = 'NSE-VIX v1 26-Nov-2026'

HF_EDITS = [
    ("OPEN_BAND_MULT = float(os.environ.get('KADIMA_OPEN_BAND_MULT', '1.4'))\n",
     "OPEN_BAND_MULT = float(os.environ.get('KADIMA_OPEN_BAND_MULT', '1.4'))\n"
     "VIX_OPEN = os.environ.get('KADIMA_VIX_OPEN', '1') != '0'   # " + MARK + ": India VIX as the 09:15 slot IV\n"),
    ("        f = M.make_forecast(days, di, i, n, anchor, as_of=day, tod=self._tod_cache[key],\n",
     "        iv_source = 'atm_iv' if atm_iv is not None else None\n"
     "        if atm_iv is None and start == (9, 15) and VIX_OPEN:   # " + MARK + " (trader: use NSE data)\n"
     "            try:\n"
     "                from . import nse_daily\n"
     "                atm_iv, iv_source = nse_daily.vix_for_forecast(now)\n"
     "            except Exception:                           # noqa: BLE001 - optional input, never blocks a slot\n"
     "                atm_iv, iv_source = None, None\n"
     "        f = M.make_forecast(days, di, i, n, anchor, as_of=day, tod=self._tod_cache[key],\n"),
    ("        if start == (9, 15) and OPEN_BAND_MULT != 1.0:   # OPEN-BAND v1 21-Nov-2026 (trader approved)\n",
     "        if iv_source:\n"
     "            f.inputs['iv_source'] = iv_source\n"
     "        vix_sized = str(iv_source or '').startswith('india_vix')   # " + MARK + ": VIX sizes the 09:15 band\n"
     "        if start == (9, 15) and OPEN_BAND_MULT != 1.0 and not vix_sized:   # OPEN-BAND v1 21-Nov-2026 (trader approved)\n"),
]

NIGHTLY_EDITS = [
    ("    if args.only == 'bhavcopy':\n        return 0\n",
     "    if args.only == 'bhavcopy':\n        return 0\n"
     "    try:                                                # " + MARK + ": NSE archives (VIX, participant OI, A/D)\n"
     "        from kadima import nse_daily\n"
     "        log('nse_daily: ' + json.dumps(nse_daily.run_nightly(now)))\n"
     "    except Exception as exc:                            # noqa: BLE001 - never blocks the retune\n"
     "        log(f'nse_daily: failed {exc}')\n"),
]


def patch(path, edits, check_only=False):
    src = open(path).read()
    if MARK in src:
        return 'already'
    out = src
    for old, new in edits:
        n = out.count(old)
        if n != 1:
            raise SystemExit(f'ABORT: anchor found {n} times in {path}: {old.strip()[:70]!r}')
        out = out.replace(old, new)
    if not check_only:
        tmp = path + '.nsevix-tmp'
        with open(tmp, 'w') as f:
            f.write(out)
        os.replace(tmp, path)
    return 'patched' if not check_only else 'ok-to-patch'


def main(argv):
    root = argv[0] if argv else '.'
    check_only = '--check' in argv
    only = None
    if '--only' in argv:
        only = argv[argv.index('--only') + 1]
    targets = [('kadima/hourly_forecast.py', HF_EDITS), ('tools/forecast_nightly.py', NIGHTLY_EDITS)]
    # verify every file first, then write: all or nothing
    for rel, edits in targets:
        if only and rel != only:
            continue
        p = os.path.join(root, rel)
        src = open(p).read()
        if MARK not in src:
            for old, _ in edits:
                if src.count(old) != 1:
                    raise SystemExit(f'ABORT: anchor found {src.count(old)} times in {p}: {old.strip()[:70]!r}')
    for rel, edits in targets:
        if only and rel != only:
            continue
        print(rel, patch(os.path.join(root, rel), edits, check_only))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
