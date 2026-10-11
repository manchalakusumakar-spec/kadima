"""Public failure taxonomy. Never serialize arbitrary exceptions or locals."""
KNOWN_MESSAGES = {
    'Invalid historical data': 'history_schema_invalid',
    'Incomplete historical candle': 'history_candle_incomplete',
    'Candle timezone missing': 'history_timezone_missing',
    'Invalid candle values': 'history_values_invalid',
    'Invalid candle ordering/range': 'history_order_or_range_invalid',
    'No closed candles': 'history_no_closed_candles',
    'Stale historical candles': 'history_stale',
    'Missing session candles': 'history_session_gap',
    'Stale stock snapshot': 'stock_snapshot_stale',
    'Index is not a stock': 'stock_mapping_is_index',
    'Unsupported stock ladder width': 'stock_ladder_width_invalid',
    'Incomplete or obsolete stock ladder': 'stock_ladder_mismatch',
    'Contract metadata mismatch': 'stock_contract_metadata_mismatch',
    'SmartAPI login unavailable': 'broker_session_unavailable',
    'SmartAPI session refresh in progress': 'broker_session_refresh_busy',
    'SmartAPI login retry cooldown': 'broker_session_retry_cooldown',
}
ERROR_TYPES = frozenset(('ValueError', 'TypeError', 'KeyError', 'AttributeError',
    'OverflowError', 'RuntimeError', 'TimeoutError', 'ConnectionError',
    'OperationalError', 'IntegrityError', 'OSError', 'ImportError', 'NameError'))


def describe(exc, stage, fallback='preparation_error'):
    return dict(reason=KNOWN_MESSAGES.get(str(exc), fallback),
                error_type=type(exc).__name__ if type(exc).__name__ in ERROR_TYPES else 'OtherError',
                preparation_stage=stage)
