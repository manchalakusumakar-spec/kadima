"""
Strike ranking.

An important framing point, stated once and then left alone: this ranks
contracts by *tradeability* - liquidity, spread, delta band, OI positioning
relative to the walls. It does not rank them by expected profit, because
expected profit is not observable from the option chain. A contract can be the
best-constructed expression of a directional view and still lose, because the
view was wrong.

What this protects you from is real and worth having: entering an illiquid
strike with a 4% spread, or a far-OTM lottery ticket with delta 0.12 that needs
a 200-point move to break even. Those are avoidable losses. Direction is not.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import MAX_DELTA, MAX_SPREAD_PCT, MIN_DELTA, MIN_OI, MIN_VOLUME
from .option_chain import OptionChain, OptionQuote
from .greeks import implied_vol_and_greeks


@dataclass
class RankedStrike:
    quote: OptionQuote
    score: float
    reasons: list[str]
    rejected: str | None = None

    def as_dict(self) -> dict:
        q = self.quote
        return {"symbol": q.symbol, "strike": q.strike, "type": q.opt_type,
                "ltp": q.ltp, "oi": q.oi, "volume": q.volume,
                "delta": round(q.delta, 3) if q.delta else None,
                "iv": round(q.iv, 2) if q.iv else None,
                "spread_pct": round(q.spread_pct, 2) if q.spread_pct else None,
                "score": round(self.score, 1), "reasons": self.reasons,
                "rejected": self.rejected,
                "historical_review": getattr(self, 'historical_review', None)}


def rank(chain: OptionChain, direction: str, spot: float,
         days_to_expiry: float) -> list[RankedStrike]:
    """direction: 'CE' for bullish, 'PE' for bearish."""
    out: list[RankedStrike] = []
    walls = chain.walls()
    wall_strikes = {q.strike for q in walls.get("CE", []) + walls.get("PE", [])}

    for q in chain.quotes.values():
        if q.opt_type != direction:
            continue

        # fill Greeks where the broker did not supply them
        if q.delta is None and q.ltp and q.ltp > 0:
            iv, greeks = implied_vol_and_greeks(
                spot=spot, strike=q.strike, premium=q.ltp,
                days=days_to_expiry, opt_type=q.opt_type)
            if greeks:
                q.iv = q.iv or iv
                q.delta, q.gamma = greeks["delta"], greeks["gamma"]
                q.theta, q.vega = greeks["theta"], greeks["vega"]

        reasons, score = [], 0.0
        reject = None

        if (q.oi or 0) < MIN_OI:
            reject = f"OI {q.oi} below floor {MIN_OI}"
        elif (q.volume or 0) < MIN_VOLUME:
            reject = f"volume {q.volume} below floor {MIN_VOLUME}"
        elif q.spread_pct is not None and q.spread_pct > MAX_SPREAD_PCT:
            reject = f"spread {q.spread_pct:.1f}% above {MAX_SPREAD_PCT}%"
        elif q.delta is not None and not (MIN_DELTA <= abs(q.delta) <= MAX_DELTA):
            reject = f"delta {abs(q.delta):.2f} outside {MIN_DELTA}-{MAX_DELTA}"

        if reject:
            out.append(RankedStrike(q, 0.0, [], reject))
            continue

        # ---- liquidity (0-35)
        liq = min(35.0, (q.volume or 0) / MIN_VOLUME * 12)
        score += liq
        reasons.append(f"liquidity +{liq:.0f}")

        # ---- spread quality (0-25)
        sp = q.spread_pct or MAX_SPREAD_PCT
        sq = max(0.0, 25 * (1 - sp / MAX_SPREAD_PCT))
        score += sq
        reasons.append(f"spread {sp:.2f}% +{sq:.0f}")

        # ---- delta centrality (0-25): prefer mid-band ~0.52
        if q.delta is not None:
            centre = (MIN_DELTA + MAX_DELTA) / 2
            dist = abs(abs(q.delta) - centre) / ((MAX_DELTA - MIN_DELTA) / 2)
            dc = max(0.0, 25 * (1 - dist))
            score += dc
            reasons.append(f"delta {abs(q.delta):.2f} +{dc:.0f}")

        # ---- structural: is a wall sitting right on top of us? (-15)
        opposing = walls["CE"] if direction == "CE" else walls["PE"]
        blocking = [w for w in opposing
                    if (direction == "CE" and 0 < w.strike - spot <= 50)
                    or (direction == "PE" and 0 < spot - w.strike <= 50)]
        if blocking:
            score -= 15
            reasons.append(f"wall at {blocking[0].strike} within 50pts -15")

        # ---- OI build in our favour (0-15)
        if (q.oi_change or 0) > 0 and q.strike in wall_strikes:
            score += 15
            reasons.append("OI building at this strike +15")

        out.append(RankedStrike(q, score, reasons))

    out.sort(key=lambda r: r.score, reverse=True)
    from .trade_learning import remember_selection
    remember_selection(chain, direction, spot, days_to_expiry, out)
    return out


def best(chain: OptionChain, direction: str, spot: float,
         days_to_expiry: float) -> RankedStrike | None:
    ranked = [r for r in rank(chain, direction, spot, days_to_expiry)
              if not r.rejected]
    return ranked[0] if ranked else None
