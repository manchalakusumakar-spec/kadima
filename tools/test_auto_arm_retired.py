"""Offline: auto_arm must not touch Celestial controls while the session scheduler is installed."""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ['KADIMA_DATA_DIR'] = tempfile.mkdtemp(prefix='kadima-auto-arm-')
os.environ['KADIMA_CELESTIAL_ENABLED'] = '1'
sys.path.insert(0, '.')

from kadima import auto_arm, celestial


def test_retired_when_scheduler_installed():
    present = Path(tempfile.mkdtemp(prefix='session-auto-'))
    with patch.object(auto_arm, 'SCHEDULER_STATE_DIR', present), \
         patch.object(celestial, 'update_config') as update, \
         patch.object(auto_arm.audit, 'log') as log:
        assert auto_arm.main() == 0
    update.assert_not_called()
    log.assert_called_once_with('auto_arm_retired', reason='session_scheduler_owns_arming')


def test_legacy_path_unchanged_without_scheduler():
    absent = Path(tempfile.mkdtemp(prefix='session-auto-')) / 'missing'
    with patch.object(auto_arm, 'SCHEDULER_STATE_DIR', absent), \
         patch.object(auto_arm, 'killswitch') as kill, \
         patch.object(auto_arm, '_holiday', return_value=False), \
         patch.object(auto_arm, 'now_ist') as clock, \
         patch.object(celestial, 'read_config',
                      return_value=dict(kill_latched=False, auto_trade_enabled=False, revision=7)), \
         patch.object(celestial, 'update_config',
                      return_value=dict(revision=8)) as update, \
         patch.object(auto_arm.audit, 'log'):
        kill.engaged.return_value = False
        clock.return_value.weekday.return_value = 2
        clock.return_value.date.return_value.isoformat.return_value = '2026-10-14'
        assert auto_arm.main() == 0
    update.assert_called_once_with({'auto_trade_enabled': True}, expected_revision=7)


if __name__ == '__main__':
    test_retired_when_scheduler_installed()
    test_legacy_path_unchanged_without_scheduler()
    print('auto_arm retirement tests passed')
