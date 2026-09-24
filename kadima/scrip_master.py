
"""
Angel One scrip master: resolves index + strike + expiry -> tradeable token.

The master file is ~100k rows and changes daily (new expiries, rolled
contracts). It is downloaded once at startup and cached to disk. If the
download fails, the engine refuses to start rather than running against a stale
contract list - trading a delisted token is a silent, expensive failure.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from datetime import datetime, date
from typing import Iterable

from . import audit
from .config import DATA_DIR, IST

MASTER_URL = ("https://margincalculator.angelbroking.com/OpenAPI_File/files/"
              "OpenAPIScripMaster.json")
CACHE = os.path.join(DATA_DIR, "scrip_master.json")


class ScripMaster:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self._option_rows = {}
        for row in rows:
            if row.get('instrumenttype') in ('OPTIDX', 'OPTSTK') and row.get('permitted_to_trade') is not False:
                self._option_rows.setdefault((row.get('exch_seg'), row.get('name')), []).append(row)

    # ------------------------------------------------------------------ load
    @classmethod
    def load(cls, max_age_hours: int = 12) -> "ScripMaster":
        os.makedirs(DATA_DIR, exist_ok=True)
        fresh = (os.path.exists(CACHE) and
                 (datetime.now().timestamp() - os.path.getmtime(CACHE))
                 < max_age_hours * 3600)
        if not fresh:
            try:
                with urllib.request.urlopen(MASTER_URL, timeout=60) as r:
                    data = json.loads(r.read().decode())
                with open(CACHE, "w") as fh:
                    json.dump(data, fh)
                audit.log("scrip_master_downloaded", rows=len(data))
            except Exception as exc:                    # noqa: BLE001
                audit.log("scrip_master_download_failed", error=str(exc))
                if os.environ.get('KADIMA_CELESTIAL_ENABLED','0')=='1':
                    raise RuntimeError('Celestial requires a fresh contract master') from exc
                if not os.path.exists(CACHE):
                    raise RuntimeError(
                        "Cannot start: scrip master unavailable and no cache."
                    ) from exc
        with open(CACHE) as fh:
            return cls(json.load(fh))

    # --------------------------------------------------------------- queries
    def _options_for(self, index) -> Iterable[dict]:
        root = index.opt_symbol_root
        for r in self._option_rows.get((index.opt_exchange, root), []):
            if (r.get("exch_seg") == index.opt_exchange
                    and r.get("instrumenttype", "").startswith("OPT")
                    and r.get("name") == root):
                yield r

    def nearest_expiry(self, index, on: date | None = None) -> str | None:
        on = on or datetime.now(IST).date()
        expiries = set()
        for r in self._options_for(index):
            e = r.get("expiry")
            if not e:
                continue
            try:
                d = datetime.strptime(e, "%d%b%Y").date()
            except ValueError:
                continue
            if d >= on:
                expiries.add((d, e))
        if not expiries:
            return None
        return sorted(expiries)[0][1]

    def strikes_around(self, index, atm: float, n: int,
                       expiry: str | None = None) -> list[dict]:
        expiry = expiry or self.nearest_expiry(index)
        if not expiry:
            return []
        step = index.strike_step
        wanted = {atm + i * step for i in range(-n, n + 1)}
        out = []
        for r in self._options_for(index):
            if r.get("expiry") != expiry:
                continue
            try:
                # scrip master stores strike in paise
                strike = float(r["strike"]) / 100.0
            except (KeyError, TypeError, ValueError):
                continue
            if strike not in wanted:
                continue
            sym = r.get("symbol", "")
            opt_type = "CE" if sym.endswith("CE") else ("PE" if sym.endswith("PE") else None)
            if not opt_type:
                continue
            out.append({"token": str(r["token"]), "symbol": sym,
                        "strike": strike, "opt_type": opt_type,
                        "expiry": expiry,
                        # Angel One's scrip master JSON often returns lotsize
                        # as a string, not a native int. Coerced defensively
                        # here -- this field wasn't type-safe before, and a
                        # caller doing arithmetic or a numeric comparison on
                        # it directly would break exactly the way the
                        # test_order_pathway.py smoke test did on 2026-08-21.
                        # get_lot_size() in instrument_master.py was already
                        # safe (always returns int or None); this just brings
                        # this field up to the same standard.
                        "lotsize": int(r["lotsize"]) if r.get("lotsize") else None})
        return out

    def token_for_index_spot(self, index) -> str:
        return index.token

    # ------------------------------------------------ generalized (any underlying)
    # The methods above require a registered config.Index object -- fine for
    # the 4 indices, which are few and known in advance. The F&O stock
    # universe is neither: ~150-200 names, each needing its own strike step
    # inferred from the data (no single constant like NIFTY's 50 works
    # across RELIANCE, TATASTEEL, etc.). These generalized versions take
    # name/exchange directly instead of an Index, so the stock-universe
    # scanner can query any underlying without a config.py entry for each.

    def _options_for_name(self, name: str, opt_exchange: str) -> Iterable[dict]:
        for r in self._option_rows.get((opt_exchange, name), []):
            if (r.get("exch_seg") == opt_exchange
                    and r.get("instrumenttype", "").startswith("OPT")
                    and r.get("name") == name):
                yield r

    def nearest_expiry_for_name(self, name: str, opt_exchange: str,
                                on: date | None = None) -> str | None:
        on = on or datetime.now(IST).date()
        expiries = set()
        for r in self._options_for_name(name, opt_exchange):
            e = r.get("expiry")
            if not e:
                continue
            try:
                d = datetime.strptime(e, "%d%b%Y").date()
            except ValueError:
                continue
            if d >= on:
                expiries.add((d, e))
        if not expiries:
            return None
        return sorted(expiries)[0][1]

    def infer_strike_step(self, name: str, opt_exchange: str,
                          expiry: str | None = None) -> float | None:
        """Strike intervals aren't a fixed constant per stock the way they
        are for the 4 indices -- this infers it from the actual listed
        strikes. Uses the MINIMUM gap between distinct sorted strikes,
        not an average or mode: a few missing strikes far OTM (common in
        less liquid names) only ever produce a LARGER gap than the true
        step, never smaller, so the minimum stays a safe, robust proxy
        even against a sparse tail. Returns None (not a guess) if there
        aren't at least 2 distinct strikes to compare -- same fail-closed
        convention as everything else touching contract resolution."""
        expiry = expiry or self.nearest_expiry_for_name(name, opt_exchange)
        if not expiry:
            return None
        strikes = set()
        for r in self._options_for_name(name, opt_exchange):
            if r.get("expiry") != expiry:
                continue
            try:
                strikes.add(float(r["strike"]) / 100.0)
            except (KeyError, TypeError, ValueError):
                continue
        if len(strikes) < 2:
            return None
        ordered = sorted(strikes)
        gaps = [round(b - a, 2) for a, b in zip(ordered, ordered[1:]) if b > a]
        if not gaps:
            return None
        return min(gaps)

    def strikes_for_underlying(self, name: str, opt_exchange: str, atm: float,
                               n: int, strike_step: int,
                               expiry: str | None = None) -> list[dict]:
        """Same contract and return shape as strikes_around(), generalized
        to take name/exchange/strike_step directly instead of a registered
        Index -- see this section's docstring for why."""
        expiry = expiry or self.nearest_expiry_for_name(name, opt_exchange)
        if not expiry:
            return []
        wanted = {atm + i * strike_step for i in range(-n, n + 1)}
        out = []
        for r in self._options_for_name(name, opt_exchange):
            if r.get("expiry") != expiry:
                continue
            try:
                strike = float(r["strike"]) / 100.0
            except (KeyError, TypeError, ValueError):
                continue
            if strike not in wanted:
                continue
            sym = r.get("symbol", "")
            opt_type = "CE" if sym.endswith("CE") else ("PE" if sym.endswith("PE") else None)
            if not opt_type:
                continue
            out.append({"token": str(r["token"]), "symbol": sym,
                        "strike": strike, "opt_type": opt_type,
                        "expiry": expiry,
                        "lotsize": int(r["lotsize"]) if r.get("lotsize") else None})
        return out

