"""Synthetic as-of tests; these do not validate historical profitability."""
import copy
import unittest
from datetime import datetime, timedelta
from kadima.config import IST
from kadima import structure_levels as s

START=datetime(2026,9,23,9,15,tzinfo=IST)


def history():
    # Confirmed 90->110 up-leg, then a separate support/reaction near midpoint.
    values=[(100,102,98),(98,100,95),(94,98,90),(99,102,94),
            (103,106,98),(107,109,103),(108,110,105),(105,108,103),
            (103,106,101),(104,106,102),(105,107,103),(107,109,104),
            (106,108,101.5),(106,108,104)]
    return [dict(start_ts=(START+timedelta(minutes=5*i)).isoformat(),interval=5,
                 closed=1,open=c,close=c,high=h,low=l) for i,(c,h,l) in enumerate(values)]


class StructureLevels(unittest.TestCase):
    def setUp(self):
        self.rows=history()
        self.now=START+timedelta(minutes=5*len(self.rows))

    def test_known_fibonacci_arithmetic(self):
        levels=s.fibonacci_levels(self.rows[:9],now=START+timedelta(minutes=45))
        self.assertEqual(len(levels),5)
        self.assertAlmostEqual(levels[0]['price'],105.28)
        self.assertEqual(levels[2]['price'],100)
        self.assertEqual(levels[0]['available_at'],(START+timedelta(minutes=45)).isoformat())

    def test_no_pivot_before_second_right_close(self):
        now=START+timedelta(minutes=44,seconds=59)
        self.assertEqual(s.fibonacci_levels(self.rows,now=now),[])
        self.assertEqual(s.fibonacci_levels(self.rows,now=now),s.fibonacci_levels(self.rows[:8],now=now))

    def test_down_leg_mirrors_up_leg(self):
        rows=copy.deepcopy(self.rows[:9])
        for r in rows:
            r['open'],r['close']=200-r['open'],200-r['close']
            r['low'],r['high']=200-r['high'],200-r['low']
        levels=s.fibonacci_levels(rows,now=START+timedelta(minutes=45))
        self.assertAlmostEqual(levels[0]['price'],94.72)
        self.assertEqual(levels[0]['role'],'resistance')

    def test_zone_needs_post_confirmation_reaction_and_fib_confluence(self):
        zones=s.build_zones(self.rows,asset='NIFTY',now=self.now,spot=106,atr=10)
        self.assertTrue(zones)
        z=next(z for z in zones if z['low']==100)
        self.assertEqual(z['references'],['pivot','fibonacci'])
        self.assertEqual(z['confirmed_reactions'],1)
        self.assertEqual(z['reaction_at'],(START+timedelta(minutes=65)).isoformat())
        self.assertLess(z['available_at'],z['reaction_at'])
        self.assertTrue(z['experimental'])
        early=s.build_zones(self.rows[:11],asset='NIFTY',now=START+timedelta(minutes=55),spot=106,atr=10)
        self.assertFalse(any(z['low']==100 for z in early))

    def test_stable_zone_id_and_no_future_leakage(self):
        kw=dict(asset='NIFTY',now=self.now,spot=106,atr=10)
        first=s.build_zones(self.rows,**kw)
        future=dict(self.rows[-1],start_ts=self.now.isoformat(),low=1,high=999,close=2,open=3)
        self.assertEqual(first,s.build_zones(self.rows+[future],**kw))
        later=s.build_zones(self.rows,**dict(kw,now=self.now+timedelta(seconds=1)))
        self.assertEqual([z['id'] for z in first],[z['id'] for z in later])

    def test_closed_break_invalidates_support(self):
        self.rows[-1].update(low=98,close=99,open=99)
        zones=s.build_zones(self.rows,asset='NIFTY',now=self.now,spot=106,atr=10)
        self.assertFalse(any(z['low']==100 for z in zones))

    def test_consecutive_touches_count_once(self):
        self.rows[-1]['low']=101.5
        zones=s.build_zones(self.rows,asset='NIFTY',now=self.now,spot=106,atr=10)
        self.assertEqual(next(z for z in zones if z['low']==100)['confirmed_reactions'],1)

    def test_duplicates_gaps_nan_staleness_rejected(self):
        cases=[self.rows+[self.rows[-1]], self.rows[:5]+self.rows[6:]]
        bad=copy.deepcopy(self.rows);bad[3]['low']=float('nan');cases.append(bad)
        for rows in cases:
            with self.assertRaises(ValueError):
                s.fibonacci_levels(rows,now=self.now)
        with self.assertRaises(ValueError):
            s.fibonacci_levels(self.rows,now=self.now+timedelta(minutes=6))

    def test_custom_sequence_is_disabled_and_preserved(self):
        self.assertFalse(s.CUSTOM_LEVELS_ENABLED)
        self.assertEqual(s.CUSTOM_LEVEL_CANDIDATES,(18,33,44,55,66,77,88,103,108,122,133,135,155))
