#!/usr/bin/env python3
"""
refresh_lotsize_cache.py — daily lot-size cache refresh, run OUT OF PROCESS.

WHY THIS IS SEPARATE FROM THE ENGINE:
    Parsing Angel One's scrip master peaks around 205MB RSS. The engine
    was OOM-killed by the kernel at ~264MB on 15 Aug. Doing this parse
    inside the live engine process risks reproducing that during market
    hours. This script does the heavy work in its own short-lived
    process; the engine only ever reads the resulting ~3.6MB cache.

CRON (08:45 IST, weekdays — 30 min before the 09:15 open):
    45 8 * * 1-5 /home/ubuntu/kadima-py/venv/bin/python \
        /home/ubuntu/kadima-py/refresh_lotsize_cache.py \
        >> /home/ubuntu/kadima-py/logs/lotsize_refresh.log 2>&1

    Note: verify the server's timezone. If it runs UTC, 08:45 IST is
    03:15 UTC -> use "15 3 * * 1-5" instead. Check with `timedatectl`.

EXIT CODES:
    0 = cache refreshed and verified
    1 = refresh failed, EXISTING cache left in place (may be stale)
    2 = refresh failed AND no usable cache exists -> sizing will be
        INERT and every entry will be rejected. This is loud on purpose.
"""

import os
import sys
import resource
from datetime import datetime, timezone, timedelta

sys.path.insert(0, "/home/ubuntu/kadima-py")

IST = timezone(timedelta(hours=5, minutes=30))

# Alert if the parse creeps toward the OOM threshold that killed the
# service before. This is a canary, not a limit.
RSS_WARN_MB = 240


def main() -> int:
    stamp = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")

    try:
        from kadima.instrument_master import refresh_cache, cache_status, get_lot_size
    except Exception as exc:
        print(f"[{stamp}] FATAL: cannot import instrument_master: {exc}")
        return 2

    before = cache_status()
    ok = refresh_cache()

    # Force a fresh read so the reported count reflects the new file,
    # not stale module globals.
    import kadima.instrument_master as im
    im._LOT_MAP = None
    im._SYMBOL_MAP = None
    after = cache_status()
    contracts = after.get("loaded_contracts", 0)

    peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024

    print(
        f"[{stamp}] refresh_ok={ok} contracts={contracts} "
        f"age_h={after.get('age_hours')} peak_rss_mb={peak_mb:.1f}"
    )

    if peak_mb > RSS_WARN_MB:
        print(
            f"[{stamp}] WARNING: peak RSS {peak_mb:.1f}MB approaching the "
            f"~264MB level at which the engine was OOM-killed on 15 Aug. "
            f"Consider STREAM_PARSE=True (needs ijson)."
        )

    if not ok:
        if before.get("exists") and contracts > 0:
            print(f"[{stamp}] ERROR: refresh failed; PREVIOUS cache retained "
                  f"({contracts} contracts, may be stale)")
            return 1
        print(f"[{stamp}] CRITICAL: refresh failed and no usable cache. "
              f"Lot-size lookups will return None; ALL entries will be "
              f"rejected until this is fixed.")
        return 2

    if contracts == 0:
        print(f"[{stamp}] CRITICAL: refresh reported success but cache is "
              f"empty. Check the exch_seg filter against the live feed.")
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
