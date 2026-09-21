from datetime import date, datetime, timedelta, timezone
import types
import unittest
from unittest.mock import patch

from kadima import gap_and_go as gap
from kadima import gap_baseline as baseline

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 9, 21, 9, 40, tzinfo=IST)


def bar(minute, o, h, l, c, v, day=21):
    return dict(start_ts=datetime(2026, 9, day, 9, minute, tzinfo=IST).isoformat(),
                open=o, high=h, low=l, close=c, volume=v)


def good_bars():
    return [
        bar(15, 102.0, 104.0, 101.8, 103.8, 2500),
        bar(20, 103.8, 104.5, 103.2, 104.1, 2200),
        bar(25, 104.0, 104.1, 103.2, 103.6, 900),
        bar(30, 103.7, 104.9, 103.6, 104.8, 1800),
    ]


def confirms(**patch_):
    value = dict(bull_shield=True, gamma_oi=True, futures_available=True,
                 futures_ok=True, news_clear=True, macro_clear=True,
                 ce_wall_ratio=1.10, ce_wall_distance=20.0,
                 next_resistance=110.0)
    value.update(patch_)
    return value


class BaselineTests(unittest.TestCase):
    def test_previous_close_uses_last_prior_session(self):
        rows = [
            dict(start_ts=datetime(2026, 9, 18, 15, 25, tzinfo=IST).isoformat(),
                 close=99, volume=1),
            dict(start_ts=datetime(2026, 9, 18, 15, 30, tzinfo=IST).isoformat(),
                 close=100, volume=1),
            dict(start_ts=datetime(2026, 9, 21, 9, 15, tzinfo=IST).isoformat(),
                 close=102, volume=1),
        ]
        self.assertEqual(baseline.previous_close(rows, date(2026, 9, 21)), 100)

    def test_same_time_median_is_not_todays_generic_average(self):
        rows = []
        for day, v1, v2 in [(14, 900, 1000), (15, 1000, 1100), (16, 1100, 1200),
                            (17, 5000, 6000)]:
            rows.extend([bar(15, 1, 1, 1, 1, v1, day), bar(20, 1, 1, 1, 1, v2, day)])
        value = baseline.same_time_volume_baseline(rows, NOW.replace(hour=9, minute=15),
                                                   slots=2)
        self.assertEqual(value, 2200.0)
        self.assertAlmostEqual(baseline.relative_volume(4400, value), 2.0)

    def test_insufficient_sessions_fail_closed(self):
        rows = [bar(15, 1, 1, 1, 1, 1000, 18)]
        self.assertIsNone(baseline.same_time_volume_baseline(
            rows, NOW.replace(hour=9, minute=15)))


