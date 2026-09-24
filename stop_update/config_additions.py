# Draft dynamic stop specification (offline only; not a deployment flag).
STOP_REGIMES = {
    'INTRADAY': dict(bar_seconds=300, spot_buffer=.25, noise_atr=1.5,
        max_sl_pct=.30, breakeven_r=1., trail_r=1.5, tighten_r=2.,
        trail_atr=2.5, tight_atr=1.5, sniper_trail_atr=2., sniper_tight_atr=1.2,
        giveback=.35, sniper_giveback=.25, partial_r=1., partial_fraction=.40,
        partial_below_score=90, initial_timeout_seconds=1800, timeout_gain_r=.30,
        theta_start='13:30:00', theta_factor=.85, shield_exit_below=40,
        opposing_wall_ratio=1.5, opposing_wall_points=50, confirmation_bars=2,
        limit_ticks=1),
    'SCALP': dict(bar_seconds=60, spot_buffer=0., noise_atr=1.2,
        min_sl_pct=.08, max_sl_pct=.10, breakeven_pct=.06,
        t1_pct=.12, t1_fraction=.50, t1_lock_pct=.06,
        t2_pct=.20, hold_score=90, trail_atr=1., timeout_seconds=120,
        cutoff='14:45:00', momentum_exit_below=70, confirmation_bars=2,
        spread_multiple=2., spread_snapshots=2, limit_ticks=2),
    'GAMMA': dict(bar_seconds=60, spot_buffer=.20, noise_atr=2.,
        max_sl_pct=.35, preentry_max_sl_pct=.25, max_lots=2,
        locks=[[1.5, 1.0], [2., 1.3], [3., 1.8], [4., 2.88]],
        peak_lock=.72, chandelier_start=2., trail_atr=3.,
        cutoff='15:10:00', preentry_seconds=120,
        wall_strength_pct=.10, wall_window_seconds=600, oi_flip_polls=2,
        iv_crush_points=3., half_fraction=.50, opposing_wall_ratio=1.5,
        opposing_wall_points=50, opposing_fraction=.40,
        acceleration_bars=2, spot_stall_atr=.10, limit_ticks=1),
    'CAS': dict(bar_seconds=15, max_sl_pct=1., start='15:15:00',
        stall_seconds=60, free_ride_multiple=2., partial_fraction=.50,
        giveback=.40, tighten_multiple=5., tight_giveback=.25,
        max_spread_pct=.25, limit_ticks=2,
        risk_budget_inr=None, max_trades_per_day=None),
}

SESSION_MODIFIERS = {
    'EXPIRY': dict(trail_factor=.75, time_factor=.5, theta_start='12:30:00'),
    'EXPIRY_MINUS_1': dict(intraday_trail_factor=.85,
                         gamma_below90_regime=None),
    'MACRO': dict(breakeven_r=.5),
}

# Proposed, not a claim about actual broker or exchange session times.
CAS_HARD_EXIT = '15:18:30'
STOP_ENGINE = dict(version=3, max_input_age_seconds=5,
    thesis_max_age_seconds=30, min_spread_multiple=3.,
    reprice_seconds=2, max_reprices=3, broker_buffer_seconds=90,
    calibration_min_occurrences=30, mode='OFFLINE_DRAFT')

# Offline candidate settings agreed for NIFTY Intraday. Shorter-regime windows
# and other instruments require their own tested settings before inclusion.
STRUCTURE_GUARD = dict(
    regimes=['INTRADAY'], assets=['NIFTY'], confidence_min_pct=65.,
    grace_seconds=60, confirmation_bar_seconds=60, structure_bar_seconds=300,
    proximity_atr=.25, buffer_atr=.30, min_points=5.,
    forecast_max_age_seconds=60, structure_max_age_seconds=5,
    reaction_max_age_seconds=1800,
)

# Trusted operator configuration, NEVER accepted from a forecast or replay row.
# Empty until recovery-before-exit probabilities have out-of-sample validation.
# See docs/dynamic_stops_v1.md for the required validation record.
STRUCTURE_GUARD_VALIDATIONS = {}

# User instruction, 24 Sep 2026. Draft manual positions use Intraday until
# the existing CAS start, then no strategy price/thesis stops. Deadlines and
# pending broker intents remain binding. No live adoption is enabled here.
MANUAL_STOP_POLICY = dict(version=1, cas_start='15:15:00', full_premium_risk=True)
REGIME_STOPS_ENABLED = os.environ.get('KADIMA_REGIME_STOPS_ENABLED', '0') == '1'
