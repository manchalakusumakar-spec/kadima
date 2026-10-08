"""Regression tests for F&O stock venue resolution in instrument_master."""

import unittest

from kadima import instrument_master as im


def absorb(rows):
    lot, sym, universe, spot = {}, {}, {}, {}
    for row in rows:
        im._absorb_row(row, lot, sym, universe, spot)
    return lot, sym, universe, spot


def opt(name, token, exchange, lot):
    return {
        "exch_seg": exchange,
        "instrumenttype": "OPTSTK",
        "symbol": f"{name}{exchange}{token}CE",
        "name": name,
        "token": str(token),
        "lotsize": str(lot),
    }


class InstrumentUniverseVenueTests(unittest.TestCase):
    def test_nfo_survives_later_bfo_duplicate(self):
        lot, sym, universe, _ = absorb([
            opt("AAA", 11, "NFO", 100),
            opt("AAA", 12, "BFO", 200),
        ])
        self.assertEqual(universe["AAA"]["opt_exchange"], "NFO")
        self.assertEqual(universe["AAA"]["lot_size"], 100)
        self.assertEqual(lot["11"], 100)
        self.assertEqual(lot["12"], 200)

    def test_later_nfo_replaces_earlier_bfo(self):
        _, _, universe, _ = absorb([
            opt("AAA", 12, "BFO", 200),
            opt("AAA", 11, "NFO", 100),
        ])
        self.assertEqual(universe["AAA"], {
            "lot_size": 100,
            "opt_exchange": "NFO",
        })

    def test_bfo_only_underlying_remains_bfo(self):
        _, _, universe, _ = absorb([
            opt("BFOONLY", 21, "BFO", 50),
            opt("BFOONLY", 22, "BFO", 50),
        ])
        self.assertEqual(universe["BFOONLY"], {
            "lot_size": 50,
            "opt_exchange": "BFO",
        })

    def test_equity_spot_resolution_unchanged(self):
        _, _, universe, spot = absorb([
            {
                "exch_seg": "NSE",
                "instrumenttype": "",
                "symbol": "AAA-EQ",
                "name": "AAA",
                "token": "1",
            },
            opt("AAA", 11, "NFO", 100),
            opt("AAA", 12, "BFO", 200),
        ])
        self.assertEqual(spot["AAA"], {"token": "1", "exchange": "NSE"})
        self.assertEqual(universe["AAA"]["opt_exchange"], "NFO")


if __name__ == "__main__":
    unittest.main()
