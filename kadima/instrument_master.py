"""
instrument_master.py — lot size resolution + F&O universe enumeration for KADIMA.

Resolves contract lot sizes from Angel One's scrip master so the capital
guard can compute the rupee cost of an order. Without this, capital_guard
rejects every entry (by design — it will not guess a lot size).

MEMORY NOTE — this matters on your instance:
    Angel One's OpenAPIScripMaster.json is tens of MB and parses into a
    large list of dicts. Holding that in memory alongside the engine is
    a plausible contributor to the kernel OOM kill seen at ~264MB.

    This module therefore:
      - streams the download to a temp file rather than into memory
      - parses once, extracts ONLY {token: lotsize} plus a small
        verification map, then discards the parsed structure
      - caches the compact result to disk as JSON (a few hundred KB)
      - loads the compact cache on subsequent starts, never the full file

    Peak extra memory is the one-time parse. If that alone is too much on
    this instance, set STREAM_PARSE=True to use ijson (incremental) —
    slower but near-constant memory. Requires: pip install ijson

REFRESH:
    The F&O universe and lot sizes change (expiries roll, lot sizes get
    revised by exchanges). The cache is rebuilt if older than
    CACHE_MAX_AGE_HOURS. A stale lot size is a silent sizing error, so
    this fails closed: if the cache is stale AND refresh fails, lookups
    return None rather than serving old values.

UNIVERSE EXTENSION (added for gamma_blast_scanner.py):
    The same one-time streaming pass that builds the lot-size map also
    builds the F&O STOCK universe — every underlying with tradeable
    stock options (OPTSTK), paired with its NSE cash-market spot token
    so a cheap price/volume screen can run without a second full parse
    of the scrip master.

    Index options (OPTIDX — NIFTY, BANKNIFTY, SENSEX, MIDCPNIFTY, etc.)
    are deliberately EXCLUDED from the universe: those four are already
    covered by config.INDICES / engine.py's dedicated pipeline. Filtering
    on instrumenttype == "OPTSTK" is the standard Angel One/NSE
    convention for "this is a stock option, not an index option" — more
    reliable than matching against a hardcoded name list, but VERIFY this
    against a live OpenAPIScripMaster.json sample before first live run;
    schema conventions can drift. Use tools/verify_universe.py for that.

    Equity spot token resolution: matched by NSE cash-market rows where
    exch_seg == "NSE", instrumenttype == "" (empty — Angel One's convention
    for plain equity), and symbol ends with "-EQ". The row's `name` field
    is expected to match the option row's `name` field for the same
    underlying. An OPTSTK underlying with NO resolvable spot token is
    excluded from the universe rather than included with a guessed token —
    same fail-closed philosophy as the rest of this module.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Optional

SCRIP_MASTER_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
)

CACHE_DIR = os.getenv("KADIMA_CACHE_DIR", os.path.expanduser("~/kadima-py/cache"))
COMPACT_CACHE = os.path.join(CACHE_DIR, "lotsize_map.json")
CACHE_MAX_AGE_HOURS = 12

STREAM_PARSE = False  # set True to use ijson for constant-memory parsing

_LOT_MAP: dict | None = None       # token -> lotsize
_SYMBOL_MAP: dict | None = None    # token -> tradingsymbol (verification only)
_UNIVERSE_MAP: dict | None = None  # underlying name -> {lot_size, opt_exchange}
_SPOT_MAP: dict | None = None      # underlying name -> {token, exchange}


# ---------------------------------------------------------------- build
def _cache_age_hours(path: str) -> float:
    if not os.path.exists(path):
        return float("inf")
    return (time.time() - os.path.getmtime(path)) / 3600.0


def _download_to_temp() -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = os.path.join(CACHE_DIR, "_scripmaster.tmp.json")
    with urllib.request.urlopen(SCRIP_MASTER_URL, timeout=120) as resp, open(tmp, "wb") as f:
        while True:
            chunk = resp.read(1 << 20)  # 1MB chunks, never whole file in RAM
            if not chunk:
                break
            f.write(chunk)
    return tmp


def _extract_compact(tmp_path: str) -> tuple[dict, dict, dict, dict]:
    """Parse the scrip master once and keep only what's needed:
    lot sizes, a verification symbol map, the F&O stock universe, and
    a name -> equity spot token map."""
    lot_map: dict = {}
    sym_map: dict = {}
    universe_map: dict = {}
    spot_map: dict = {}

    if STREAM_PARSE:
        import ijson  # type: ignore

        with open(tmp_path, "rb") as f:
            for row in ijson.items(f, "item"):
                _absorb_row(row, lot_map, sym_map, universe_map, spot_map)
    else:
        with open(tmp_path) as f:
            rows = json.load(f)
        for row in rows:
            _absorb_row(row, lot_map, sym_map, universe_map, spot_map)
        del rows  # release the large structure promptly

    return lot_map, sym_map, universe_map, spot_map


def _absorb_row(row: dict, lot_map: dict, sym_map: dict,
                 universe_map: dict, spot_map: dict) -> None:
    """One pass, four outputs. Called once per scrip master row."""
    exch = row.get("exch_seg")

    # ---- equity spot token (for the universe's price/volume screen) ----
    if exch == "NSE" and row.get("instrumenttype", "") == "":
        sym = row.get("symbol", "")
        if sym.endswith("-EQ"):
            name = row.get("name")
            token = row.get("token")
            if name and token:
                spot_map[name] = {"token": str(token), "exchange": "NSE"}
        return

    # ---- derivatives (NFO/BFO) ----
    if exch not in ("NFO", "BFO"):
        return

    token = row.get("token")
    lot = row.get("lotsize")
    if not token or not lot:
        return
    try:
        lot_i = int(lot)
    except (TypeError, ValueError):
        return
    if lot_i <= 0:
        return

    # existing lot-size map: every NFO/BFO contract, unchanged behaviour
    lot_map[str(token)] = lot_i
    sym_map[str(token)] = row.get("symbol") or row.get("name") or ""

    # F&O stock universe: OPTSTK only (excludes OPTIDX — the 4 indices
    # already have dedicated handling in engine.py / config.INDICES).
    itype = row.get("instrumenttype", "")
    if itype == "OPTSTK":
        name = row.get("name")
        if name:
            # keep one representative lot size per underlying; the most
            # recently seen row wins, which is fine — lot size is a
            # per-underlying property that rarely differs across a
            # single underlying's active expiries at any given moment
            universe_map[name] = {"lot_size": lot_i, "opt_exchange": exch}


def refresh_cache() -> bool:
    """Rebuild the compact cache (lot sizes + universe). Returns True on success."""
    tmp = None
    try:
        tmp = _download_to_temp()
        lot_map, sym_map, universe_map, spot_map = _extract_compact(tmp)
        if not lot_map:
            return False
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(COMPACT_CACHE, "w") as f:
            json.dump({"lot": lot_map, "sym": sym_map,
                       "universe": universe_map, "spot": spot_map}, f)
        return True
    except Exception:
        return False
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


# ---------------------------------------------------------------- load
def _ensure_loaded() -> bool:
    global _LOT_MAP, _SYMBOL_MAP, _UNIVERSE_MAP, _SPOT_MAP

    stale = _cache_age_hours(COMPACT_CACHE) > CACHE_MAX_AGE_HOURS

    if stale:
        ok = refresh_cache()
        if not ok and not os.path.exists(COMPACT_CACHE):
            return False
        if not ok:
            # Refresh failed and cache is stale. Fail closed: a stale lot
            # size is a silent sizing error, which is worse than no trade.
            return False
        _LOT_MAP = None  # force reload of the fresh file

    if _LOT_MAP is None:
        if not os.path.exists(COMPACT_CACHE):
            return False
        try:
            with open(COMPACT_CACHE) as f:
                blob = json.load(f)
            _LOT_MAP = blob.get("lot", {})
            _SYMBOL_MAP = blob.get("sym", {})
            _UNIVERSE_MAP = blob.get("universe", {})
            _SPOT_MAP = blob.get("spot", {})
        except Exception:
            return False

    return bool(_LOT_MAP)


# ---------------------------------------------------------------- api (lot size)
def get_lot_size(symbol: str, token: str) -> Optional[int]:
    """
    Lot size for this contract, or None if it cannot be resolved.

    Lookup is by TOKEN, which is unambiguous. Symbol is used only to
    verify the token maps to the contract you think it does — a mismatch
    returns None rather than sizing against the wrong instrument.
    """
    if not _ensure_loaded():
        return None

    lot = _LOT_MAP.get(str(token))
    if lot is None:
        return None

    # Verification: if we have a symbol on file and it disagrees with the
    # caller's, something is wrong upstream. Refuse rather than size.
    known = (_SYMBOL_MAP or {}).get(str(token))
    if known and symbol and known.upper() != str(symbol).upper():
        return None

    return lot


def contract_cost(entry_price: float, symbol: str, token: str, lots: int) -> Optional[float]:
    """Rupee cost of `lots` lots at `entry_price`, or None if unresolvable."""
    lot_size = get_lot_size(symbol, token)
    if lot_size is None or entry_price is None or entry_price <= 0:
        return None
    return entry_price * lot_size * lots


def cache_status() -> dict:
    """For health endpoints / debugging."""
    _ensure_loaded()
    return {
        "cache_path": COMPACT_CACHE,
        "exists": os.path.exists(COMPACT_CACHE),
        "age_hours": round(_cache_age_hours(COMPACT_CACHE), 2),
        "max_age_hours": CACHE_MAX_AGE_HOURS,
        "loaded_contracts": len(_LOT_MAP) if _LOT_MAP else 0,
        "universe_underlyings": len(_UNIVERSE_MAP) if _UNIVERSE_MAP else 0,
        "universe_with_spot_resolved": len(
            [n for n in (_UNIVERSE_MAP or {}) if n in (_SPOT_MAP or {})]),
    }


# ---------------------------------------------------------------- api (universe)
def stock_universe() -> list[dict]:
    """
    Every F&O stock underlying (OPTSTK) with a resolvable NSE spot token.

    Returns a list of:
        {"name": "RELIANCE", "lot_size": 250, "opt_exchange": "NFO",
         "spot_token": "2885", "spot_exchange": "NSE"}

    An underlying present in OPTSTK but with NO resolvable spot token is
    silently excluded (fail-closed — same philosophy as get_lot_size):
    a universe entry with an unresolvable spot means the cheap screener
    has nothing to poll, so including it would just be dead weight that
    can also mask a real data-quality problem. Check cache_status() if
    the returned count looks low against your expectations for the F&O
    stock list.
    """
    if not _ensure_loaded():
        return []

    out = []
    for name, info in (_UNIVERSE_MAP or {}).items():
        spot = (_SPOT_MAP or {}).get(name)
        if not spot:
            continue
        out.append({
            "name": name,
            "lot_size": info["lot_size"],
            "opt_exchange": info["opt_exchange"],
            "spot_token": spot["token"],
            "spot_exchange": spot["exchange"],
        })
    return out

