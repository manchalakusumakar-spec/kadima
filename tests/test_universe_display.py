import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from datetime import datetime

from kadima import universe_display as display
from kadima import stock_universe_scanner as scanner
from kadima.config import IST
from kadima.option_chain import OptionQuote
from kadima.strike_selector import RankedStrike

NOW=datetime(2026,9,16,10,0,tzinfo=IST)


class UniverseDisplay(unittest.TestCase):
    def chain(self):
        q=OptionQuote('1','STOCK30SEP26C100',100,'CE',ltp=10,oi=100000000,
                      volume=100000000,delta=.5,bid=9.95,ask=10.05)
        return SimpleNamespace(_last_poll=time.monotonic(),_expiry='30SEP2026',
            index=SimpleNamespace(opt_exchange='NFO'),quotes={'1':q},walls=lambda:{'CE':[],'PE':[]})

    def test_real_selector_exports_contract_identity_and_timestamp(self):
        with patch.object(display,'now_ist',return_value=NOW):
            contract,status=display.selected_contract(self.chain(),'CE',100,14,NOW.isoformat())
        self.assertEqual(status,'selected')
        self.assertEqual((contract['strike'],contract['type'],contract['token'],contract['expiry']),
                         (100,'CE','1','2026-09-30'))
        self.assertEqual(contract['updated_at'],NOW.isoformat())

    def test_stale_expired_illiquid_and_directionless_have_no_fabricated_strike(self):
        for mode in ('stale','expired','illiquid','directionless'):
            with self.subTest(mode=mode),patch.object(display,'now_ist',return_value=NOW):
                chain=self.chain();direction='CE'
                if mode=='stale':chain._last_poll-=60
                if mode=='expired':chain._expiry='15SEP2026'
                if mode=='illiquid':chain.quotes['1'].ask=20
                if mode=='directionless':direction=None
                contract,status=display.selected_contract(chain,direction,100,14,NOW.isoformat())
                self.assertIsNone(contract);self.assertNotEqual(status,'selected')

    def test_display_selection_does_not_mutate_execution_quotes(self):
        chain=self.chain()
        def pick(view,*args,**kwargs):
            view.quotes['1'].delta=.6
            return RankedStrike(view.quotes['1'],80,[])
        with patch.object(display,'now_ist',return_value=NOW),patch.object(display.strike_selector,'best',side_effect=pick):
            display.selected_contract(chain,'CE',100,14,NOW.isoformat())
        self.assertEqual(chain.quotes['1'].delta,.5)

    def test_legacy_snapshot_includes_selected_contract(self):
        s=scanner.UniverseScanner.__new__(scanner.UniverseScanner)
        s.universe=[{'name':'STOCK'}];s._last_universe_quotes={}
        s._tracked={'STOCK':SimpleNamespace(spot=100)}
        s._last_score={'STOCK':dict(tier='WATCH',score=65,direction='CE',ts=NOW.isoformat(),
                                  best_contract={'strike':100,'type':'CE'},strike_status='selected')}
        with tempfile.TemporaryDirectory() as tmp,patch.object(scanner,'SNAPSHOT_PATH',tmp+'/snapshot.json'),patch('kadima.celestial.health'):
            s._save_snapshot()
            data=json.loads(Path(tmp,'snapshot.json').read_text())
        self.assertEqual(data['tracked'][0]['best_contract'],{'strike':100,'type':'CE'})
        self.assertEqual(data['tracked'][0]['strike_status'],'selected')

class PremiumPace(unittest.TestCase):
    def setUp(self):
        self.chain=UniverseDisplay().chain()

    def sample(self, seconds, price=10, direction='CE'):
        from datetime import timedelta
        now=NOW+timedelta(seconds=seconds)
        q=self.chain.quotes['1'];q.ltp=price;q.bid=price*.999;q.ask=price*1.001
        with patch.object(display,'now_ist',return_value=now):
            return display.selected_contract(self.chain,direction,100,14,now.isoformat())[0]

    def test_five_minute_pace_exported_for_same_contract(self):
        self.assertEqual(self.sample(0)['pace_status'],'warming_up')
        for second in range(30,301,30):c=self.sample(second,10+second/300)
        self.assertAlmostEqual(c['premium_move_pct'],10)
        self.assertEqual(c['baseline_at'],NOW.isoformat())
        self.assertEqual(c['pace_status'],'ready')

    def test_history_collected_before_direction_confirmation(self):
        for second in range(0,300,30):self.sample(second,direction=None)
        self.assertEqual(self.sample(300,11)['pace_status'],'ready')

    def test_gap_and_contract_change_reset_history(self):
        for second in range(0,301,30):self.sample(second)
        self.assertEqual(self.sample(420)['pace_status'],'warming_up')
        self.chain.quotes['1'].symbol='DIFFERENT'
        self.assertEqual(self.sample(450)['pace_status'],'warming_up')
        self.assertEqual(len(self.chain._display_premiums),1)

    def test_flat_and_falling_are_not_positive_eta(self):
        for second in range(0,301,30):c=self.sample(second)
        self.assertEqual(c['pace_status'],'not_rising')
        self.assertEqual(self.sample(330,9)['pace_status'],'not_rising')

    def test_invalid_quote_breaks_continuity(self):
        for second in range(0,301,30):self.sample(second)
        self.chain.quotes['1'].bid=0
        from datetime import timedelta
        now=NOW+timedelta(seconds=330)
        with patch.object(display,'now_ist',return_value=now):display.premium_pace(self.chain,now.isoformat())
        self.assertEqual(self.sample(360)['pace_status'],'warming_up')
