import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('forecast_panel_update',Path(__file__).resolve().parents[1]/'forecaster_update/update_dashboard.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class PanelTests(unittest.TestCase):
    def test_patch_preserves_existing_controls_and_is_idempotent(self):
        old='<button>Existing control</button><section class="panel wide"><h2>Data Coverage</h2></section><script>function renderAll(){renderPositions()}</script>'
        new=m.transform(old)
        self.assertIn('<button>Existing control</button>',new)
        self.assertIn('renderPositions()',new)
        self.assertIn('id="forecastPanel"',new)
        self.assertEqual(new,m.transform(new))
    def test_unknown_or_partial_layout_rejected(self):
        for source in ('<html>different dashboard</html>','<div id="forecastPanel"></div>'):
            with self.assertRaises(ValueError):m.transform(source)

if __name__=='__main__':unittest.main()
