import ast
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from kadima import gap_live

IST=timezone(timedelta(hours=5,minutes=30))
NOW=datetime(2026,9,21,9,40,tzinfo=IST)


class HelperTests(unittest.TestCase):
    def test_shield_score_uses_existing_weights(self):
        snap=types.SimpleNamespace(vwap=100,ema_fast=103,ema_slow=101,rsi=60,
            avg_vol=1000,vol=2000,failed_breakdown=False)
        self.assertEqual(gap_live._shield_score(snap,104,True),95)

    def test_nearest_future_is_nearest_active_futstk(self):
        master=types.SimpleNamespace(rows=[
            {'exch_seg':'NFO','instrumenttype':'FUTSTK','name':'ABC',
             'expiry':'29Sep2026','token':'2','symbol':'ABCSEP','permitted_to_trade':True},
            {'exch_seg':'NFO','instrumenttype':'FUTSTK','name':'ABC',
             'expiry':'27Oct2026','token':'3','symbol':'ABCOCT','permitted_to_trade':True},
        ])
        row=gap_live._nearest_future(master,'ABC',NOW.date())
        self.assertEqual(row['token'],'2')

    def test_macro_gate_is_fail_closed_and_blocks_event_day(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'macro.json'
            path.write_text(json.dumps(dict(ts=NOW.isoformat(),economic_calendar=[
                {'date':'2026-09-21','end_date':'2026-09-21','name':'TEST'}])))
            fake=types.SimpleNamespace(SNAPSHOT_PATH=str(path))
            with patch.dict('sys.modules',{'kadima.macro_watch':fake}):
                self.assertFalse(gap_live._macro_clear(NOW))

    def test_live_worker_has_no_direct_order_transport(self):
        tree=ast.parse(Path('kadima/gap_live.py').read_text())
        blocked_imports=set()
        blocked_calls=set()
        for node in ast.walk(tree):
            if isinstance(node,ast.ImportFrom):
                blocked_imports.add((node.module or '').split('.')[-1])
            elif isinstance(node,ast.Import):
                blocked_imports.update(a.name.split('.')[-1] for a in node.names)
            elif isinstance(node,ast.Call):
                fn=node.func
                name=fn.attr if isinstance(fn,ast.Attribute) else fn.id if isinstance(fn,ast.Name) else None
                if name in {'place_limit_order','submit'}:
                    blocked_calls.add(name)
        self.assertFalse(blocked_imports & {'angel_orders','execution_ledger'})
        self.assertFalse(blocked_calls)


if __name__=='__main__':
    unittest.main()
