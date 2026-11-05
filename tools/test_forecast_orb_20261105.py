"""Tests for the opening-range breakout lean (kadima/forecast_orb.py, 05-Nov-2026, shadow only).

    venv/bin/python -I tools/test_forecast_orb_20261105.py
"""
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kadima import forecast_orb as ORB  # noqa: E402


def day(closes, wick=2.0):
    """A full session (75 bars) of opens/highs/lows/closes built from closes."""
    closes = list(closes) + [closes[-1]] * (75 - len(closes))
    opens = [closes[0]] + closes[:-1]
    highs = [max(o, c) + wick for o, c in zip(opens, closes)]
    lows = [min(o, c) - wick for o, c in zip(opens, closes)]
    return opens, highs, lows, closes


class OrbLean(unittest.TestCase):
    def test_breakout_up(self):
        # First hour 100..110, then price climbs to 134 by 12:15.
        c = [100 + i for i in range(11)] + [110] + [110 + i for i in range(1, 25)]
        ln = ORB.lean_for('12:15', *day(c))
        self.assertEqual((ln.lean, ln.rule), ('UP', 'orb'))
        self.assertAlmostEqual(ln.or_high, 112.0)

    def test_breakout_down(self):
        c = [100 - i for i in range(12)] + [89 - i for i in range(24)]
        ln = ORB.lean_for('13:15', *day(c))
        self.assertEqual(ln.lean, 'DOWN')

    def test_inside_range_no_lean(self):
        c = [100, 110, 90, 105, 95, 100, 100, 100, 100, 100, 100, 100] + [101] * 24
        ln = ORB.lean_for('12:15', *day(c))
        self.assertEqual(ln.lean, 'NONE')

    def test_only_bars_before_the_slot_are_read(self):
        # A huge rally INSIDE the 12:15 slot must not change the 12:15 lean.
        base = [100] * 36
        quiet = day(base + [100] * 39)
        rally = day(base + [200] * 39)
        self.assertEqual(ORB.lean_for('12:15', *quiet).lean, ORB.lean_for('12:15', *rally).lean)
        self.assertEqual(ORB.lean_for('12:15', *rally).ref, 100)

    def test_fade_last_hour(self):
        c = [100] * 60 + [100 + 3 * i for i in range(1, 13)]   # +36 pts from 14:15 to 15:15
        ln = ORB.lean_for('15:15', *day(c))
        self.assertEqual((ln.lean, ln.rule), ('DOWN', 'fade'))
        c = [100] * 60 + [100 - 3 * i for i in range(1, 13)]
        self.assertEqual(ORB.lean_for('15:15', *day(c)).lean, 'UP')

    def test_fade_small_move_no_lean(self):
        c = [100] * 60 + [101] * 12
        self.assertEqual(ORB.lean_for('15:15', *day(c)).lean, 'NONE')

    def test_slots_without_rule(self):
        c = [100 + i for i in range(75)]
        self.assertIsNone(ORB.lean_for('09:15', *day(c)))
        self.assertEqual(ORB.lean_for('10:15', *day(c)).lean, 'NONE')
        self.assertEqual(ORB.lean_for('11:15', *day(c)).lean, 'NONE')
        self.assertEqual(ORB.lean_for('14:15', *day(c)).lean, 'NONE')

    def test_missing_first_hour_bar_gives_no_lean(self):
        o, h, lo, c = day([100] * 36)
        h[3] = None
        self.assertIsNone(ORB.lean_for('12:15', o, h, lo, c))

    def test_05nov_session_shape(self):
        # 05-Nov-2026: first hour 24298..~24445, 13:15 ref 24472 (above) -> UP; the slot closed 24550.
        c = [24356, 24341, 24350, 24380, 24400, 24420, 24438, 24430, 24402, 24390, 24374, 24367]
        c += [24380, 24395, 24388, 24380, 24375, 24372, 24369, 24375, 24386, 24400, 24411, 24370]
        c += [24380, 24390, 24397, 24400, 24405, 24410, 24412, 24420, 24430, 24428, 24440, 24415]
        c += [24430, 24442, 24453, 24460, 24463, 24470, 24468, 24465, 24470, 24472, 24472, 24472]
        o, h, lo, cl = day(c)
        lo[0] = 24298.45
        h[6] = 24445.0
        self.assertEqual(ORB.lean_for('12:15', o, h, lo, cl).lean, 'NONE')    # 24415 inside
        self.assertEqual(ORB.lean_for('13:15', o, h, lo, cl).lean, 'UP')


class Record(unittest.TestCase):
    def test_record_once_per_slot(self):
        c = sqlite3.connect(':memory:')
        ln = ORB.Lean('12:15', 'UP', 'orb', 101.0, 100.0, 90.0, 'x')
        self.assertTrue(ORB.record(c, '2026-11-05', 'NIFTY', ln, '2026-11-05T11:15:02+05:30'))
        self.assertFalse(ORB.record(c, '2026-11-05', 'NIFTY', ORB.Lean('12:15', 'DOWN', 'orb', 1.0), 'later'))
        self.assertEqual(c.execute('SELECT lean FROM hourly_forecast_orb_shadow').fetchall(), [('UP',)])
        self.assertFalse(ORB.record(c, '2026-11-05', 'NIFTY', None, 'x'))


class EngineHook(unittest.TestCase):
    """The forecaster records the lean at slot start and never raises."""

    def setUp(self):
        try:
            import kadima.audit as A
            A.log = lambda *a, **k: None
            from kadima import hourly_forecast as HF
        except Exception as exc:                     # noqa: BLE001 - needs the live venv
            self.skipTest(f'kadima.hourly_forecast not importable here: {exc}')
        self.HF = HF
        self.events = []
        HF.audit.log = lambda ev, **k: self.events.append((ev, k))
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, 'forecast.db')

    def _run(self, rows):
        from datetime import date, datetime, timedelta, timezone
        HF = self.HF
        ist = timezone(timedelta(hours=5, minutes=30))
        d = date(2026, 11, 5)
        t0 = datetime(2026, 11, 5, 13, 15, tzinfo=ist)
        orig = HF.history_rows
        HF.history_rows = lambda token, until, days=None, db=None: [r for r in rows if r[0] + timedelta(minutes=5) <= until]
        try:
            fake = type('F', (), {'db': self.db})()
            return HF.HourlyForecaster._orb_shadow(fake, 'NIFTY', '99926000', d, '13:15', t0, t0)
        finally:
            HF.history_rows = orig

    def test_records_breakout(self):
        from datetime import datetime, timedelta, timezone
        ist = timezone(timedelta(hours=5, minutes=30))
        t = datetime(2026, 11, 5, 9, 15, tzinfo=ist)
        rows = [(t + timedelta(minutes=5 * j), 100 + j, 102 + j, 99 + j, 101 + j) for j in range(75)]
        ln = self._run(rows)
        self.assertEqual(ln.lean, 'UP')
        got = sqlite3.connect(self.db).execute('SELECT slot, lean FROM hourly_forecast_orb_shadow').fetchall()
        self.assertEqual(got, [('13:15', 'UP')])
        self.assertEqual(self.events[-1][0], 'hourly_forecast_orb_shadow')
        self.assertEqual(ln.ref, 148)                # last close before 13:15 (bar 47), not later bars

    def test_error_is_swallowed(self):
        self.assertIsNone(self._run([('not a datetime',)]))
        self.assertEqual(self.events[-1][0], 'hourly_forecast_orb_shadow_error')


if __name__ == '__main__':
    unittest.main(verbosity=1)
