"""
stock_universe_scanner.py — continuous gamma scanning across the NIFTY
F&O stock universe (beyond the 4 indices engine.py already covers).

MEMORY-CONSCIOUS BY DESIGN, not just by hope:
    This EC2 instance visibly OOM-killed a process earlier the same night
    this file was written (2026-08-23) — from just one extra lightweight
    script running alongside kadima.service. A real, freshly-observed
    constraint, not theoretical caution. This script is architected
    around it, not just deployed and hoped about:

    - Runs as its OWN systemd service, its own process, its own memory
      space — same isolation principle as eod_watchdog.py. A memory
      problem here can never touch the already-hardened, tomorrow-
      critical 4-index engine.

    - Does NOT subscribe to live WebSocket ticks for the whole ~150-200
      stock universe. That would mean holding tick buffers and candle
      state for all of them simultaneously, all day — exactly the kind
      of unbounded-with-universe-size footprint that caused tonight's
      OOM kill, just bigger. Instead, two bounded stages:

        STAGE 1 (cheap, ALL ~150-200 names): periodic REST batch quote
        polls only. No per-stock state is held between polls — each
        cycle's snapshot is compared against the PREVIOUS poll's ranking
        purely in local variables that go out of scope immediately
        after use. This stage's memory footprint is flat regardless of
        universe size.

        STAGE 2 (full gamma scoring, ONLY the current shortlist, hard-
        capped at MAX_SHORTLIST): this is the ONLY place OptionChain and
        GammaHistory instances get created and held in memory — bounded
        to roughly the same order of magnitude as the 4-index engine
        already handles fine today, never the whole universe. Names
        that drop off the shortlist have their state released after a
        grace period, not accumulated indefinitely.

    Gamma detection (OI velocity, IV expansion, PCR/max-pain shift) all
    fundamentally need a ROLLING WINDOW of samples over time — none of
    it is computable from a single snapshot. That's precisely why this
    can't be a simple stateless "poll once and exit" batch script the
    way eod_watchdog.py is: stage 2 has to stay alive and keep polling
    its (small, bounded) shortlist to build that window. The memory
    discipline comes from keeping stage 2's working set small, not from
    avoiding persistence altogether.

CONFIRMED signals are now handed to the central engine through stock_handoff.
This process never imports AutoExecutor or places orders. Central execution
adds fresh candle/alignment, structural stop, liquidity and shared-slot gates.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta

from . import alerts, audit, gamma_blast_scanner as gamma, killswitch, strike_selector
from .config import IST, MARKET_CLOSE
from . import stock_handoff
from .instrument_master import stock_universe
from .option_chain import OptionChain
from .rate_limiter import QUOTE
from .scrip_master import ScripMaster
from .session import SESSION
from .utils import in_market_hours, now_ist

# Snapshot of currently-tracked-stock live scores, written here and read
# by control_api.py's /universe_snapshot route (separate process, shared
# filesystem -- same low-frequency atomic-write pattern as last_known.py,
# deliberately not a new HTTP server, to respect this box's documented
# memory constraints).
SNAPSHOT_PATH = os.path.expanduser("~/kadima-py/state/universe_snapshot.json")
_SNAPSHOT_LOCK = threading.Lock()

# ---------------------------------------------------------------- config
STAGE1_POLL_SEC = 60          # cheap universe-wide screen cadence
STAGE2_POLL_SEC = 30          # matches OI_POLL_SEC's convention for the shortlist
MAX_SHORTLIST = 20            # hard cap -- this bounds stage 2's worst-case memory
SHORTLIST_GRACE_MIN = 15      # keep tracking a name this long after it drops out
                              # of the stage-1 top-K, so a gamma history that's
                              # nearly built up isn't thrown away on a transient dip
QUOTE_BATCH_SIZE = 50         # Angel One's getMarketData has a practical per-call
                              # token limit; batch the universe into chunks this size


@dataclass
class _TrackedStock:
    chain: OptionChain
    history: gamma.GammaHistory
    spot: float | None = None
    days_to_expiry: float = 0.0
    spot_at: str | None = None
    session_vwap: float | None = None
    last_seen_in_shortlist: datetime = field(default_factory=lambda: datetime.now(IST))


class UniverseScanner:
    def __init__(self) -> None:
        from .celestial_scanner import Radar
        self.radar = Radar()
        self.scrips = ScripMaster.load()
        self.universe = stock_universe()
        audit.log("universe_scanner_loaded", stock_count=len(self.universe))
        self._strike_step_cache: dict[str, int] = {}
        self._tracked: dict[str, _TrackedStock] = {}
        self._last_score: dict[str, dict] = {}
        # Real last-traded price for EVERY name in the universe, not just
        # the up-to-20 that get gamma-scored -- stage 1 already fetches
        # this for the whole universe every cycle, it just used to throw
        # it away immediately except for the top movers. Small, bounded
        # (~208 tiny records), safe on this box's memory budget.
        self._last_universe_quotes: dict[str, dict] = {}
        self._stop = threading.Event()

    # ---------------------------------------------------------- stage 1
    def _stage1_screen(self) -> list[dict]:
        """One cheap pass over the whole universe. Returns the top
        MAX_SHORTLIST names ranked by |% change| for gamma-scoring
        promotion. Also records a real last-traded price for every name
        seen this pass into self._last_universe_quotes, independent of
        the shortlist -- that's what lets the dashboard show a real
        price for stocks the scanner isn't actively gamma-scoring."""
        moves: list[tuple[float, dict]] = []
        quotes: dict[str, dict] = {}

        for i in range(0, len(self.universe), QUOTE_BATCH_SIZE):
            batch = self.universe[i:i + QUOTE_BATCH_SIZE]
            by_exchange: dict[str, list[str]] = {}
            for stock in batch:
                by_exchange.setdefault(stock["spot_exchange"], []).append(stock["spot_token"])

            # Retry only unresolved tokens with endpoint-specific spacing.
            resp = self._fetch_screen_batch(by_exchange, i)

            by_token = {str(s["spot_token"]): s for s in batch}
            for row in resp.get("data", {}).get("fetched", []):
                token = str(row.get("symbolToken"))
                stock = by_token.get(token)
                if not stock:
                    continue
                ltp = row.get("ltp")
                pct_change = row.get("percentChange")
                if ltp is not None:
                    try:
                        if not math.isfinite(float(ltp)) or float(ltp) <= 0:
                            continue
                        quotes[stock["name"]] = {
                            "ltp": float(ltp),
                            "received_at": row["_received_at"],
                            "pct_change": (float(pct_change) if pct_change is not None else None),
                        }
                    except (TypeError, ValueError):
                        pass
                if pct_change is None:
                    continue
                try:
                    moves.append((abs(float(pct_change)), stock | {"ltp": ltp}))
                except (TypeError, ValueError):
                    continue

        self._last_universe_quotes = quotes
        moves.sort(key=lambda m: m[0], reverse=True)
        return [stock for _, stock in moves[:MAX_SHORTLIST]]

    def _fetch_screen_batch(self, by_exchange: dict, batch_start: int) -> dict:
        pending = {ex: list(tokens) for ex, tokens in by_exchange.items()}
        fetched = {}
        for attempt in range(3):
            # Other services may consume the account's allowance too.
            if self._stop.wait((1.25, 2.5, 5.0)[attempt]):
                break
            if not QUOTE.wait_and_allow(timeout_sec=3.0):
                continue
            try:
                SESSION.ensure()
                response = SESSION.api.getMarketData(
                    mode="FULL", exchangeTokens=pending)
                rows = ((response or {}).get("data") or {}).get("fetched") or []
                if not (response or {}).get("status"):
                    rows = []
                expected = {str(t) for tokens in pending.values() for t in tokens}
                for row in rows:
                    token = str(row.get("symbolToken"))
                    try:
                        price = float(row.get("ltp"))
                    except (TypeError, ValueError):
                        continue
                    if token in expected and math.isfinite(price) and price > 0:
                        fetched[token] = dict(row, _received_at=now_ist().isoformat())
            except Exception:
                # Do not write broker responses or credentials to logs.
                audit.log("universe_stage1_request_failed",
                          batch_start=batch_start, attempt=attempt + 1)
            pending = {
                ex: [t for t in tokens if str(t) not in fetched]
                for ex, tokens in pending.items()
            }
            pending = {ex: tokens for ex, tokens in pending.items() if tokens}
            if not pending:
                break
        missing = sum(map(len, pending.values()))
        if missing:
            audit.log("universe_stage1_incomplete", batch_start=batch_start,
                      missing=missing, received=len(fetched))
        return {"data": {"fetched": list(fetched.values())}}

    # ---------------------------------------------------------- promotion
    def _promote(self, stock: dict) -> None:
        name = stock["name"]
        if name in self._tracked:
            self._tracked[name].last_seen_in_shortlist = datetime.now(IST)
            return

        # Defensive cap, not just relying on _stage1_screen being the only
        # caller that happens to already be bounded. Given how real the
        # memory constraint behind this whole design is, the bound belongs
        # on the function that actually allocates state, not on trusting
        # every future caller to pre-slice correctly.
        if len(self._tracked) >= MAX_SHORTLIST:
            audit.log("universe_promote_refused_at_cap", name=name,
                      currently_tracked=len(self._tracked))
            return

        step = self._strike_step_cache.get(name)
        if step is None:
            step = self.scrips.infer_strike_step(name, stock["opt_exchange"])
            if step is None:
                audit.log("universe_promote_failed_no_strike_step", name=name)
                return
            self._strike_step_cache[name] = step

        from .config import Index
        ad_hoc_index = Index(name=name, exchange=stock["spot_exchange"],
                             token=stock["spot_token"], opt_exchange=stock["opt_exchange"],
                             opt_symbol_root=name, strike_step=step, weight=0.0)
        chain = OptionChain(ad_hoc_index, self.scrips)
        self._tracked[name] = _TrackedStock(chain=chain, history=gamma.GammaHistory())
        audit.log("universe_stock_promoted", name=name, strike_step=step)

    def _demote_stale(self) -> None:
        cutoff = datetime.now(IST) - timedelta(minutes=SHORTLIST_GRACE_MIN)
        stale = [name for name, t in self._tracked.items()
                if t.last_seen_in_shortlist < cutoff]
        for name in stale:
            stock_handoff.invalidate(name, end_episode=True)
            del self._tracked[name]
            self._last_score.pop(name, None)
            audit.log("universe_stock_demoted", name=name)

    # ---------------------------------------------------------- stage 2
    def _stage2_score(self) -> None:
        for name, tracked in list(self._tracked.items()):
            spot_map_token = tracked.chain.index.token
            if not QUOTE.wait_and_allow(timeout_sec=2.0):
                stock_handoff.invalidate(name)
                continue
            try:
                SESSION.ensure()
                resp = SESSION.api.getMarketData(
                    mode="FULL", exchangeTokens={tracked.chain.index.exchange: [spot_map_token]})
                fetched = (resp or {}).get("data", {}).get("fetched", [])
                matching = [x for x in fetched if str(x.get('symbolToken')) == str(spot_map_token)]
                if not resp.get('status') or len(matching) != 1:
                    raise ValueError('Missing underlying quote')
                tracked.spot = float(matching[0]['ltp'])
                if not math.isfinite(tracked.spot) or tracked.spot <= 0:
                    raise ValueError('Invalid underlying quote')
                tracked.spot_at = now_ist().isoformat()
                avg = matching[0].get('avgPrice')
                tracked.session_vwap = float(avg) if avg is not None else None
                if tracked.session_vwap is not None and not math.isfinite(tracked.session_vwap):
                    tracked.session_vwap = None
            except Exception as exc:                    # noqa: BLE001
                stock_handoff.invalidate(name)
                audit.log("universe_spot_poll_failed", name=name, error=type(exc).__name__)
                continue

            if not tracked.spot:
                continue

            try:
                expiry = datetime.strptime(self.scrips.nearest_expiry(tracked.chain.index), '%d%b%Y')
                expiry = expiry.replace(tzinfo=IST,hour=MARKET_CLOSE[0],minute=MARKET_CLOSE[1])
                tracked.days_to_expiry = (expiry - now_ist()).total_seconds() / 86400
                if tracked.days_to_expiry <= 0:
                    raise ValueError('Expired contract')
            except (TypeError, ValueError):
                stock_handoff.invalidate(name)
                continue
            tracked.chain.rebuild_ladder(tracked.spot)
            if not tracked.chain.poll():
                stock_handoff.invalidate(name)
                continue
            chain_at = now_ist().isoformat()

            result = gamma.score(name, tracked.chain, tracked.history,
                                 tracked.spot, tracked.days_to_expiry)
            self.radar.publish_chain(tracked.chain,tracked.spot,tracked.spot_at,result)

            audit.log("universe_gamma_score", name=name, tier=result.tier,
                      score=result.score, direction=result.direction,
                      components=result.components.as_dict())

            self._last_score[name] = {
                "tier": result.tier,
                "score": result.score,
                "direction": result.direction,
                "ts": now_ist().isoformat(),
            }

            if result.tier in ("ACTIVE", "CONFIRMED") and result.direction:
                pick = strike_selector.best(tracked.chain, result.direction,
                                             tracked.spot,
                                             days_to_expiry=tracked.days_to_expiry)
                if pick:
                    alerts.send_prealert(name, result.tier, result.direction,
                                          round(result.score, 1), tracked.spot,
                                          pick.as_dict(), tracked.chain.pcr(),
                                          tracked.chain.max_pain())

            try:
                sid = stock_handoff.publish(name,result.direction,result.tier,result.score,{
                    'index': asdict(tracked.chain.index), 'spot': tracked.spot,
                    'spot_at': tracked.spot_at, 'session_vwap': tracked.session_vwap,
                    'chain_at': chain_at, 'days_to_expiry': tracked.days_to_expiry,
                    'score': result.score, 'quotes': [asdict(q) for q in tracked.chain.quotes.values()]})
                if sid is not None:
                    audit.log('universe_gamma_handoff',name=name,signal_id=sid,score=result.score)
            except (ValueError,TypeError):
                stock_handoff.invalidate(name)
                audit.log('universe_handoff_invalid',name=name)

    # ---------------------------------------------------------- snapshot
    def _save_snapshot(self) -> None:
        """Atomic write of (a) currently-tracked stocks' real gamma
        scores -- bounded to at most MAX_SHORTLIST entries, same
        discipline as last_known.py -- and (b) a real last-traded price
        for every name in the universe stage 1 has seen a quote for,
        scored or not. control_api.py serves this file as-is to the
        dashboard via /universe_snapshot."""
        from . import celestial
        celestial.health('stock_universe',dict(expected=len(self.universe),priced=len(self._last_universe_quotes),
            option_chains_tracked=len(self._tracked),option_chain_limit=MAX_SHORTLIST,
            coverage='full_underlying_screen_rotating_option_shortlist'))
        rows = []
        for name, tracked in self._tracked.items():
            latest = self._last_score.get(name)
            if tracked.spot is None or latest is None:
                continue
            rows.append({
                "name": name,
                "spot": tracked.spot,
                "tier": latest["tier"],
                "score": latest["score"],
                "direction": latest["direction"],
                "last_updated": latest["ts"],
            })
        rows.sort(key=lambda r: r["score"], reverse=True)
        payload = {
            "ts": now_ist().isoformat(),
            "tracked": rows,
            "prices": self._last_universe_quotes,
            "execution": "central_executor_gated",
            "coverage": {
                "expected": len(self.universe),
                "priced": len(self._last_universe_quotes),
                "missing": sorted({s["name"] for s in self.universe}
                                  - set(self._last_universe_quotes)),
                "gamma_limit": MAX_SHORTLIST,
            },
        }
        try:
            with _SNAPSHOT_LOCK:
                os.makedirs(os.path.dirname(SNAPSHOT_PATH), exist_ok=True)
                tmp = SNAPSHOT_PATH + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(payload, f, indent=2)
                os.replace(tmp, SNAPSHOT_PATH)
        except Exception as exc:                    # noqa: BLE001
            audit.log("universe_snapshot_write_failed", error=str(exc))

    # ---------------------------------------------------------- main loop
    def run_forever(self) -> None:
        last_stage1 = 0.0
        last_stage2 = 0.0
        while not self._stop.is_set():
            if in_market_hours() and not killswitch.engaged():
                now = time.monotonic()
                if now - last_stage1 >= STAGE1_POLL_SEC:
                    shortlist = self._stage1_screen()
                    for stock in shortlist:
                        self._promote(stock)
                    self._demote_stale()
                    last_stage1 = now
                    self._save_snapshot()
                    audit.log("universe_stage1_complete", shortlisted=len(shortlist),
                              currently_tracked=len(self._tracked))

                if now - last_stage2 >= STAGE2_POLL_SEC:
                    self._stage2_score()
                    last_stage2 = now
                    self._save_snapshot()
            time.sleep(2)

    def stop(self) -> None:
        self._stop.set()


def main() -> int:
    audit.log("universe_scanner_starting")
    from . import celestial
    if celestial.enabled():
        from .full_universe_scanner import FullUniverseScanner
        scanner = FullUniverseScanner()
    else:
        scanner = UniverseScanner()
    try:
        scanner.run_forever()
    except KeyboardInterrupt:
        scanner.stop()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