class DetectorTests(unittest.TestCase):
    def evaluate(self, bars=None, **kwargs):
        return gap.evaluate("PATANJALI", bars or good_bars(),
            previous_close=kwargs.pop("previous_close", 100.0),
            session_vwap=kwargs.pop("session_vwap", 103.2),
            prior_vwap=kwargs.pop("prior_vwap", 103.1),
            baseline_volume=kwargs.pop("baseline_volume", 2000.0),
            confirmations=kwargs.pop("confirmations", confirms()),
            now=kwargs.pop("now", NOW), **kwargs)

    def test_full_sequence_reaches_confirmed_but_never_live_entry(self):
        row = self.evaluate()
        self.assertTrue(row["eligible"], row)
        self.assertEqual(row["stage"], gap.Stage.CONFIRMED.value)
        self.assertEqual(row["mode"], "PAPER_ONLY")
        self.assertGreaterEqual(row["metrics"]["relative_volume"], 2)
        self.assertEqual(row["metrics"]["planned_rr"], 2.0)

    def test_watch_is_never_tradeable(self):
        row = self.evaluate(previous_close=101.5)
        self.assertFalse(row["eligible"])
        self.assertEqual(row["stage"], gap.Stage.WATCH.value)

    def test_opening_relative_volume_requires_2x(self):
        row = self.evaluate(baseline_volume=3000)
        self.assertIn("opening_relative_volume_below_2x", row["blocked_reasons"])

    def test_gap_must_remain_substantially_unfilled(self):
        rows = good_bars()
        rows[2] = dict(rows[2], low=100.8)
        row = self.evaluate(rows)
        self.assertIn("gap_substantially_filled", row["blocked_reasons"])

    def test_first_pullback_cannot_exceed_half_impulse(self):
        rows = good_bars()
        rows[2] = dict(rows[2], low=102.9)
        row = self.evaluate(rows)
        self.assertIn("pullback_deeper_than_50pct", row["blocked_reasons"])

    def test_pullback_volume_must_contract(self):
        rows = good_bars()
        rows[2] = dict(rows[2], volume=2500)
        row = self.evaluate(rows)
        self.assertIn("pullback_volume_not_contracting", row["blocked_reasons"])

    def test_orb_break_requires_closed_breakout_bar(self):
        rows = good_bars()
        rows[-1] = dict(rows[-1], high=104.4, close=104.3)
        row = self.evaluate(rows)
        self.assertEqual(row["stage"], gap.Stage.PULLBACK_HOLD.value)
        self.assertIn("orb_or_pullback_high_break_unconfirmed", row["blocked_reasons"])

    def test_shield_and_gamma_are_both_mandatory(self):
        a = self.evaluate(confirmations=confirms(bull_shield=False))
        b = self.evaluate(confirmations=confirms(gamma_oi=False))
        self.assertIn("bull_shield_unconfirmed", a["blocked_reasons"])
        self.assertIn("gamma_oi_unconfirmed", b["blocked_reasons"])

    def test_futures_confirmation_required_only_when_available(self):
        blocked = self.evaluate(confirmations=confirms(futures_ok=False))
        allowed = self.evaluate(confirmations=confirms(futures_available=False,
                                                       futures_ok=False))
        self.assertIn("futures_price_oi_unconfirmed", blocked["blocked_reasons"])
        self.assertTrue(allowed["eligible"], allowed)

    def test_macro_and_news_fail_closed(self):
        macro = self.evaluate(confirmations=confirms(macro_clear=False))
        news = self.evaluate(confirmations=confirms(news_clear=False))
        self.assertIn("macro_event_suppression_active_or_unknown", macro["blocked_reasons"])
        self.assertIn("news_gate_not_clear", news["blocked_reasons"])

    def test_fresh_nearby_ce_wall_150pct_blocks_entry(self):
        row = self.evaluate(confirmations=confirms(ce_wall_ratio=1.5,
                                                   ce_wall_distance=40.0))
        self.assertFalse(row["eligible"])
        self.assertIn("nearby_ce_wall_strengthened_150pct", row["blocked_reasons"])

    def test_120pct_wall_is_penalty_not_automatic_reject(self):
        row = self.evaluate(confirmations=confirms(ce_wall_ratio=1.3,
                                                   ce_wall_distance=40.0))
        self.assertTrue(row["eligible"], row)
        self.assertTrue(row["metrics"]["wall_penalty"])

    def test_known_resistance_must_leave_two_r_room(self):
        row = self.evaluate(confirmations=confirms(next_resistance=106.0))
        self.assertIn("known_resistance_blocks_2r", row["blocked_reasons"])

    def test_vwap_must_hold_or_rise(self):
        row = self.evaluate(session_vwap=102.8, prior_vwap=103.2)
        self.assertIn("session_vwap_falling", row["blocked_reasons"])

    def test_outside_0915_1030_never_confirms(self):
        row = self.evaluate(now=NOW.replace(hour=10, minute=31))
        self.assertEqual(row["stage"], gap.Stage.WATCH.value)
        self.assertFalse(row["eligible"])


class ContractTests(unittest.TestCase):
    def test_selector_restricts_ce_to_atm_or_one_step_itm(self):
        quote = lambda strike: types.SimpleNamespace(strike=strike)
        rows = [types.SimpleNamespace(rejected=None, quote=quote(s)) for s in (90, 100, 110)]
        chain = types.SimpleNamespace(atm=100,
            index=types.SimpleNamespace(strike_step=10))
        fake = types.SimpleNamespace(rank=lambda *a: rows)
        with patch("kadima.strike_selector.rank", fake.rank):
            selected = gap.ranked_atm_or_itm_calls(chain, 101, 7)
        self.assertEqual([r.quote.strike for r in selected], [90, 100])

    def test_paper_book_forbids_averaging_or_duplicate_symbol(self):
        row = DetectorTests().evaluate()
        book = gap.PaperBook()
        book.enter(row, contract="PATANJALI100CE", quantity=10,
                   option_price=12.5, now=NOW)
        with self.assertRaisesRegex(ValueError, "one_position_per_symbol"):
            book.enter(row, contract="PATANJALI90CE", quantity=10,
                       option_price=15, now=NOW)

    def test_paper_book_rejects_watch_signal(self):
        row = DetectorTests().evaluate(previous_close=101.5)
        with self.assertRaisesRegex(ValueError, "requires_confirmed"):
            gap.PaperBook().enter(row, contract="X", quantity=1,
                                  option_price=10, now=NOW)


if __name__ == "__main__":
    unittest.main()
