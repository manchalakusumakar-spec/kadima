"""Run the exact embedded diagnostic with a read-only fake broker."""
import ast
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from kadima.config import IST

ROOT = Path(__file__).resolve().parents[1]
tree = ast.parse((ROOT/'tools/check_mcx_access.sh').read_text().split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0])
CHECK = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
             and any(getattr(target, 'id', '') == 'CHECK' for target in node.targets))


class Diagnostic(unittest.TestCase):
    def run_check(self, exchanges, margin, login_error=False):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'scrip_master.json').write_text(json.dumps([
                dict(exch_seg='MCX', name='CRUDEOIL', instrumenttype='FUTCOM',
                     symbol='CRUDEOIL21SEP99FUT', token='123', expiry='21SEP2099', lotsize='100')]))
            api = Mock(spec=['getProfile', 'position', 'getMarketData', 'getMarginApi'])
            api.getProfile.return_value = dict(status=True, data=dict(exchanges=exchanges,
                                                                    clientcode='PRIVATE_CLIENT'))
            api.position.return_value = dict(status=True, data=[])
            api.getMarketData.return_value = dict(status=True, data=dict(fetched=[
                dict(symbolToken='123', exchange='MCX', ltp=9815)]))
            api.getMarginApi.return_value = margin
            session = ModuleType('kadima.session')
            session.SESSION = SimpleNamespace(api=api, refresh_token='PRIVATE_REFRESH',
                ensure=Mock(side_effect=RuntimeError('PRIVATE_EXCEPTION') if login_error else None))
            package = ModuleType('kadima')
            package.audit = SimpleNamespace(log=Mock())
            package.config = SimpleNamespace(IST=IST, DATA_DIR=tmp)
            output = io.StringIO()
            with patch.dict(sys.modules, {'kadima':package, 'kadima.session':session}), \
                    contextlib.redirect_stdout(output):
                exec(compile(CHECK, 'mcx-check', 'exec'), {})
            text = output.getvalue()
            self.assertNotIn('PRIVATE', text)
            data = json.loads(text.split('KADIMA_CHECK ', 1)[1])
            self.assertEqual(data['orders_submitted'], 0)
            return data, api

    def test_aliases_and_text_profile_formats(self):
        for exchanges in (['nse_cm','mcx_fo'], '[NSE,MCX]', ['NSE','MCX']):
            data, api = self.run_check(exchanges, dict(status=True, data=dict(totalMarginRequired=100)))
            self.assertTrue(data['mcx_listed_in_profile'])
            self.assertEqual(api.getMarginApi.call_args.args[0]['positions'][0]['price'], 9815)
            self.assertEqual(api.getMarginApi.call_args.args[0]['positions'][0]['orderType'], 'LIMIT')
            self.assertEqual(data['mcx_margin_probe']['request_order_type'], 'LIMIT')
            self.assertTrue(data['mcx_margin_probe']['ok'])

    def test_rejection_reports_code_without_raw_message(self):
        data, _ = self.run_check(['NSE','PRIVATE_LABEL'],
            dict(status=False, errorcode='AB4022', message='Null or Empty Margin Data PRIVATE_CLIENT'))
        probe = data['mcx_margin_probe']
        self.assertFalse(probe['ok'])
        self.assertEqual(probe['broker_response']['errorcode'], 'AB4022')
        self.assertEqual(probe['broker_response']['message_categories'], ['margin_or_funds'])
        self.assertEqual(data['profile_exchange_labels'], ['NSE'])

    def test_login_failure_stops_read_requests_and_hides_exception(self):
        data, api = self.run_check([], {}, login_error=True)
        self.assertEqual(data['diagnostic_error_type'], 'RuntimeError')
        self.assertEqual(api.method_calls, [])


if __name__ == '__main__':
    unittest.main()
