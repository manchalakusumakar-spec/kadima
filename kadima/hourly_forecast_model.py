"""Hourly index path forecast: pure maths, no broker, no database, no I/O.

One forecast covers one slot (09:15-10:15 ... 14:15-15:15, 15:15-15:30) and
is a path of 5-minute points from the slot's anchor price. It is built from
three parts, every one of which is computed from data available at the slot
start only:

  1. Analog paths. Past sessions' 5-minute bars near the same time of day
     whose recent shape (returns over 1/3/6/12 bars, day move, gap, place
     in the day's range) looked like today's. Their forward paths, in units
     of that day's volatility, are averaged with similarity, recency and
     time-of-day weights.
  2. Technical tilt. EMA 9/21 on 5-minute closes, RSI 14, price against the
     session's average price and, live only, the Bull Shield direction.
  3. Volatility. Realized 5-minute volatility blended with ATM implied
     volatility, shaped by the measured time-of-day profile, gives the band.

Calibration is learned, never assumed. Coefficients start from cautious
defaults and are refitted from resolved slots with a ridge prior worth
MIN_SAMPLES slots, so with fewer than ~30 resolved slots the defaults still
dominate (the trader's own "30 resolved occurrences" rule).

A probability here is a model estimate, calibrated against this model's own
record. It is not a guarantee and it is never an order.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

STEP_MIN = 5
BARS_PER_DAY = 75              # 09:15 -> 15:30 in 5-minute bars
TRADING_DAYS_PER_YEAR = 252
SIGMA_LOOKBACK = 24            # 2 hours of 5-minute returns
ANALOG_K = 25
ANALOG_BAR_WINDOW = 6          # +/- 30 minutes around the slot start
ANALOG_MIN_DAYS = 5
ANALOG_MIN_NEFF = 8.0
RECENCY_HALFLIFE_DAYS = 45.0
MIN_SAMPLES = 30               # ridge prior strength, in resolved slots
FEATURE_WEIGHTS = (0.5, 1.0, 1.0, 0.8, 0.8, 0.6, 0.6)
FLAT_BAND = 0.05               # |p_up - 0.5| below this reads as FLAT
MODEL_VERSION = 'hf-2.0'
FLAT_SCORE_PCT = 0.10        # FLAT counts as a hit if the slot moved less than this

DEFAULT_PARAMS = dict(
    beta_reg=0.5,       # weight on the pooled regression end move (sigma units)
    beta_analog=0.15,   # weight on the analog mean path; cut from 0.3 after 9 Oct showed a fixed bias
    beta_tech=0.15,     # EMA/RSI/TWAP tilt, sigma*sqrt(n) units
    beta_shield=0.10,   # live Bull Shield direction x score
    beta_trend=0.05,    # day move so far / opening gap (new: starts small, earns weight)
    beta_oi=0.05,       # option-chain PCR change and OI wall shifts (new: starts small)
    band_only=False,    # set by the self-audit when no part shows skill: show the range only
    band_z=1.0,         # band half-width in model sds (1.0 ~ 68%)
    prob_c=0.7,         # probability sharpness around 0.5 (cautious until calibrated)
    n_samples=0,
    version=MODEL_VERSION,
)

SLOTS = (((9, 15), (10, 15)), ((10, 15), (11, 15)), ((11, 15), (12, 15)),
         ((12, 15), (13, 15)), ((13, 15), (14, 15)), ((14, 15), (15, 15)),
         ((15, 15), (15, 30)))


# ---------------------------------------------------------------- helpers
def finite(x):
    return (not isinstance(x, bool) and isinstance(x, (int, float))
            and math.isfinite(x))


def clip(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def norm_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def slot_label(hm):
    return f'{hm[0]:02d}:{hm[1]:02d}'


def bar_index(hm):
    return ((hm[0] * 60 + hm[1]) - (9 * 60 + 15)) // STEP_MIN


def slot_steps(start, end):
    return bar_index(end) - bar_index(start)


def slot_for(t):
    """(start, end) of the slot containing wall time t (a datetime.time), or None."""
    m = t.hour * 60 + t.minute
    for start, end in SLOTS:
        if start[0] * 60 + start[1] <= m < end[0] * 60 + end[1]:
            return start, end
    return None


@dataclass
class Day:
    """One session of 5-minute bars, index 0 = 09:15. Missing bars are None."""
    day: date
    opens: list
    highs: list
    lows: list
    closes: list
    _rets: list = field(default=None, repr=False, compare=False)

    def complete_until(self, j):
        return all(self.closes[x] is not None for x in range(j))


def build_days(rows):
    """rows: iterable of (start datetime, open, high, low, close), any order."""
    by_day = {}
    for start, o, h, lo, c in rows:
        if not all(finite(v) and v > 0 for v in (o, h, lo, c)):
            continue
        j = ((start.hour * 60 + start.minute) - (9 * 60 + 15)) // STEP_MIN
        if not 0 <= j < BARS_PER_DAY:
            continue
        d = by_day.setdefault(start.date(), Day(start.date(), *[[None] * BARS_PER_DAY for _ in range(4)]))
        d.opens[j], d.highs[j], d.lows[j], d.closes[j] = o, h, lo, c
    return [by_day[k] for k in sorted(by_day)]


def bar_returns(d):
    """Intraday 5-minute log returns; bar 0 is open->close so the gap is excluded."""
    if d._rets is not None:
        return d._rets
    out = [None] * BARS_PER_DAY
    for j in range(BARS_PER_DAY):
        c = d.closes[j]
        prev = d.opens[0] if j == 0 else d.closes[j - 1]
        if c is not None and prev is not None:
            out[j] = math.log(c / prev)
    d._rets = out
    return out


def robust_sigma(rets):
    rets = [r for r in rets if r is not None]
    if len(rets) < 6:
        return None
    rms = math.sqrt(sum(r * r for r in rets) / len(rets))
    med = sorted(rets)[len(rets) // 2]
    mad = sorted(abs(r - med) for r in rets)[len(rets) // 2] * 1.4826
    s = 0.5 * rms + 0.5 * mad if mad > 0 else rms
    return s if s > 1e-6 else None


def tod_profile(days):
    """Relative 5-minute volatility by bar of day, shrunk to 1.0 when data is thin."""
    default = [1.6 if j < 3 else 1.3 if j < 6 else 1.15 if j < 12 else
               1.1 if j >= 72 else 1.0 for j in range(BARS_PER_DAY)]
    sums, counts = [0.0] * BARS_PER_DAY, [0] * BARS_PER_DAY
    for d in days:
        for j, r in enumerate(bar_returns(d)):
            if r is not None:
                sums[j] += abs(r)
                counts[j] += 1
    n = min(counts) if counts else 0
    if n == 0:
        return default
    raw = [sums[j] / counts[j] for j in range(BARS_PER_DAY)]
    mean = sum(raw) / len(raw)
    if mean <= 0:
        return default
    raw = [x / mean for x in raw]
    smooth = [sum(raw[max(0, j - 1):j + 2]) / len(raw[max(0, j - 1):j + 2]) for j in range(BARS_PER_DAY)]
    w = n / (n + 10.0)
    return [w * s + (1 - w) * dft for s, dft in zip(smooth, default)]


def sigma_at(days, di, i, tod):
    """Per-bar base sigma (tod-normalised) from the last SIGMA_LOOKBACK returns before bar i."""
    rets, mults = [], []
    k, j = di, i - 1
    while k >= 0 and len(rets) < SIGMA_LOOKBACK:
        if j < 0:
            k, j = k - 1, BARS_PER_DAY - 1
            continue
        r = bar_returns(days[k])[j]
        if r is not None:
            rets.append(r)
            mults.append(tod[j])
        j -= 1
    s = robust_sigma(rets)
    if s is None:
        return None
    norm = math.sqrt(sum(m * m for m in mults) / len(mults))
    return s / norm if norm > 0 else s


def closes_before(days, di, i, count):
    """Last `count` closes strictly before bar i of day di, walking back across days."""
    out = []
    k, j = di, i - 1
    while k >= 0 and len(out) < count:
        if j < 0:
            k, j = k - 1, BARS_PER_DAY - 1
            continue
        c = days[k].closes[j]
        if c is not None:
            out.append(c)
        j -= 1
    return list(reversed(out))


def prev_close(days, di):
    for k in range(di - 1, -1, -1):
        cl = [c for c in days[k].closes if c is not None]
        if cl:
            return cl[-1]
    return None


# ---------------------------------------------------------------- features
def features(days, di, i, anchor, sigma):
    """Shape of the market at the slot start, in sigma units, clipped to +/-4."""
    d = days[di]
    hist = closes_before(days, di, i, 13)
    if len(hist) < 13 or not finite(anchor) or not sigma:
        return None

    def r(L):
        # hist[-1] is the bar that just closed (~ the anchor), so the last L
        # completed bars span hist[-L-1] -> anchor.
        return math.log(anchor / hist[-L - 1]) / (sigma * math.sqrt(L))

    day_open = d.opens[0] if d.opens[0] is not None else anchor
    pc = prev_close(days, di)
    gap = math.log(day_open / pc) / (sigma * math.sqrt(12)) if pc else 0.0
    dayret = math.log(anchor / day_open) / (sigma * math.sqrt(max(i, 1))) if i > 0 else 0.0
    if i > 0:
        hs = [x for x in d.highs[:i] if x is not None] + [anchor]
        ls = [x for x in d.lows[:i] if x is not None] + [anchor]
        rng = max(hs) - min(ls)
        pos = ((anchor - min(ls)) / rng) * 2 - 1 if rng > 0 else 0.0
    else:
        pos = 0.0
    return [clip(v, -4, 4) for v in (r(1), r(3), r(6), r(12), dayret, gap, pos)]


def forward_path(d, i, n, sigma):
    if i + n > BARS_PER_DAY or d.opens[i] is None:
        return None
    path = []
    for k in range(1, n + 1):
        c = d.closes[i + k - 1]
        if c is None:
            return None
        path.append(math.log(c / d.opens[i]) / sigma)
    return path


@dataclass
class AnalogResult:
    mean: list
    sd: list
    up_frac: float
    n_eff: float
    used: int
    reg_end: float = 0.0       # ridge-regression end move, sigma units
    pool: int = 0
    best: list = None          # forward path of the closest past match, sigma units
    best_from: str = None      # that match's day and bar time, e.g. '2026-10-08 11:15'


def solve(a, b):
    """Gaussian elimination with partial pivoting; returns None if singular."""
    n = len(b)
    m = [row[:] + [b[r]] for r, row in enumerate(a)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[piv][c]) < 1e-12:
            return None
        m[c], m[piv] = m[piv], m[c]
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                for k in range(c, n + 1):
                    m[r][k] -= f * m[c][k]
    return [m[r][n] / m[r][r] for r in range(n)]


def ridge(xs, ys, ws, lam):
    """Weighted ridge regression without intercept (features are centred moves)."""
    k = len(xs[0])
    a = [[sum(w * x[i] * x[j] for x, w in zip(xs, ws)) + (lam if i == j else 0.0)
          for j in range(k)] for i in range(k)]
    b = [sum(w * x[i] * y for x, y, w in zip(xs, ys, ws)) for i in range(k)]
    return solve(a, b)


def analogs(days, di, i, n, feat, tod, as_of):
    """Weighted nearest-neighbour forward paths from sessions strictly before day di."""
    if di < ANALOG_MIN_DAYS or feat is None:
        return None
    cands = []
    for k in range(di):
        dk = days[k]
        for j in range(max(0, i - ANALOG_BAR_WINDOW), min(BARS_PER_DAY - n, i + ANALOG_BAR_WINDOW) + 1):
            if j == 0 and i != 0:
                continue    # the open bar behaves differently; match it only to 09:15
            s = sigma_at(days, k, j, tod)
            if s is None or dk.opens[j] is None:
                continue
            f = features(days, k, j, dk.opens[j], s)
            p = forward_path(dk, j, n, s)
            if f is None or p is None:
                continue
            dist = math.sqrt(sum(w * (a - b) ** 2 for w, a, b in zip(FEATURE_WEIGHTS, f, feat)))
            age = max(0, (as_of - dk.day).days)
            cands.append((dist, abs(j - i), age, p, f, dk.day, j))
    if len(cands) < 10:
        return None
    # Pooled ridge regression of the end move on all features: statistically
    # far more efficient than a 25-path average, which mainly supplies shape.
    rw = [0.5 ** (c[2] / RECENCY_HALFLIFE_DAYS) * math.exp(-c[1] / 6.0) for c in cands]
    coef = ridge([c[4] for c in cands], [c[3][-1] for c in cands], rw,
                 lam=max(5.0, 0.25 * sum(rw)))
    reg_end = sum(cf * x for cf, x in zip(coef, feat)) if coef else 0.0
    cands.sort(key=lambda c: c[0])
    near = cands[:ANALOG_K]
    h = max(near[len(near) // 2][0], 1e-6)
    ws = [math.exp(-0.5 * (dist / h) ** 2) * math.exp(-dj / 6.0)
          * 0.5 ** (age / RECENCY_HALFLIFE_DAYS) for dist, dj, age, *_ in near]
    tw = sum(ws)
    if tw <= 0:
        return None
    mean = [sum(w * c[3][k] for w, c in zip(ws, near)) / tw for k in range(n)]
    sd = [math.sqrt(sum(w * (c[3][k] - mean[k]) ** 2 for w, c in zip(ws, near)) / tw) for k in range(n)]
    up = sum(w for w, c in zip(ws, near) if c[3][-1] > 0) / tw
    neff = tw * tw / sum(w * w for w in ws)
    b = near[0]
    hm = 9 * 60 + 15 + STEP_MIN * b[6]
    return AnalogResult(mean, sd, up, neff, len(near), clip(reg_end, -3, 3), len(cands),
                        best=list(b[3]), best_from=f'{b[5].isoformat()} {hm // 60:02d}:{hm % 60:02d}')


# ---------------------------------------------------------------- technical tilt
def ema(values, period):
    if len(values) < period:
        return None
    a = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = a * v + (1 - a) * e
    return e


def rsi(values, period=14):
    if len(values) < period + 1:
        return None
    gains = losses = 0.0
    for a, b in zip(values[-period - 1:-1], values[-period:]):
        ch = b - a
        gains += max(ch, 0)
        losses += max(-ch, 0)
    if gains + losses == 0:
        return 50.0
    return 100.0 * gains / (gains + losses)


def tech_tilt(days, di, i, anchor, sigma, shield_dir=None, shield_score=None):
    """-1..+1 directional tilt and its parts. Shield is a live-only input."""
    closes = closes_before(days, di, i, 60) + [anchor]
    parts, weights = {}, {}
    e9, e21 = ema(closes, 9), ema(closes, 21)
    if e9 is not None and e21 is not None:
        parts['ema'] = clip((e9 - e21) / (anchor * sigma * 2), -1, 1)
        weights['ema'] = 0.25
    rv = rsi(closes, 14)
    if rv is not None:
        parts['rsi'] = clip((rv - 50) / 25, -1, 1)
        weights['rsi'] = 0.2
    today = [c for c in days[di].closes[:i] if c is not None]
    if len(today) >= 3:
        twap = sum(today) / len(today)
        parts['twap'] = clip((anchor - twap) / (anchor * sigma * math.sqrt(6)), -1, 1)
        weights['twap'] = 0.2
    if shield_dir in ('CE', 'PE') and finite(shield_score):
        parts['shield'] = (1 if shield_dir == 'CE' else -1) * clip(shield_score / 100, 0, 1)
    tw = sum(weights.values())
    m = sum(parts[k] * weights[k] for k in weights) / tw if tw else 0.0
    return clip(m, -1, 1), parts


def trend_tilt(days, di, i, anchor, sigma):
    """Day move so far (or the opening gap at 09:15), -1..+1. Trend days keep going more often than not."""
    d = days[di]
    day_open = d.opens[0] if d.opens[0] is not None else anchor
    if i > 0:
        return clip(math.log(anchor / day_open) / (sigma * math.sqrt(i)) / 2, -1, 1)
    pc = prev_close(days, di)
    return clip(math.log(day_open / pc) / (sigma * math.sqrt(12)) / 2, -1, 1) if pc else 0.0


def oi_tilt(oi):
    """Option-chain tilt, -1..+1, + = bullish: PCR rising (puts written below), CE/PE OI walls moving up.

    oi: dict(pcr_now, pcr_prev, ce_wall_now, ce_wall_prev, pe_wall_now, pe_wall_prev, step).
    The sign convention is a starting point only; the self-audit learns beta_oi, including its sign.
    """
    if not oi:
        return 0.0, {}
    parts = {}
    if finite(oi.get('pcr_now')) and finite(oi.get('pcr_prev')):
        parts['pcr'] = clip((oi['pcr_now'] - oi['pcr_prev']) / 0.2, -1, 1)
    step = oi.get('step') or 50
    for k in ('ce', 'pe'):
        a, b = oi.get(k + '_wall_now'), oi.get(k + '_wall_prev')
        if finite(a) and finite(b):
            parts[k + '_wall'] = clip((a - b) / step, -1, 1)
    w = dict(pcr=0.5, ce_wall=0.25, pe_wall=0.25)
    if finite(oi.get('pcr_now')) and finite(oi.get('prev_day_pcr')):
        parts['prev_day_pcr'] = clip((oi['pcr_now']-oi['prev_day_pcr'])/0.2,-1,1)
        w['prev_day_pcr'] = 0.25
    for k in ('ce','pe'):
        a,b = oi.get(k+'_wall_now'),oi.get('prev_day_'+k+'_wall')
        if finite(a) and finite(b):
            parts['prev_day_'+k+'_wall'] = clip((a-b)/step,-1,1)
            w['prev_day_'+k+'_wall'] = 0.125
    if finite(oi.get('oi_build')):
        parts['oi_build'] = clip(oi['oi_build']/0.2,-1,1)
        w['oi_build'] = 0.35
    if finite(oi.get('max_pain')) and finite(oi.get('spot')):
        parts['max_pain'] = clip((oi['max_pain']-oi['spot'])/step,-1,1)
        w['max_pain'] = 0.15
    tw = sum(w[k] for k in parts)
    return (clip(sum(parts[k] * w[k] for k in parts) / tw, -1, 1) if tw else 0.0), parts


# ---------------------------------------------------------------- forecast
@dataclass
class Forecast:
    anchor: float
    sigma: float
    steps: int
    points: list            # [(k, mean, upper, lower)] prices, k = 1..steps
    prob_up: float
    direction: str          # UP / DOWN / FLAT
    projected_pct: float
    projected_pts: float
    band_pts: float         # half-width at slot end
    n_eff: float
    inputs: dict = field(default_factory=dict)   # raw regressors for later refitting
    swing: list = field(default_factory=list)    # expected 5-minute ups and downs, prices, k = 1..steps


def iv_sigma(iv_pct):
    if not finite(iv_pct) or not 3 <= iv_pct <= 150:
        return None
    return iv_pct / 100.0 / math.sqrt(TRADING_DAYS_PER_YEAR * BARS_PER_DAY)


def make_forecast(days, di, i, n, anchor, *, as_of, tod=None, params=None,
                  atm_iv=None, shield_dir=None, shield_score=None, oi=None):
    """Forecast n 5-minute points from bar i of days[di], using nothing at or after bar i."""
    params = {**DEFAULT_PARAMS, **(params or {})}
    tod = tod or tod_profile(days[:di])
    s_real = sigma_at(days, di, i, tod)
    s_iv = iv_sigma(atm_iv)
    if s_iv is not None:
        s_iv /= math.sqrt(sum(t * t for t in tod) / len(tod))
    if s_real and s_iv:
        sigma = math.sqrt(0.5 * s_real ** 2 + 0.5 * s_iv ** 2)
    else:
        sigma = s_real or s_iv
    if not sigma or not finite(anchor) or anchor <= 0:
        return None
    feat = features(days, di, i, anchor, sigma)
    an = analogs(days, di, i, n, feat, tod, as_of)
    m, parts = tech_tilt(days, di, i, anchor, sigma, shield_dir, shield_score)
    shield = parts.get('shield', 0.0)
    trend = trend_tilt(days, di, i, anchor, sigma)
    oit, oi_parts = oi_tilt(oi)

    mults = [tod[min(i + k, BARS_PER_DAY - 1)] for k in range(n)]
    model_sd = [math.sqrt(sum(x * x for x in mults[:k + 1])) for k in range(n)]
    use_an = an is not None and an.n_eff >= ANALOG_MIN_NEFF
    a_mean = an.mean if use_an else [0.0] * n
    reg_end = an.reg_end if an is not None else 0.0
    sd = ([math.sqrt(0.5 * ms ** 2 + 0.5 * asd ** 2) for ms, asd in zip(model_sd, an.sd)]
          if use_an else model_sd)
    root = math.sqrt(n)
    x = dict(x_reg=reg_end, x_analog=a_mean[-1], x_tech=m * root, x_shield=shield * root,
             x_trend=trend * root, x_oi=oit * root)
    ramp = (x['x_reg'] * params['beta_reg'] + x['x_tech'] * params['beta_tech']
            + x['x_shield'] * params['beta_shield'] + x['x_trend'] * params['beta_trend']
            + x['x_oi'] * params['beta_oi'])
    mean = [ramp * (k + 1) / n + params['beta_analog'] * a_mean[k] for k in range(n)]
    if params.get('band_only'):
        mean = [0.0] * n
    z = params['band_z']
    points = [(k + 1,
               anchor * math.exp(mean[k] * sigma),
               anchor * math.exp((mean[k] + z * sd[k]) * sigma),
               anchor * math.exp((mean[k] - z * sd[k]) * sigma)) for k in range(n)]

    # Expected swings: the closest past match's 5-minute ups and downs, detrended so it starts
    # and ends on the average path, laid over that path. Display only; the average path above
    # is what is scored and calibrated.
    best = an.best if an is not None and an.best and len(an.best) == n else None
    wig = [best[k] - best[-1] * (k + 1) / n for k in range(n)] if best else [0.0] * n
    swing = [round(anchor * math.exp((mean[k] + wig[k]) * sigma), 2) for k in range(n)]

    p_model = norm_cdf(mean[-1] / sd[-1]) if sd[-1] > 0 else 0.5
    p_raw = 0.5 * p_model + 0.5 * an.up_frac if use_an else p_model
    p = 0.5 if params.get('band_only') else clip(0.5 + params['prob_c'] * (p_raw - 0.5), 0.05, 0.95)
    direction = 'UP' if p >= 0.5 + FLAT_BAND else 'DOWN' if p <= 0.5 - FLAT_BAND else 'FLAT'
    end = points[-1][1]
    return Forecast(
        anchor=anchor, sigma=sigma, steps=n, points=points, prob_up=round(p, 4),
        direction=direction, projected_pct=round((end / anchor - 1) * 100, 3),
        projected_pts=round(end - anchor, 2),
        band_pts=round((points[-1][2] - points[-1][3]) / 2, 2), n_eff=round(an.n_eff, 1) if an else 0.0,
        swing=swing,
        inputs=dict(x, p_raw=p_raw, swing_from=an.best_from if best else None, sd=sd, mean=mean, sd_end=sd[-1], p_model=p_model,
                    sigma=sigma, sigma_real=s_real, sigma_iv=s_iv, tech=m, tech_parts=parts,
                    trend=trend, oi=oit, oi_parts=oi_parts, oi_features=dict(oi or {}), band_only=bool(params.get('band_only')),
                    analog_used=bool(use_an), analog_up=an.up_frac if an else None,
                    features=feat, params=dict(params)))


# ---------------------------------------------------------------- scoring
def score(points, anchor, actual, direction, sigma, sd_end):
    """points: [(k, mean, upper, lower)]; actual: [price or None] aligned with points."""
    pairs = [(p, a) for p, a in zip(points, actual) if a is not None]
    if not pairs or actual[-1] is None:
        return None
    end_move = actual[-1] - anchor
    flat_tol = FLAT_SCORE_PCT / 100 * anchor
    if direction == 'UP':
        hit = end_move > 0
    elif direction == 'DOWN':
        hit = end_move < 0
    else:
        hit = abs(end_move) <= flat_tol
    return dict(dir_hit=bool(hit),
                mae_pts=round(sum(abs(a - p[1]) for p, a in pairs) / len(pairs), 2),
                coverage=round(sum(1 for p, a in pairs if p[3] <= a <= p[2]) / len(pairs), 3),
                end_error_pts=round(actual[-1] - points[-1][1], 2),
                actual_pct=round((actual[-1] / anchor - 1) * 100, 3))


# ---------------------------------------------------------------- calibration
BETAS = (('beta_reg', 'x_reg', -0.5, 1.5), ('beta_analog', 'x_analog', -0.5, 1.5),
         ('beta_tech', 'x_tech', -0.5, 0.5), ('beta_shield', 'x_shield', -0.5, 0.5),
         ('beta_trend', 'x_trend', -0.5, 0.5), ('beta_oi', 'x_oi', -0.5, 0.5))
COMPONENTS = tuple(b[1] for b in BETAS)


def fit_params(samples, prior=None):
    """Refit coefficients from resolved slots, with a MIN_SAMPLES-strength prior.

    sample: dict(x_reg, x_analog, x_tech, x_shield, x_trend, x_oi, y (end log-return / sigma),
                 p_raw, up (bool), zs (|actual-mean|/sd per point)). Missing parts count as 0.
    """
    prior = {**DEFAULT_PARAMS, **(prior or {})}
    keys = [b[1] for b in BETAS]
    s = [dict(x, **{k: x.get(k) if finite(x.get(k)) else 0.0 for k in keys})
         for x in samples if finite(x.get('y')) and finite(x.get('p_raw'))]
    n = len(s)
    out = dict(prior, n_samples=n, version=MODEL_VERSION, band_only=bool(prior.get('band_only')))
    if n == 0:
        return out
    lam = float(MIN_SAMPLES)
    # Ridge regression of y on the regressors, shrunk toward the prior betas:
    # minimise sum (y - b.x)^2 + lam * sum v_i (b_i - prior_i)^2.
    var = [max(sum(x[k] ** 2 for x in s) / n, 1e-6) for k in keys]
    a = [[sum(x[ki] * x[kj] for x in s) + (lam * var[i] if i == j else 0.0)
          for j, kj in enumerate(keys)] for i, ki in enumerate(keys)]
    b = [sum(x[k] * x['y'] for x in s) + lam * var[i] * prior[BETAS[i][0]]
         for i, k in enumerate(keys)]
    coef = solve(a, b)
    if coef:
        for (name, _, lo, hi), c in zip(BETAS, coef):
            out[name] = round(clip(c, lo, hi), 4)
    # Band width: the 68th percentile of standardized errors, shrunk to the prior.
    zs = sorted(z for x in s for z in (x.get('zs') or []) if finite(z))
    if zs:
        q = zs[min(len(zs) - 1, int(0.6827 * len(zs)))]
        out['band_z'] = round(clip((n * q + lam * prior['band_z']) / (n + lam), 0.6, 2.0), 4)
    # Probability sharpness: regress (outcome - 0.5) on (p_raw - 0.5).
    sxx = sum((x['p_raw'] - 0.5) ** 2 for x in s)
    sxy = sum((x['p_raw'] - 0.5) * ((1.0 if x['up'] else 0.0) - 0.5) for x in s)
    prior_x = lam * 0.01
    out['prob_c'] = round(clip((sxy + prior_x * prior['prob_c']) / (sxx + prior_x), 0.0, 1.5), 4)
    return out


# ---------------------------------------------------------------- self-audit
MAX_STEP = 0.10            # largest change to any weight per recalibration
EVAL_MIN = 20              # slots needed before a challenger may replace the champion
BAND_ONLY_BELOW = 0.52     # direction hit rate (>= 30 directional calls) that switches to range-only
BAND_ONLY_EXIT = 0.55      # hit rate needed to show direction again


def replay(sample, params):
    """Direction and probability this sample would have received under `params`."""
    m = sum(params.get(b[0], 0.0) * (sample.get(b[1]) or 0.0) for b in BETAS)
    sd = sample.get('sd_end') or 1.0
    p = clip(0.5 + params.get('prob_c', 1.0) * (norm_cdf(m / sd) - 0.5), 0.05, 0.95)
    return ('UP' if p >= 0.5 + FLAT_BAND else 'DOWN' if p <= 0.5 - FLAT_BAND else 'FLAT'), p, m


def evaluate(samples, params):
    """Hit rate on directional calls, band coverage and log-loss for a set of resolved slots."""
    calls = hits = 0
    ll = 0.0
    inside = total = 0
    z = params.get('band_z', 1.0)
    for s in samples:
        d, p, _ = replay(s, params)
        up = bool(s.get('up'))
        ll -= math.log(p if up else 1 - p)
        if d != 'FLAT':
            calls += 1
            hits += (d == 'UP') == up
        for e in s.get('zs') or []:
            total += 1
            inside += e <= z
    n = len(samples)
    return dict(n=n, calls=calls, hit=hits / calls if calls else None,
                coverage=inside / total if total else None, logloss=ll / n if n else None)


def component_scorecard(samples):
    """How often each part pointed the right way on its own (|signal| > 0.05)."""
    out = {}
    for _, key, _, _ in BETAS:
        right = used = 0
        for s in samples:
            v = s.get(key)
            if finite(v) and abs(v) > 0.05 and finite(s.get('y')) and s['y'] != 0:
                used += 1
                right += (v > 0) == (s['y'] > 0)
        out[key[2:]] = dict(n=used, hit=round(right / used, 3) if used else None)
    return out


def step_toward(champion, challenger, max_step=MAX_STEP):
    """Move each weight at most max_step toward the challenger, so one bad day cannot swing the model."""
    out = dict(champion)
    for name, _, lo, hi in BETAS:
        a, b = champion.get(name, DEFAULT_PARAMS[name]), challenger.get(name, DEFAULT_PARAMS[name])
        out[name] = round(clip(a + clip(b - a, -max_step, max_step), lo, hi), 4)
    for name, lo, hi, step in (('band_z', 0.6, 2.0, 0.1), ('prob_c', 0.0, 1.5, 0.15)):
        a, b = champion.get(name, DEFAULT_PARAMS[name]), challenger.get(name, DEFAULT_PARAMS[name])
        out[name] = round(clip(a + clip(b - a, -step, step), lo, hi), 4)
    out['n_samples'] = challenger.get('n_samples', 0)
    out['version'] = MODEL_VERSION
    return out


def _better(a, b):
    """Why challenger b beats champion a on the holdout, or '' if it does not.

    No measure may get materially worse, and at least one must clearly improve.
    """
    cov_a = abs((a['coverage'] if a['coverage'] is not None else 0.68) - 0.68)
    cov_b = abs((b['coverage'] if b['coverage'] is not None else 0.68) - 0.68)
    hit_a, hit_b = a['hit'] or 0.5, b['hit'] or 0.5
    if b['logloss'] > a['logloss'] + 0.005 or hit_b < hit_a - 0.02 or cov_b > cov_a + 0.02:
        return ''
    gains = []
    if b['logloss'] < a['logloss'] - 0.002:
        gains.append(f"probability error {a['logloss']:.3f}\u2192{b['logloss']:.3f}")
    if hit_b >= hit_a + 0.02:
        gains.append(f'direction hits {hit_a:.0%}\u2192{hit_b:.0%}')
    if cov_b <= cov_a - 0.02:
        gains.append(f"band holds {a['coverage']:.0%}\u2192{b['coverage']:.0%} (target 68%)")
    return ', '.join(gains)


def recalibrate(champion, train, holdout, prior=None):
    """Champion/challenger: adopt new weights only if they do at least as well on recent unseen slots.

    train: resolved slots used to fit the challenger. holdout: the most recent slots, never
    used for fitting, on which both are compared. Returns (params, decision dict).
    """
    champion = {**DEFAULT_PARAMS, **champion}
    challenger = step_toward(champion, fit_params(train, prior=prior or champion))
    challenger['band_only'] = champion.get('band_only', False)
    a, b = evaluate(holdout, champion), evaluate(holdout, challenger)
    decision = dict(holdout=len(holdout), champion=a, challenger=b, adopted=False, reason='')
    if len(holdout) < EVAL_MIN:
        decision['reason'] = f'kept: only {len(holdout)} recent slots (need {EVAL_MIN})'
        chosen = champion
    elif a['logloss'] is not None and b['logloss'] is not None and _better(a, b):
        decision.update(adopted=True, reason='adopted: ' + _better(a, b))
        chosen = challenger
    else:
        decision['reason'] = 'kept: new weights did not beat current ones on recent slots'
        chosen = champion
    # Range-only fallback, with hysteresis, judged on the chosen weights.
    r = evaluate(holdout, dict(chosen, band_only=False))
    if r['calls'] >= 30 and r['hit'] is not None:
        if not chosen.get('band_only') and r['hit'] < BAND_ONLY_BELOW:
            chosen = dict(chosen, band_only=True)
            decision['reason'] += f"; direction hidden: {r['hit']:.0%} on {r['calls']} calls"
        elif chosen.get('band_only') and r['hit'] >= BAND_ONLY_EXIT:
            chosen = dict(chosen, band_only=False)
            decision['reason'] += f"; direction shown again: {r['hit']:.0%} on {r['calls']} calls"
    decision['band_only'] = bool(chosen.get('band_only'))
    return chosen, decision


# ---------------------------------------------------------------- deviation reasons
REASONS = {
    'engine_down': 'Engine was not running at the slot start (late or missed forecast)',
    'small_move': 'Market barely moved (< 0.10%): noise, not a model failure',
    'trend_day_against': 'Trend day: model leaned against a day move already above 0.8%',
    'band_breach': 'Move was larger than the volatility band allowed',
    'oi_shift_missed': 'Option chain shifted with the move (PCR or OI walls) but the forecast disagreed',
    'component_bias': 'One part of the model leaned the same way all day',
    'opening_gap': 'Opening gap or overnight news set the first hour',
    'reversal': 'Price reversed within the slot after first moving the forecast way',
}


def deviation_reasons(slot, day_pct, actual_pct, coverage, direction, hit, late, oi_move=None,
                      first_half_pct=None, gap_pct=None):
    """Rule-based tags explaining a slot's deviation. Inputs are plain numbers; returns tag list."""
    tags = []
    if late:
        tags.append('engine_down')
    if actual_pct is not None and abs(actual_pct) < FLAT_SCORE_PCT:
        tags.append('small_move')
    if hit:
        return tags
    fc_sign = 1 if direction == 'UP' else -1 if direction == 'DOWN' else 0
    if day_pct is not None and abs(day_pct) >= 0.8 and actual_pct is not None and fc_sign * day_pct < 0 \
            and actual_pct * day_pct > 0:
        tags.append('trend_day_against')
    if coverage is not None and coverage < 0.6:
        tags.append('band_breach')
    if oi_move is not None and actual_pct is not None and abs(oi_move) >= 0.3 \
            and oi_move * actual_pct > 0 and fc_sign * actual_pct <= 0:
        tags.append('oi_shift_missed')
    if slot == '09:15' and gap_pct is not None and abs(gap_pct) >= 0.5:
        tags.append('opening_gap')
    if first_half_pct is not None and actual_pct is not None and fc_sign != 0 \
            and first_half_pct * fc_sign > 0 and actual_pct * fc_sign < 0:
        tags.append('reversal')
    return tags


def day_component_bias(rows):
    """Parts that leaned the same way (|x| >= 0.3) in every slot of the day (>= 3 slots)."""
    out = []
    for key in COMPONENTS:
        vals = [r.get(key) for r in rows if finite(r.get(key))]
        if len(vals) >= 3 and (all(v >= 0.3 for v in vals) or all(v <= -0.3 for v in vals)):
            out.append(key[2:])
    return out
