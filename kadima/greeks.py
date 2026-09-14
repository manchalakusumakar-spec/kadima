"""
Black-Scholes Greeks + implied-vol solver.

This exists to close the SENSEX/MIDCAP Greeks gap in the audit. Two caveats
you should hold onto:

1. These are computed, not fed. They depend on an assumed risk-free rate and
   on the model's assumptions holding. Broker-supplied Greeks for NIFTY and
   BANKNIFTY are more reliable - prefer them where available and only fall
   back here.

2. Because SENSEX Greeks are modelled while NIFTY's are fed, the two are not
   strictly comparable. That is exactly why SENSEX carries a lower vote weight
   in the four-index confirmation. Do not raise that weight without first
   checking computed values against a reliable source for a few sessions.
"""
from __future__ import annotations

import math

RISK_FREE = 0.065          # ~India 1yr T-bill; revisit periodically


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def bs_price(spot, strike, days, vol, opt_type, r=RISK_FREE):
    t = max(days, 0.0001) / 365.0
    if vol <= 0 or spot <= 0 or strike <= 0:
        return None
    d1 = (math.log(spot / strike) + (r + 0.5 * vol * vol) * t) / (vol * math.sqrt(t))
    d2 = d1 - vol * math.sqrt(t)
    if opt_type == "CE":
        return spot * _norm_cdf(d1) - strike * math.exp(-r * t) * _norm_cdf(d2)
    return strike * math.exp(-r * t) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def greeks(spot, strike, days, vol, opt_type, r=RISK_FREE) -> dict | None:
    t = max(days, 0.0001) / 365.0
    if vol <= 0:
        return None
    sqrt_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (r + 0.5 * vol * vol) * t) / (vol * sqrt_t)
    d2 = d1 - vol * sqrt_t
    pdf = _norm_pdf(d1)

    if opt_type == "CE":
        delta = _norm_cdf(d1)
        theta = (-(spot * pdf * vol) / (2 * sqrt_t)
                 - r * strike * math.exp(-r * t) * _norm_cdf(d2)) / 365
    else:
        delta = _norm_cdf(d1) - 1
        theta = (-(spot * pdf * vol) / (2 * sqrt_t)
                 + r * strike * math.exp(-r * t) * _norm_cdf(-d2)) / 365

    return {"delta": delta,
            "gamma": pdf / (spot * vol * sqrt_t),
            "theta": theta,
            "vega": spot * pdf * sqrt_t / 100}


def implied_vol_and_greeks(spot, strike, premium, days, opt_type,
                           r=RISK_FREE, tol=1e-4, max_iter=100):
    """Bisection IV solve - slower than Newton but will not diverge."""
    if premium is None or premium <= 0 or spot <= 0:
        return None, None
    lo, hi = 0.001, 5.0
    for _ in range(max_iter):
        mid = (lo + hi) / 2
        price = bs_price(spot, strike, days, mid, opt_type, r)
        if price is None:
            return None, None
        if abs(price - premium) < tol:
            return mid * 100, greeks(spot, strike, days, mid, opt_type, r)
        if price > premium:
            hi = mid
        else:
            lo = mid
    vol = (lo + hi) / 2
    return vol * 100, greeks(spot, strike, days, vol, opt_type, r)
