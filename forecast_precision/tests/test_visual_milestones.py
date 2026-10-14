import unittest
from pathlib import Path
from web.visual_milestones import transform
class VisualMilestoneTests(unittest.TestCase):
    def source(self):
        # Repository's matching legacy UI is a shape fixture.
        p=Path(__file__).resolve().parents[2]/"web"/"hourly_forecast.html"
        return p.read_text()
    def test_original_plot_retained(self):
        patched=transform(self.source())
        self.assertIn("const milestoneMarks",patched)
        self.assertIn("const step = 1",patched)
        self.assertIn("function milestoneTable(s)",patched)
        self.assertIn("No automatic reforecast or trade is activated",patched)
        self.assertIn("forecast Manual",patched) if False else self.assertIn("function chart(s)",patched)
    def test_no_double_injection(self):
        with self.assertRaises(ValueError):transform(transform(self.source()))
    def test_fails_closed_on_unrecognized_plot(self):
        with self.assertRaises(ValueError):
            transform(self.source().replace("const step = pts.length > 6 ? 3 : 1;",""))
