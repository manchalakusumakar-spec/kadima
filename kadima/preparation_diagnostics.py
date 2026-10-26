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


def _hint(exc):
    """Short, cleaned exception text (2026-10-27): long tokens, URLs and quotes removed."""
    import re
    text = re.sub(r'https?://\S+', '<url>', str(exc))
    text = re.sub(r'[A-Za-z0-9_\-.]{24,}', '<redacted>', text)
    text = re.sub(r'[^A-Za-z0-9 _.:,()<>/-]', ' ', text)
    return ' '.join(text.split())[:120]


def describe(exc, stage, fallback='preparation_error'):
    name = type(exc).__name__
    return dict(reason=KNOWN_MESSAGES.get(str(exc), fallback),
                error_type=name if name in ERROR_TYPES else 'OtherError',
                error_class=name if name.isidentifier() else 'Unknown',
                error_hint=_hint(exc),
                preparation_stage=stage)
