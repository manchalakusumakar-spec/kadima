"""Index calendar/news entry gate; no broker or configuration mutations."""
from datetime import date, time, timedelta
from threading import Lock

from . import audit
from .config import GAMMA_INDICES, IST

_minutes = {}
_lock = Lock()


def index_event_block(name, now) -> str | None:
    if name not in GAMMA_INDICES:
        return None
    from . import celestial
    local = now.astimezone(IST)
    reason = None
    if celestial.news_impact(name, 'CE', now)['news_blocked']:
        reason = 'news_risk_block'
    else:
        reason = macro_event_block(name, now)
    minute = local.strftime('%Y-%m-%dT%H:%M')
    with _lock:
        if _minutes.get(name) != minute:
            audit.log('index_news_gate', index=name,
                      decision='block' if reason else 'allow', reason=reason)
            _minutes[name] = minute
    return reason


def macro_event_block(name, now, *, events=None, holidays=None):
    """Pure calendar decision for replay; never logs or reads news/config."""
    if name not in GAMMA_INDICES:
        return None
    from . import macro_watch, market_session_gate
    if events is None:
        events = macro_watch.ECONOMIC_EVENTS
    if holidays is None:
        try:
            policy = market_session_gate.validate_policy(
                market_session_gate.read_json(market_session_gate.POLICY))
            holidays = set(policy['holidays'])
        except (OSError, ValueError):
            holidays = set()
    local = now.astimezone(IST)
    for event in events:
        day = date.fromisoformat(event['end_date'])
        if 'RBI MPC' in event['name']:
            start, end = time(9, 45), time(10, 30)
        elif 'FOMC' in event['name']:
            day += timedelta(days=1)
            while day.weekday() >= 5 or day.isoformat() in holidays:
                day += timedelta(days=1)
            start, end = time(9, 15), time(9, 45)
        else:
            continue
        if local.date() == day and start <= local.time().replace(tzinfo=None) < end:
            return 'macro_event_window:' + event['name']
    return None
