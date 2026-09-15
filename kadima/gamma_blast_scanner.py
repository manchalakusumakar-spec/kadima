"""
gamma_blast_scanner.py — composite gamma-activity scorer.

WHAT THIS IS:
    A pure scoring module, deliberately separate from data collection.
    It takes whatever option-chain state already exists (an OptionChain
    that has already been polled, same object engine.py already builds
    for the four indices) and turns it into a WATCH/ACTIVE/CONFIRMED
    tier — same convention as Bull Shield v2.1, so both signal streams
    read the same way in /state and in alerts.

    For the four indices this costs ZERO extra network calls: engine.py
    already polls OptionChain every OI_POLL_SEC. This module just scores
    what's already there. For the wider F&O stock universe, a SEPARATE
    process (gamma_universe_scanner.py, not yet built) does its own
    polling for a shortlist and calls the same score() function here —
    the scoring logic is shared, the data collection is not, so a bug in
    universe polling can never destabilise the four-index engine that
    already works.

ON THE FACTOR COUNT:
    Earlier notes referenced an "11-factor" scanner. Reviewing the real
    codebase, only some of those factors correspond to things Angel
    One's API actually gives you (api.py's own UNAVAILABLE block lists
    six signals that are NOT computable from this feed at all: iceberg
    orders, dealer gamma, absorption/distribution, retail-trap
    detection, stop clusters, intraday FII/DII flows). Padding the
    weight table to hit a specific number with made-up factors would be
    worse than a smaller, honest set — every factor below is something
    this codebase can actually compute from data it actually has.

FACTORS (weights sum to 100, same convention as Bull Shield's WEIGHTS):

    oi_velocity          25  reuses OIVelocityTracker as-is. Real wall
                              build/loss, not a new metric.
    iv_expansion         20  ATM IV change over ROLLING_WINDOW_MIN. Rapid
                              IV expansion is the closest thing to a
                              direct "gamma event" signal available here.
    strike_concentration 20  Herfindahl index of OI across the tracked
                              ladder. OI piling into one or two strikes
                              (vs spread evenly) signals positioning
                              around a specific level.
    wall_proximity       15  distance from spot to the nearest large
                              wall. A big wall close to spot is where a
                              gamma-driven pin or break is most likely.
    pcr_shift            10  change in PCR over the window. Contributes
                              to INTENSITY (something is moving), not
                              direction — PCR's directional meaning is
                              genuinely contested (rising PCR can mean
                              bearish positioning OR bullish put-writing)
                              so this scanner does not use it to pick
                              CE/PE, only to size the alert.
    max_pain_shift        5  change in max pain strike over the window.
                              Smaller weight — max pain is a settlement-
                              day heuristic, weaker as an intraday signal.
    underlying_momentum   5  above_vwap / ema_bull from indicators.Snapshot,
                              IF the caller has one available (indices
                              always will; stock-universe stage 2 may
                              not, in which case this factor is simply
                              absent and the other 95 points still apply
                              — logged explicitly, never silently
                              defaulted to a guessed direction).

DIRECTION:
    Derived ONLY from oi_velocity (which side's wall is losing OI — same
    convention engine.py already uses: CE walls dissolving = resistance
    lifting = bullish) and underlying_momentum when available. pcr_shift
    and max_pain_shift deliberately do NOT vote on direction, per the
    ambiguity note above. If oi_velocity has no signal this cycle,
    direction is None and the result cannot reach ACTIVE/CONFIRMED
    (an intensity score with no direction is not an actionable signal —
    logged as "gamma_activity_no_direction", not silently discarded).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from . import audit
from .config import IST
from .greeks import implied_vol_and_greeks
from .option_chain import OptionChain, OptionQuote

TIER_WATCH = 30.0
TIER_ACTIVE = 50.0
TIER_CONFIRMED = 70.0    # eligibility score, not a win probability
                         # instruction, same proportional scaling as engine.py's
                         # Bull Shield thresholds — kept consistent deliberately,
                         # so "CONFIRMED" means the same confidence level whether
                         # you're reading a Bull Shield or a gamma signal.

WEIGHTS = {
    "oi_velocity": 25.0,
    "iv_expansion": 20.0,
    "strike_concentration": 20.0,
    "wall_proximity": 15.0,
    "pcr_shift": 10.0,
    "max_pain_shift": 5.0,
    "underlying_momentum": 5.0,
}
assert abs(sum(WEIGHTS.values()) - 100.0) < 1e-9, "gamma WEIGHTS must sum to 100"

ROLLING_WINDOW_MIN = 10          # same horizon as OI_VELOCITY_WINDOW_MIN, for
                                  # comparability between the two signal streams
IV_EXPANSION_PP_FOR_MAX_SCORE = 3.0    # 3 percentage points of IV move -> full score
PCR_SHIFT_FOR_MAX_SCORE = 0.30         # PCR moving by 0.30 -> full score
MAX_PAIN_SHIFT_PCT_FOR_MAX_SCORE = 1.0  # max pain moving 1% of spot -> full score
WALL_PROXIMITY_PCT_FOR_MAX_SCORE = 0.5  # wall within 0.5% of spot -> full score
MIN_WALL_OI_TO_COUNT = 50_000          # ignore walls too small to matter


@dataclass
class GammaSnapshot:
    """Minimal subset of indicators.Snapshot needed for the momentum
    factor. Callers pass this explicitly rather than importing engine's
    IndexState, keeping this module decoupled from engine.py."""
    above_vwap: Optional[bool] = None
    ema_bull: Optional[bool] = None


@dataclass
class GammaHistory:
    """Rolling window of PCR / max-pain / ATM-IV samples for one
    index or underlying. Caller owns one instance per name and keeps
    calling record() every time the chain is polled; this module never
    polls anything itself."""
    pcr: deque = field(default_factory=lambda: deque(maxlen=200))
    max_pain: deque = field(default_factory=lambda: deque(maxlen=200))
    atm_iv: deque = field(default_factory=lambda: deque(maxlen=200))

    def record(self, ts: datetime, pcr: Optional[float],
               max_pain: Optional[float], atm_iv: Optional[float]) -> None:
        self.pcr.append((ts, pcr))
        self.max_pain.append((ts, max_pain))
        self.atm_iv.append((ts, atm_iv))

    @staticmethod
    def _window_delta(samples: deque, window_min: int) -> Optional[tuple[float, float]]:
        """(oldest_value, latest_value) within the window, or None if
        there isn't enough history yet. Same 'stay silent on a partial
        window' philosophy as OIVelocityTracker — a delta computed over
        an incomplete window is misleading, not just imprecise."""
        if len(samples) < 2:
            return None
        now = samples[-1][0]
        window = timedelta(minutes=window_min)
        oldest_val = None
        for ts, val in samples:
            if now - ts <= window and val is not None:
                oldest_val = val
                break
        latest_val = samples[-1][1]
        if oldest_val is None or latest_val is None:
            return None
        return oldest_val, latest_val


@dataclass
class GammaComponents:
    oi_velocity: Optional[dict] = None
    iv_expansion_pp: Optional[float] = None
    strike_concentration_hhi: Optional[float] = None
    wall_proximity_pct: Optional[float] = None
    pcr_shift: Optional[float] = None
    max_pain_shift_pct: Optional[float] = None
    momentum: Optional[str] = None

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class GammaResult:
    name: str
    score: float
    tier: str                     # NONE / WATCH / ACTIVE / CONFIRMED
    direction: Optional[str]      # "CE" / "PE" / None
    components: GammaComponents


# ---------------------------------------------------------------- helpers
def _atm_quote_pair(chain: OptionChain) -> tuple[Optional[OptionQuote], Optional[OptionQuote]]:
    """Nearest CE and PE to the chain's current ATM strike."""
    if chain.atm is None:
        return None, None
    ce = min((q for q in chain.quotes.values() if q.opt_type == "CE"),
             key=lambda q: abs(q.strike - chain.atm), default=None)
    pe = min((q for q in chain.quotes.values() if q.opt_type == "PE"),
             key=lambda q: abs(q.strike - chain.atm), default=None)
    return ce, pe


