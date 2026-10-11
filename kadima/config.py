
"""
KADIMA data engine configuration.

Every tunable lives here. Nothing downstream hard-codes a threshold.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# ---------------------------------------------------------------- credentials
# Never commit these. Export them in the systemd/PM2 environment.
ANGEL_API_KEY = os.environ.get("ANGEL_API_KEY", "")
ANGEL_CLIENT_ID = os.environ.get("ANGEL_CLIENT_ID", "")
ANGEL_PASSWORD = os.environ.get("ANGEL_PASSWORD", "")   # MPIN
ANGEL_TOTP_SECRET = os.environ.get("ANGEL_TOTP_SECRET", "")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Macro & News Watch (macro_watch.py) -- free-tier financial news API,
# see macro_watch.py module docstring for what this covers and why.
MARKETAUX_API_KEY = os.environ.get("MARKETAUX_API_KEY", "")

# ---------------------------------------------------------------- market hours
# MARKET_CLOSE updated 2026-08-19 for SEBI's Closing Auction Session (CAS),
# effective 2026-08-03. The equity Derivatives Segment (what KADIMA trades —
# NIFTY/BANKNIFTY/SENSEX/MIDCPNIFTY options) now closes at 15:40, extended
# from the pre-CAS 15:30. Cash-market F&O-eligible stocks stop continuous
# trading earlier (15:15) and run a closing auction to 15:35 — that boundary
# does NOT apply here since this engine only trades index derivatives.
MARKET_OPEN = (9, 15)
MARKET_CLOSE = (15, 40)
PREMARKET_START = (9, 0)      # engine boots, no signals emitted
SIGNAL_CUTOFF = (15, 35)      # stop emitting new Bull Shield entry signals.
                              # Extended 15:10 -> 15:35 on 2026-08-24, day
                              # one of live trading, after a real ~130-pt
                              # NIFTY move happened at 15:20-15:25 and was
                              # structurally invisible to the system.
                              # Trader's explicit instruction: auto-trading
                              # enabled up to the 15:40 close. IMPORTANT:
                              # there is NO broker-side MIS backstop for
                              # ANYTHING entered after 15:20 (it already
                              # fired once, does not fire again) -- see
                              # auto_executor.py's EOD_FORCE_CLOSE_HHMM and
                              # eod_watchdog.py's retimed cron for how this
                              # window's exit safety is (thinly) covered.
# Gamma signals shared this cutoff with Bull Shield again as of 2026-08-23.
# Briefly had its own later cutoff (15:35) from 2026-08-20, matching the
# trader's observed late-session gamma-blast window, made viable by
# Gamma matches Bull Shield's cutoff again as of 2026-08-24 (both 15:35).
# History: had its own later cutoff under NRML (2026-08-20), reverted to
# match Bull Shield's 15:10 when NRML was rejected (2026-08-23), then
# BOTH extended together to 15:35 the same night after a real missed move
# at 15:20-15:25 -- trader's explicit instruction, accepting that this
# window has no broker-side MIS backstop (see SIGNAL_CUTOFF's comment
# above for the full reasoning).
GAMMA_SIGNAL_CUTOFF = (15, 35)

# ---------------------------------------------------------------- instruments
# exchange tokens per Angel One scrip master. VERIFY THESE against the current
# OpenAPIScripMaster.json before first live run - they change.
@dataclass(frozen=True)
class Index:
    name: str
    exchange: str          # NSE / BSE  (spot)
    token: str             # spot token
    opt_exchange: str      # NFO / BFO  (options)
    opt_symbol_root: str   # prefix in scrip master, e.g. "NIFTY"
    strike_step: int
    weight: float          # contribution to four-index confirmation vote
    # lot_size intentionally removed 2026-08-19: it was declared here but never
    # read anywhere in the codebase (grep-verified across every module). Real
    # lot sizes are always sourced live via instrument_master.get_lot_size(),
    # which pulls from Angel One's actual scrip master. A static value here
    # would silently go stale without anything ever noticing — which is
    # exactly what happened: this field said NIFTY=75 while the real NSE lot
    # size had already moved to 65 as of the January 2026 revision. Dead data
    # that looks like real data is worse than no data; removed rather than
    # just corrected, so it can't happen again.


INDICES: dict[str, Index] = {
    "NIFTY": Index("NIFTY", "NSE", "99926000", "NFO", "NIFTY", 50, 1.0),
    "BANKNIFTY": Index("BANKNIFTY", "NSE", "99926009", "NFO", "BANKNIFTY", 100, 1.0),
    "SENSEX": Index("SENSEX", "BSE", "99919000", "BFO", "SENSEX", 100, 0.7),
    "MIDCPNIFTY": Index("MIDCPNIFTY", "NSE", "99926074", "NFO", "MIDCPNIFTY", 25, 0.6),
}
# SENSEX/MIDCAP carry lower vote weight: Greeks coverage is thinner and depth is
# lower, so treating their confirmation as equal to NIFTY's would be false
# precision. Raise once you've validated their data quality yourself.

GAMMA_EXTRA_INDICES: dict[str, Index] = {
    "FINNIFTY": Index("FINNIFTY", "NSE", "99926037", "NFO", "FINNIFTY", 50, 0.5),
    "NIFTYNXT50": Index("NIFTYNXT50", "NSE", "99926013", "NFO", "NIFTYNXT50", 100, 0.3),
    "BANKEX": Index("BANKEX", "BSE", "99919012", "BFO", "BANKEX", 100, 0.4),
    "SENSEX50": Index("SENSEX50", "BSE", "99919082", "BFO", "SENSEX50", 50, 0.3),
}
# These 4 lost their weekly expiry under SEBI's Nov-2024 single-weekly-index
# rule and now trade monthly only (last Tue for NSE names, last Thu for BSE).
# Scanning them every day of the month for a gamma blast that can only
# happen on their one expiry day would waste REST/API quota and add noise --
# GAMMA_EXPIRY_ONLY below gates engine.py's OI loop to their expiry day only.
# Deliberately kept OUT of INDICES above: that dict also drives Bull Shield's
# cross-index confirmation vote in engine.py's _evaluate(), and merging these
# in would silently change that vote's math. GAMMA_INDICES below is the
# union, used only by the separate gamma-blast scan path (_score_gamma).
GAMMA_EXPIRY_ONLY: set[str] = {"FINNIFTY", "NIFTYNXT50", "BANKEX", "SENSEX50"}
GAMMA_INDICES: dict[str, Index] = {**INDICES, **GAMMA_EXTRA_INDICES}

# Stored-data OI confirmation: opt in only after reviewing offline replay.
OI_CONFIRM_MODE = os.environ.get('OI_CONFIRM_MODE', 'shadow').lower()
if OI_CONFIRM_MODE not in ('shadow', 'live'):
    OI_CONFIRM_MODE = 'shadow'

# ---------------------------------------------------------------- cadences
WS_HEARTBEAT_SEC = 10          # expect at least one tick per this, else reconnect
WS_RECONNECT_BASE_SEC = 2      # exponential backoff base
WS_RECONNECT_MAX_SEC = 60
WS_MAX_RETRIES = 0             # 0 = unlimited

OI_POLL_SEC = 10               # REST getMarketData cadence for option chain.
                               # Reduced from 30 on 2026-08-24 -- trader's
                               # explicit request to cut gamma's detection
                               # lag, confirmed safe: this only changes
                               # HOW OFTEN data is sampled, not the 10-min
                               # rolling window or any scoring math, so it
                               # doesn't dilute confirmation, just makes it
                               # fresher. Comfortably within Angel One's
                               # published quote rate limit (10/sec) for
                               # 4 indices. With the per-index 1s spacing
                               # already in _oi_loop, actual full-cycle
                               # time lands around 14s, matching the
                               # trader's requested 10-15s range.
OI_VELOCITY_WINDOW_MIN = 10    # rolling window for wall-loss detection
OI_VELOCITY_DROP_PCT = 15.0    # NIFTY default; BANKNIFTY tightened below
OI_VELOCITY_DROP_PCT_BY_INDEX = {
    "NIFTY": 15.0,
    "BANKNIFTY": 12.0,
    "SENSEX": 15.0,
    "MIDCPNIFTY": 18.0,
}

CANDLE_INTERVALS_MIN = (1, 3, 5)

# ---------------------------------------------------------------- option chain
STRIKES_EACH_SIDE = 5          # ATM +/- N strikes tracked per index
MIN_DELTA = 0.45
MAX_DELTA = 0.60
MAX_SPREAD_PCT = 1.5           # (ask-bid)/mid, reject wider
MIN_OI = 100_000               # contract-level minimum open interest
MIN_VOLUME = 5_000

# ---------------------------------------------------------------- indicators
EMA_FAST = 33      # rescaled from 20 on 2026-08-24 (5-min -> 3-min candles).
                   # 20*5min=100min real-world window; 33*3min=99min --
                   # same lookback, just refreshed every 3 min instead of
                   # 5. This is the whole point of the rescale: faster
                   # confirmation, not a different (weaker or noisier)
                   # one. See engine.py's _on_candle_close for the
                   # interval switch this pairs with.
EMA_SLOW = 83      # 50*5min=250min -> 83*3min=249min
MA_LONG = 92       # 55*5min=275min -> 92*3min=276min
RSI_PERIOD = 23    # 14*5min=70min -> 23*3min=69min
VOLUME_AVG_WINDOW = 33   # extracted 2026-08-24 from a hardcoded "20" that
                         # was sitting directly in indicators.compute() --
                         # same 100min-window rescale as EMA_FAST above,
                         # now a named constant instead of a silent magic
                         # number that would have kept comparing volume
                         # against a 5-min-shaped window after the switch
                         # to 3-min candles, quietly diluting this factor
                         # even though nothing else looked wrong.

# ---------------------------------------------------------------- storage
DATA_DIR = os.environ.get("KADIMA_DATA_DIR", "/var/lib/kadima")
DB_PATH = os.path.join(DATA_DIR, "kadima.db")
AUDIT_PATH = os.path.join(DATA_DIR, "audit")
KILL_SWITCH_FILE = os.path.join(DATA_DIR, "KILL")

# ---------------------------------------------------------------- risk
MAX_CONCURRENT_POSITIONS = 1
MAX_LOTS_PER_SIGNAL = 2
DAILY_LOSS_LIMIT_INR = 10_000      # set to your real number
# Manual long options are protected independently of new automatic entries.
# Set MANUAL_PROTECT_ENABLED=False to opt out of manual adoption/protection.
# Stops are engine-monitored SELL limits, not broker-hosted stop orders.
MANUAL_PROTECT_ENABLED = True
MANUAL_HARD_STOP_PCT = 0.30
MANUAL_TRAIL_ACTIVATE_PCT = 0.10  # thereafter retain 50% via trade_policy.ratchet
NO_NEW_SIGNALS_AFTER_HOUR = 13     # non-expiry days, per your own rule
ORDER_PLACEMENT_ENABLED = True     # LIVE since 2026-08-21, trader's explicit decision



# --------------------------------------------------------------- timing module
# Gamma entry/exit timing, phase 1 (record only). When True, every Gamma
# entry evaluation writes one line to DATA_DIR/timing/{day}.jsonl for the
# after-market outcome job. Changes no trading decision. Set False to stop
# recording without a code change.
TIMING_RECORDER_ENABLED = True
# End of the measuring window for the outcome job. Matches the live
# auto_executor EOD_FORCE_CLOSE_HHMM (15:18 on the box as of 2026-09-29).
TIMING_WINDOW_END = (15, 18)


# --------------------------------------------------------------- no-auto-exit window
# 2026-09-06, trader's explicit instruction: during this window, the fixed
# stop-loss, the giveback-trail exit, and capital_guard's deployment
# ceiling are all disabled -- positions ride untouched by the algo.
# Outside this window every existing rule applies exactly as before.
from datetime import datetime as _datetime, timezone as _timezone, timedelta as _timedelta

_IST = _timezone(_timedelta(hours=5, minutes=30))

NO_AUTO_EXIT_WINDOW_START = (15, 15)
NO_AUTO_EXIT_WINDOW_END = (15, 40)


def in_no_auto_exit_window() -> bool:
    now = _datetime.now(_IST)
    start_h, start_m = NO_AUTO_EXIT_WINDOW_START
    end_h, end_m = NO_AUTO_EXIT_WINDOW_END
    return (start_h * 60 + start_m) <= (now.hour * 60 + now.minute) <= (end_h * 60 + end_m)


# HOURLY FORECAST v1
# ---------------------------------------------------------------- hourly forecast panel
# 2026-10-09, trader's decisions: NIFTY only to start (KADIMA_FORECAST_INDICES
# env var, comma-separated, adds SENSEX/BANKNIFTY later), display only, plus
# human-clicked CE/PE BUY buttons with a confirm step. Each click goes through
# auto_executor.forecast_manual_buy(), which keeps every existing entry gate.
FORECAST_MANUAL_TRADE_ENABLED = True
FORECAST_MANUAL_MAX_LOTS = MAX_LOTS_PER_SIGNAL
FORECAST_MANUAL_FALLBACK_SL_PCT = 0.30   # used only when no confirmed swing stop exists
# Reserved for a possible future auto-trade mode from the forecast. Nothing reads it.
FORECAST_AUTOTRADE_ENABLED = False
