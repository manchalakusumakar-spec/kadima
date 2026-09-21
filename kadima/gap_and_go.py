"""Gap-and-Go detector, paper ledger and guarded live intent helpers.

Detection remains pure. Live orders, when explicitly enabled, must still travel
through KADIMA's shared AutoExecutor/ledger; this module never talks to Angel One
directly. Thresholds are hypotheses, not probabilities or profit guarantees.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from enum import Enum
import math
import os


class Stage(str, Enum):
    WATCH = "WATCH"
    GAP_DETECTED = "GAP_DETECTED"
    IGNITION = "IGNITION"
    PULLBACK_HOLD = "PULLBACK_HOLD"
    ORB_BREAK = "ORB_BREAK"
    CONFIRMED = "CONFIRMED"
    ENTRY = "ENTRY"


@dataclass(frozen=True)
class Rules:
    min_gap_pct: float = 1.0
    min_impulse_pct: float = 0.35
    min_relative_volume: float = 2.0
    gap_retained_fraction: float = 0.50
    max_pullback_fraction: float = 0.50
    max_pullback_volume_ratio: float = 0.80
    min_rr: float = 2.0
    vwap_hold_tolerance: float = 0.001
    wall_reject_ratio: float = 1.50
    wall_penalty_ratio: float = 1.20
    wall_distance_points: float = 50.0


SCAN_START = time(9, 15)
SCAN_END = time(10, 30)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _positive(value):
    return _finite(value) and value > 0


def _closed_bar(row):
    if not isinstance(row, dict):
        return False
    try:
        ts = datetime.fromisoformat(row["start_ts"])
    except (KeyError, TypeError, ValueError):
        return False
    values = [row.get(k) for k in ("open", "high", "low", "close")]
    if ts.tzinfo is None or not all(_positive(v) for v in values):
        return False
    if not row["low"] <= min(row["open"], row["close"]) <= max(row["open"], row["close"]) <= row["high"]:
        return False
    volume = row.get("volume")
    return type(volume) in (int, float) and not isinstance(volume, bool) and math.isfinite(volume) and volume >= 0


def _window(now):
    local = now.timetz().replace(tzinfo=None)
    return now.weekday() < 5 and SCAN_START <= local <= SCAN_END


def paper_prefilter_enabled():
    """Explicit opt-in for read-only/paper prefilter publication."""
    return os.environ.get("KADIMA_GAP_PAPER", "0") == "1"


def live_requested():
    """Separate explicit live opt-in; defaults OFF on every installation."""
    return os.environ.get("KADIMA_GAP_LIVE", "0") == "1"


def live_blocks(symbol, now=None):
    """Existing global permissions remain mandatory for Gap live entry."""
    from . import celestial
    from .utils import now_ist
    now = now or now_ist()
    cfg = celestial.read_config()
    reasons = []
    if not live_requested():
        reasons.append("gap_live_disabled")
    if not celestial.enabled():
        reasons.append("rollout_disabled")
    if not cfg.get("auto_trade_enabled"):
        reasons.append("auto_paused")
    if cfg.get("kill_latched"):
        reasons.append("kill_latched")
    news = celestial.news_impact(symbol, "CE", now)
    if not news.get("news_available"):
        reasons.append("fresh_news_coverage_required")
    if news.get("news_blocked"):
        reasons.append("news_risk_block")
    return reasons


def signal_id(row):
    """One durable live opportunity per underlying/episode."""
    from . import storage
    c = storage.conn()
    c.execute("CREATE TABLE IF NOT EXISTS gap_episodes "
              "(episode TEXT PRIMARY KEY, signal_id INTEGER NOT NULL)")
    with c:
        saved = c.execute("SELECT signal_id FROM gap_episodes WHERE episode=?",
                          (row["episode"],)).fetchone()
        if saved:
            return saved[0]
        cur = c.execute("INSERT INTO signals "
            "(ts,index_name,tier,score,direction,payload,kind) VALUES (?,?,?,?,?,?,?)",
            (row["ts"], row["symbol"], "CONFIRMED", row.get("score", 0),
             "CE", __import__("json").dumps(row), "gap_and_go"))
        sid = cur.lastrowid
        c.execute("INSERT INTO gap_episodes VALUES (?,?)", (row["episode"], sid))
        return sid


def observe_full_quote(symbol, quote, now, rules=None):
    """Build a fail-closed gap prefilter row from one broker FULL quote.

    This never reaches CONFIRMED or ENTRY. It exists only to cheaply identify
    opening gap candidates before any historical preparation.
    """
    rules = rules or Rules()
    row = _result(symbol, now)
    row["mode"] = "PAPER_PREFILTER"
    if not _window(now):
        return _block(row, "outside_gap_scan_window")
    if not isinstance(quote, dict):
        return _block(row, "full_quote_unavailable")
    try:
        opening = float(quote.get("open"))
        previous = float(quote.get("close"))
        ltp = float(quote.get("ltp"))
        raw_vwap = quote.get("avgPrice")
        vwap = float(raw_vwap) if raw_vwap is not None else None
        raw_volume = quote.get("tradeVolume")
        volume = float(raw_volume) if raw_volume is not None else None
    except (TypeError, ValueError, OverflowError):
        return _block(row, "full_quote_fields_invalid")
    if not all(_positive(v) for v in (opening, previous, ltp)):
        return _block(row, "open_previous_close_or_ltp_missing")
    gap_pct = (opening / previous - 1.0) * 100.0
    row["metrics"].update(open=opening, previous_close=previous, ltp=ltp,
                          gap_pct=round(gap_pct, 3),
                          session_vwap=vwap if _positive(vwap) else None,
                          cumulative_volume=volume if _finite(volume) and volume >= 0 else None)
    if gap_pct < rules.min_gap_pct:
        return _block(row, "gap_up_below_paper_threshold")
    row["stage"] = Stage.GAP_DETECTED.value
    return row


def _result(symbol, now):
    return dict(symbol=symbol, strategy="gap_and_go", mode="PAPER_ONLY",
                ts=now.isoformat(), stage=Stage.WATCH.value, eligible=False,
                blocked_reasons=[], metrics={})


def _block(row, reason):
    if reason not in row["blocked_reasons"]:
        row["blocked_reasons"].append(reason)
    return row


def evaluate(symbol, bars5, *, previous_close, session_vwap, prior_vwap,
             baseline_volume, confirmations, now, rules=None):
    """Evaluate one bullish stock setup using only closed 5-minute bars.

    confirmations is explicit/fail-closed:
      bull_shield: existing Shield v2.1 confirmation result
      gamma_oi: existing Gamma/OI confirmation result
      futures_available + futures_ok: require futures confirmation when available
      news_clear: existing fresh symbol-news gate
      macro_clear: macro-event suppression gate
      ce_wall_ratio + ce_wall_distance: fresh nearby CE-wall build evidence
      next_resistance: optional underlying resistance used to validate 2R room
    """
    rules = rules or Rules()
    row = _result(symbol, now)
    if not _window(now):
        return _block(row, "outside_gap_scan_window")
    if not isinstance(bars5, list) or len(bars5) < 1 or not all(_closed_bar(b) for b in bars5):
        return _block(row, "closed_5m_bars_unavailable")
    bars = sorted(bars5, key=lambda b: b["start_ts"])
    if not _positive(previous_close):
        return _block(row, "previous_close_unavailable")

    first = bars[0]
    opening_gap = first["open"] - previous_close
    gap_pct = opening_gap / previous_close * 100.0
    row["metrics"].update(previous_close=round(previous_close, 4),
                          open=first["open"], gap_pct=round(gap_pct, 3))
    if opening_gap <= 0 or gap_pct < rules.min_gap_pct:
        return _block(row, "gap_up_below_paper_threshold")
    row["stage"] = Stage.GAP_DETECTED.value

    # First five or ten minutes form the ignition impulse.
    impulse_count = 2 if len(bars) >= 2 else 1
    impulse = bars[:impulse_count]
    impulse_high = max(b["high"] for b in impulse)
    impulse_low = min(b["low"] for b in impulse)
    impulse_close = impulse[-1]["close"]
    impulse_pct = (impulse_high - impulse_low) / previous_close * 100.0
    impulse_volume = sum(float(b["volume"]) for b in impulse)
    if impulse_close <= first["open"] or impulse_pct < rules.min_impulse_pct:
        return _block(row, "bullish_opening_impulse_unconfirmed")
    if not _positive(baseline_volume):
        return _block(row, "same_time_volume_baseline_unavailable")
    rvol = impulse_volume / baseline_volume
    if rvol < rules.min_relative_volume:
        return _block(row, "opening_relative_volume_below_2x")

    session_low = min(b["low"] for b in bars)
    retained = (session_low - previous_close) / opening_gap
    if retained < rules.gap_retained_fraction:
        return _block(row, "gap_substantially_filled")
    if not _positive(session_vwap) or bars[-1]["close"] <= session_vwap:
        return _block(row, "price_not_above_session_vwap")
    if _positive(prior_vwap) and session_vwap < prior_vwap * (1 - rules.vwap_hold_tolerance):
        return _block(row, "session_vwap_falling")

    row["metrics"].update(impulse_pct=round(impulse_pct, 3),
                          impulse_volume=int(impulse_volume),
                          relative_volume=round(rvol, 3),
                          gap_retained_fraction=round(retained, 3),
                          session_vwap=session_vwap)
    row["stage"] = Stage.IGNITION.value

    # Need at least one closed pullback bar and then a later closed trigger bar.
    if len(bars) < impulse_count + 2:
        return _block(row, "awaiting_first_pullback_and_trigger")
    pullback = bars[impulse_count:-1]
    trigger = bars[-1]
    pullback_low = min(b["low"] for b in pullback)
    pullback_high = max(b["high"] for b in pullback)
    impulse_range = impulse_high - impulse_low
    if impulse_range <= 0:
        return _block(row, "opening_impulse_range_invalid")
    retracement = (impulse_high - pullback_low) / impulse_range
    pullback_volume = sum(float(b["volume"]) for b in pullback) / len(pullback)
    impulse_avg_volume = impulse_volume / len(impulse)
    volume_ratio = pullback_volume / impulse_avg_volume if impulse_avg_volume > 0 else float("inf")
    if retracement < 0 or retracement > rules.max_pullback_fraction:
        return _block(row, "pullback_deeper_than_50pct")
    if volume_ratio > rules.max_pullback_volume_ratio:
        return _block(row, "pullback_volume_not_contracting")
    if pullback_low <= previous_close:
        return _block(row, "pullback_filled_opening_gap")

    row["metrics"].update(pullback_fraction=round(retracement, 3),
                          pullback_volume_ratio=round(volume_ratio, 3),
                          pullback_low=pullback_low, pullback_high=pullback_high)
    row["stage"] = Stage.PULLBACK_HOLD.value

    opening_range_high = max(b["high"] for b in impulse)
    trigger_level = max(opening_range_high, pullback_high)
    if trigger["close"] <= trigger_level or trigger["high"] <= trigger_level:
        return _block(row, "orb_or_pullback_high_break_unconfirmed")
    row["stage"] = Stage.ORB_BREAK.value

    entry = trigger["close"]
    stop = pullback_low
    risk = entry - stop
    if not _positive(risk):
        return _block(row, "underlying_structure_risk_invalid")
    target_2r = entry + rules.min_rr * risk
    resistance = confirmations.get("next_resistance") if isinstance(confirmations, dict) else None
    if _positive(resistance) and resistance < target_2r:
        return _block(row, "known_resistance_blocks_2r")
    row["metrics"].update(entry=entry, stop=stop, risk=round(risk, 4),
                          target_2r=round(target_2r, 4), planned_rr=rules.min_rr,
                          trigger_level=trigger_level)

    if not isinstance(confirmations, dict):
        return _block(row, "confirmations_unavailable")
    if confirmations.get("bull_shield") is not True:
        return _block(row, "bull_shield_unconfirmed")
    if confirmations.get("gamma_oi") is not True:
        return _block(row, "gamma_oi_unconfirmed")
    if confirmations.get("futures_available") is True and confirmations.get("futures_ok") is not True:
        return _block(row, "futures_price_oi_unconfirmed")
    if confirmations.get("news_clear") is not True:
        return _block(row, "news_gate_not_clear")
    if confirmations.get("macro_clear") is not True:
        return _block(row, "macro_event_suppression_active_or_unknown")

    wall_ratio = confirmations.get("ce_wall_ratio")
    wall_distance = confirmations.get("ce_wall_distance")
    if _positive(wall_ratio) and _finite(wall_distance) and 0 <= wall_distance <= rules.wall_distance_points:
        row["metrics"]["ce_wall_ratio"] = round(wall_ratio, 3)
        row["metrics"]["ce_wall_distance"] = round(wall_distance, 3)
        if wall_ratio >= rules.wall_reject_ratio:
            return _block(row, "nearby_ce_wall_strengthened_150pct")
        if wall_ratio >= rules.wall_penalty_ratio:
            row["metrics"]["wall_penalty"] = True

    row["stage"] = Stage.CONFIRMED.value
    row["eligible"] = True
    row["mode"] = "LIVE_READY" if live_requested() else "PAPER_ONLY"
    row["blocked_reasons"].extend(live_blocks(symbol, now) if live_requested() else [])
    if row["blocked_reasons"]:
        row["eligible"] = False
    return row


def ranked_atm_or_itm_calls(chain, spot, days_to_expiry):
    """Reuse KADIMA ranking but restrict Gap lane to ATM or one-step ITM CE."""
    from . import strike_selector
    if not _positive(spot) or not _positive(days_to_expiry):
        return []
    atm = getattr(chain, "atm", None)
    step = getattr(getattr(chain, "index", None), "strike_step", None)
    if not _positive(atm) or not _positive(step):
        return []
    allowed = {float(atm), float(atm - step)}
    return [r for r in strike_selector.rank(chain, "CE", spot, days_to_expiry)
            if not r.rejected and float(r.quote.strike) in allowed]


@dataclass
class PaperPosition:
    symbol: str
    contract: str
    quantity: int
    option_entry: float
    underlying_entry: float
    underlying_stop: float
    target_2r: float
    entered_at: datetime
    status: str = "open"
    exit_reason: str | None = None


class PaperBook:
    """In-memory rehearsal ledger. It cannot place or route a broker order."""
    def __init__(self):
        self.positions = {}

    def enter(self, plan, *, contract, quantity, option_price, now):
        if plan.get("stage") != Stage.CONFIRMED.value or plan.get("eligible") is not True:
            raise ValueError("paper_entry_requires_confirmed_plan")
        symbol = plan["symbol"]
        if symbol in self.positions and self.positions[symbol].status == "open":
            raise ValueError("paper_one_position_per_symbol")
        if type(quantity) is not int or quantity <= 0 or not _positive(option_price):
            raise ValueError("paper_order_invalid")
        m = plan["metrics"]
        pos = PaperPosition(symbol=symbol, contract=contract, quantity=quantity,
                            option_entry=option_price, underlying_entry=m["entry"],
                            underlying_stop=m["stop"], target_2r=m["target_2r"],
                            entered_at=now)
        self.positions[symbol] = pos
        return pos

    def exit(self, symbol, reason):
        pos = self.positions.get(symbol)
        if pos is None or pos.status != "open":
            raise ValueError("paper_position_not_open")
        pos.status = "closed"
        pos.exit_reason = reason
        return pos
