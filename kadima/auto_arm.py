"""auto_arm.py -- turn Celestial auto-trading on once each trading morning.

Run by cron at 09:05 IST on weekdays (separate from kadima.service):

    35 3 * * 1-5  cd ~/kadima-py && KADIMA_CELESTIAL_ENABLED=1 \
        KADIMA_DATA_DIR=/var/lib/kadima <python> -m kadima.auto_arm

Behaviour (trader's instruction 2026-09-29: auto-trade on by default,
switched off manually from the dashboard when not wanted):
- arms auto_trade_enabled once per morning; a mid-day dashboard pause
  stays in force until the next morning's run
- never arms when the kill switch is latched (needs the manual reset)
- never arms on a holiday listed in the session-auto calendar, or when
  the kill-switch file exists
- only flips the flag: every entry gate (news, reconciliation, daily loss,
  session permit) still applies to each trade
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from . import audit, celestial, killswitch
from .utils import now_ist

POLICY = Path('/etc/kadima-session-auto/policy.json')
# RETIRED 2026-10-14 (trader's decision). The session scheduler
# (kadima-session-auto, state in SCHEDULER_STATE_DIR) owns morning arming.
# Its day baseline is taken at midnight; this cron's 09:05 flip of
# auto_trade_enabled looked like a dashboard change, so the scheduler set
# OPERATOR_PAUSED and kept the equity lane closed all day
# (lane_permit_not_granted on every entry, 13-Oct). Do not re-enable while
# the scheduler is installed.
SCHEDULER_STATE_DIR = Path('/var/lib/kadima-session-auto')


def _holiday(today: str) -> bool:
    try:
        return today in json.loads(POLICY.read_text()).get('holidays', [])
    except (OSError, ValueError, AttributeError):
        return False   # calendar unreadable: weekday cron + entry gates still protect


def main() -> int:
    if SCHEDULER_STATE_DIR.exists():
        audit.log('auto_arm_retired', reason='session_scheduler_owns_arming')
        return 0
    now = now_ist()
    today = now.date().isoformat()
    if now.weekday() >= 5 or _holiday(today):
        audit.log('auto_arm_skipped', reason='non_trading_day', day=today)
        return 0
    if killswitch.engaged():
        audit.log('auto_arm_skipped', reason='kill_switch_file_present')
        return 0
    if not celestial.enabled():
        audit.log('auto_arm_skipped', reason='celestial_rollout_flag_not_set')
        return 1
    cfg = celestial.read_config()
    if cfg['kill_latched']:
        audit.log('auto_arm_skipped', reason='kill_latched_manual_reset_required')
        return 0
    if cfg['auto_trade_enabled']:
        audit.log('auto_arm_already_on', revision=cfg['revision'])
        return 0
    cfg = celestial.update_config({'auto_trade_enabled': True},
                                  expected_revision=cfg['revision'])
    audit.log('auto_arm_enabled', revision=cfg['revision'])
    print(f"auto_trade_enabled=True (revision {cfg['revision']})")
    return 0


if __name__ == '__main__':
    sys.exit(main())
