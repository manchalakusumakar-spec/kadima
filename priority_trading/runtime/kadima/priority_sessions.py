"""Broker-aware entry/exit clocks; normal sessions only, calendar expires closed.

MCX calendar: MCX circular MCX/TRD/636/2025 (15 December 2025).
Broker: https://www.angelone.in/riskpolicy, reviewed 28 September 2026.
MCX uses the earlier option-devolvement-day square-off boundary every day.
This intentionally leaves five minutes before the earliest 22:45 cutoff.
"""
from datetime import date, time
from zoneinfo import ZoneInfo

IST = ZoneInfo('Asia/Kolkata')
VALID_FROM = date(2026, 9, 28)
VALID_THROUGH = date(2026, 12, 31)
MCX_CLOSED = {'2026-10-02', '2026-12-25'}
MCX_EVENING_ONLY = {'2026-10-20', '2026-11-10', '2026-11-24'}


def calendar_reason(exchange, now):
    if now.tzinfo is None:
        return 'session_timezone_required'
    local = now.astimezone(IST)
    if not VALID_FROM <= local.date() <= VALID_THROUGH:
        return 'calendar_update_required'
    clock = local.time().replace(tzinfo=None)
    if exchange == 'MCX':
        if local.weekday() >= 5 or local.date().isoformat() in MCX_CLOSED:
            return 'mcx_non_trading_day'
        start = time(17) if local.date().isoformat() in MCX_EVENING_ONLY else time(9)
        if not start <= clock < time(22, 35):
            return 'outside_mcx_entry_window'
    elif exchange in ('NFO', 'BFO'):
        # The existing NSE/BSE calendar is checked separately by the caller.
        if not time(9, 15) <= clock < time(15, 15):
            return 'outside_broker_intraday_entry_window'
    else:
        return 'unsupported_trading_exchange'
    return None


def exit_due(exchange, now):
    """Exit protection does not need an entry permit, flag, or open calendar."""
    if now.tzinfo is None:
        return True
    clock = now.astimezone(IST).time().replace(tzinfo=None)
    return clock >= (time(22, 40) if exchange == 'MCX' else time(15, 18))