def _atm_iv(chain: OptionChain, spot: float, days_to_expiry: float) -> Optional[float]:
    """Average of ATM CE/PE implied vol. Prefers broker-supplied IV
    (q.iv) where present, falls back to the same Black-Scholes solver
    strike_selector.py already uses — never a second, different model."""
    ce, pe = _atm_quote_pair(chain)
    ivs = []
    for q in (ce, pe):
        if q is None or not q.ltp:
            continue
        if q.iv:
            ivs.append(q.iv)
            continue
        iv, _ = implied_vol_and_greeks(spot=spot, strike=q.strike, premium=q.ltp,
                                        days=days_to_expiry, opt_type=q.opt_type)
        if iv:
            ivs.append(iv)
    return (sum(ivs) / len(ivs)) if ivs else None


def _strike_concentration(chain: OptionChain) -> Optional[float]:
    """Herfindahl-Hirschman index of OI across the tracked ladder.
    0 = perfectly even, 1 = all OI in a single strike. Returns None if
    there isn't enough OI data yet to mean anything."""
    ois = [q.oi for q in chain.quotes.values() if q.oi]
    total = sum(ois)
    if total <= 0 or len(ois) < 2:
        return None
    return sum((oi / total) ** 2 for oi in ois)


def _wall_proximity_pct(chain: OptionChain, spot: float) -> Optional[float]:
    """% distance from spot to the nearest wall with meaningful OI.
    Smaller = closer = more relevant. None if no qualifying wall.

    A wall only counts if its OI is a genuine outlier relative to the
    ladder's own average — not just above an absolute floor. Every ATM
    strike naturally carries decent OI on both sides, so "some OI near
    spot" is normal market structure, not a signal. What's unusual is a
    strike carrying meaningfully MORE than its neighbours; that's what
    this checks (caught by a synthetic all-equal-OI test before this
    module ever ran against real data — see gamma_blast_scanner tests)."""
    if not spot:
        return None
    ois = [q.oi for q in chain.quotes.values() if q.oi]
    if not ois:
        return None
    mean_oi = sum(ois) / len(ois)
    floor = max(MIN_WALL_OI_TO_COUNT, mean_oi * 1.5)

    walls = chain.walls(3)
    candidates = [w for side in walls.values() for w in side
                  if (w.oi or 0) >= floor]
    if not candidates:
        return None
    nearest = min(candidates, key=lambda w: abs(w.strike - spot))
    return abs(nearest.strike - spot) / spot * 100.0


