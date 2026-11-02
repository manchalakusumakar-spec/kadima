"""CAS auto-execution settings. Kept out of config.py so deploying CAS never
edits the live config file (which differs from GitHub on the EC2 box)."""
import os

# 2026-09-29, trader's decisions: CAS strikes were being identified but never
# executed (scanner observe-only, INTRADAY blocked after Angel's 15:20 MIS
# square-off). CAS trades now execute automatically as CARRYFORWARD orders
# (the SmartAPI value -- "NRML" is not a valid producttype string), sized at
# min(CAS_MAX_CAPITAL_INR, live available cash), no stop-loss at entry,
# peak-trailing exit, forced exit at the existing 15:38 EOD close.
CAS_EXECUTION_ENABLED = os.environ.get("KADIMA_CAS_EXECUTION", "0") == "1"   # OFF unless enabled in systemd
CAS_PRODUCT_TYPE = "CARRYFORWARD"
CAS_ENTRY_START = (15, 15)         # CAS window opens
CAS_ENTRY_END = (15, 35)           # no fresh CAS entry at/after this (EOD close 15:38)
CAS_MAX_CAPITAL_INR = 10_000       # per trade: min(this, availablecash)
CAS_MAX_TRADES_PER_DAY = 1
CAS_MIN_PREMIUM = 2.0              # skip sub-Rs2 lottery strikes (spread/tick noise)
CAS_MAX_PREMIUM = 80.0             # CAS targets cheap premiums only
CAS_MAX_SPREAD_PCT = 10.0          # executable top-of-book spread; fail closed above this
CAS_STRIKES_OTM = 4                # consider OTM strikes up to this many steps from ATM
CAS_TRIGGER_PREMIUM_PCT = 25.0     # premium up >= this % over CAS_TRIGGER_POLLS chain polls
CAS_TRIGGER_POLLS = 2
CAS_TRIGGER_VOLUME_MULT = 3.0      # latest per-poll traded volume >= this x trailing avg
CAS_VOLUME_TRAILING_POLLS = 12
CAS_ENTRY_PRICE_CAP_MULT = 1.30    # never pay above 1.3x the premium at signal time
CAS_ENTRY_STEP_SEC = 2.0           # re-price an unfilled entry every N seconds...
CAS_ENTRY_MAX_STEPS = 4            # ...at most this many prices, then cancel the rest
CAS_TRAIL_ARM_PCT = 30.0           # trailing stop arms once premium is +30% over entry
CAS_TRAIL_GIVEBACK_PCT = 25.0      # then exits on a 25% fall from the peak premium
CAS_EXIT_WATCH_SEC = 3.0           # CAS positions are checked this often (main monitor: 10s)
# Exchange freeze limits cap one order's quantity; a CAS order is never sliced
# (the execution ledger allows one live order per signal), so quantity is
# clamped instead. Conservative values -- verify against current NSE/BSE circulars.
CAS_MAX_ORDER_QTY = {"NIFTY": 1800, "BANKNIFTY": 900, "FINNIFTY": 1800,
                     "MIDCPNIFTY": 2800, "SENSEX": 1000, "BANKEX": 900}
