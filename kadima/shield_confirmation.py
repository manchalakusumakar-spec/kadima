"""One fail-closed Shield definition for display, events and order dispatch.

65 is a weighted score threshold, not a calibrated probability of profit.
Hold evidence comes only from two consecutive completed five-minute bars.
"""
from dataclasses import dataclass, field
from datetime import datetime
import math

SHIELD_MIN_SCORE = 65.0
BANK_NAMES = ('HDFCBANK', 'ICICIBANK', 'KOTAKBANK', 'SBIN')


def fresh(at, now, seconds):
    try:
        return (at.tzinfo is not None and at.date() == now.date()
                and 0 <= (now - at).total_seconds() <= seconds)
    except (AttributeError, TypeError, ValueError):
        return False


def alignment(direction, snap, spot):
    try:
        vwap, fast, slow, rsi = (getattr(snap, k) for k in
                                 ('vwap', 'ema_fast', 'ema_slow', 'rsi'))
        if not all(math.isfinite(v) and v > 0 for v in (spot, vwap, fast, slow)):
            return dict(vwap=False, ema=False, rsi=False)
        if direction not in ('CE', 'PE') or not math.isfinite(rsi) or not 0 <= rsi <= 100:
            return dict(vwap=False, ema=False, rsi=False)
        bull = direction == 'CE'
        return dict(vwap=spot > vwap if bull else spot < vwap,
                    ema=fast > slow if bull else fast < slow,
                    rsi=rsi > 50 if bull else rsi < 50)
    except (AttributeError, TypeError, ValueError):
        return dict(vwap=False, ema=False, rsi=False)


@dataclass(frozen=True)
class ShieldEvidence:
    index: str
    direction: str
    score: float
    snap: object
    spot: float
    closed_at: datetime | None = None
    technical_at: datetime | None = None
    spot_at: datetime | None = None
    prior_closed_at: datetime | None = None
    prior_direction: str | None = None
    prior_ready: bool = False
    oi_confirmed: bool = False
    oi_at: datetime | None = None
    invalidated: bool = False
    banks: dict = field(default_factory=dict)


def confirmation_checks(evidence, now, minimum=SHIELD_MIN_SCORE):
    if not isinstance(evidence, ShieldEvidence):
        return {'evidence': False}
    e = evidence
    try:
        score_ok = (not isinstance(e.score, bool) and math.isfinite(e.score)
                    and SHIELD_MIN_SCORE <= minimum <= 100
                    and minimum <= e.score <= 100)
    except (TypeError, ValueError):
        score_ok = False
    try:
        held = (e.prior_ready is True and e.prior_direction == e.direction
                and e.prior_closed_at.date() == e.closed_at.date()
                and (e.closed_at - e.prior_closed_at).total_seconds() == 300)
    except (AttributeError, TypeError, ValueError):
        held = False
    bank_count = sum(e.banks.get(name) is True for name in BANK_NAMES)
    return dict(score=score_ok, direction=e.direction in ('CE', 'PE'),
                **alignment(e.direction, e.snap, e.spot),
                closed_bar=fresh(e.closed_at, now, 360),
                technical_fresh=fresh(e.technical_at, now, 240),
                spot_fresh=fresh(e.spot_at, now, 30),
                extra_5m_hold=held,
                oi=e.oi_confirmed is True and fresh(e.oi_at, now, 30),
                no_invalidation=e.invalidated is False,
                bank_confirmation=e.index != 'BANKNIFTY' or bank_count >= 2)


def is_shield_confirmed(evidence, now, minimum=SHIELD_MIN_SCORE):
    return all(confirmation_checks(evidence, now, minimum).values())