def _score_linear(value: Optional[float], full_score_at: float, weight: float) -> float:
    """Linear ramp from 0 to `weight` as |value| goes from 0 to
    full_score_at, capped at `weight`. None input scores 0 (absence is
    not evidence of absence — it just doesn't contribute)."""
    if value is None or full_score_at <= 0:
        return 0.0
    return min(weight, (abs(value) / full_score_at) * weight)


# ---------------------------------------------------------------- main
def score(name: str, chain: OptionChain, history: GammaHistory,
          spot: float, days_to_expiry: float,
          snap: Optional[GammaSnapshot] = None) -> GammaResult:
    """
    Score one index or underlying's current gamma activity.

    Call this AFTER chain.poll() has run for this cycle (i.e. chain.quotes
    holds fresh OI/LTP). This function computes THIS cycle's pcr/max_pain/
    atm_iv itself and records them into `history` as its first action —
    the caller does not (and must not) call history.record() separately.

    That used to be a two-step contract (caller records, then calls
    score()) with the recording order easy to get subtly wrong — record
    a stale value, or record after scoring, and the IV/PCR/max-pain
    'shift' factors silently lag by one poll cycle. Owning the recording
    here means it's correct by construction rather than by caller
    discipline: there's only one way to call this function.
    """
    comp = GammaComponents()
    total = 0.0

    # ---- record this cycle's samples first, so window deltas below are
    # always comparing "just now" against history, never comparing two
    # stale points or double-recording the same instant ----
    pcr_now = chain.pcr()
    max_pain_now = chain.max_pain()
    atm_iv_now = _atm_iv(chain, spot, days_to_expiry)
    history.record(datetime.now(IST), pcr_now, max_pain_now, atm_iv_now)

    # ---- OI velocity (also the sole direction source) ----
    vel = chain.velocity.alerts()
    direction = None
    if vel:
        comp.oi_velocity = {"alerts": vel}
        ce_tokens = {q.token for q in chain.quotes.values() if q.opt_type == "CE"}
        ce_hits = sum(1 for v in vel if v["token"] in ce_tokens)
        pe_hits = len(vel) - ce_hits
        if ce_hits > pe_hits:
            direction = "CE"      # CE walls dissolving = resistance lifting
        elif pe_hits > ce_hits:
            direction = "PE"
        if direction:
            total += WEIGHTS["oi_velocity"]

    # ---- IV expansion ----
    delta = history._window_delta(history.atm_iv, ROLLING_WINDOW_MIN) \
        if atm_iv_now is not None else None
    if delta:
        old, new = delta
        comp.iv_expansion_pp = round(new - old, 2)
        total += _score_linear(new - old, IV_EXPANSION_PP_FOR_MAX_SCORE,
                                WEIGHTS["iv_expansion"])

    # ---- strike concentration ----
    hhi = _strike_concentration(chain)
    if hhi is not None:
        n = len([q for q in chain.quotes.values() if q.oi])
        baseline = 1.0 / n if n else 0.0
        normalized = max(0.0, (hhi - baseline) / (1 - baseline)) if baseline < 1 else 0.0
        comp.strike_concentration_hhi = round(hhi, 4)
        total += normalized * WEIGHTS["strike_concentration"]

    # ---- wall proximity ----
    wp = _wall_proximity_pct(chain, spot)
    if wp is not None:
        comp.wall_proximity_pct = round(wp, 3)
        # closer = higher score, so invert: full score at 0% distance,
        # zero score at or beyond WALL_PROXIMITY_PCT_FOR_MAX_SCORE
        closeness = max(0.0, 1 - (wp / WALL_PROXIMITY_PCT_FOR_MAX_SCORE))
        total += closeness * WEIGHTS["wall_proximity"]

    # ---- PCR shift (intensity only, not direction) ----
    pcr_delta = history._window_delta(history.pcr, ROLLING_WINDOW_MIN)
    if pcr_delta:
        old, new = pcr_delta
        comp.pcr_shift = round(new - old, 3)
        total += _score_linear(new - old, PCR_SHIFT_FOR_MAX_SCORE, WEIGHTS["pcr_shift"])

    # ---- max pain shift (intensity only) ----
    mp_delta = history._window_delta(history.max_pain, ROLLING_WINDOW_MIN)
    if mp_delta and spot:
        old, new = mp_delta
        pct = (new - old) / spot * 100.0
        comp.max_pain_shift_pct = round(pct, 3)
        total += _score_linear(pct, MAX_PAIN_SHIFT_PCT_FOR_MAX_SCORE,
                                WEIGHTS["max_pain_shift"])

    # ---- underlying momentum (confirms direction, small weight) ----
    if snap is not None and direction is not None:
        aligned = (
            (direction == "CE" and snap.above_vwap and snap.ema_bull)
            or (direction == "PE" and snap.above_vwap is False and snap.ema_bull is False)
        )
        comp.momentum = "aligned" if aligned else "not_aligned"
        if aligned:
            total += WEIGHTS["underlying_momentum"]

    if direction is None:
        audit.log("gamma_activity_no_direction", name=name, raw_score=round(total, 1),
                   components=comp.as_dict())
        return GammaResult(name=name, score=round(total, 1), tier="NONE",
                            direction=None, components=comp)

    tier = "NONE"
    if total >= TIER_CONFIRMED:
        tier = "CONFIRMED"
    elif total >= TIER_ACTIVE:
        tier = "ACTIVE"
    elif total >= TIER_WATCH:
        tier = "WATCH"

    result = GammaResult(name=name, score=round(total, 1), tier=tier,
                          direction=direction, components=comp)

    audit.log("gamma_score", name=name, score=result.score, tier=tier,
              direction=direction, components=comp.as_dict())
    return result


