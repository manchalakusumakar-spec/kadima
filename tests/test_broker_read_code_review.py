import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('broker_read_code_review', ROOT/'multi_position_update/review_broker_read_codes.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class BrokerLogCodes(unittest.TestCase):
    def test_session_expiry_position_code(self):
        message = 'GET /getPosition secret Headers api_key abc Response {"errorcode":"AB1010", "message":"private"}'
        self.assertEqual(module.codes_from_message(message), [dict(operation='POSITIONS',code='AB1010',meaning='AMX_SESSION_EXPIRED')])
    def test_single_quoted_token_error(self):
        self.assertEqual(module.codes_from_message("/getPosition {'errorcode': 'AG8001'}")[0]['meaning'], 'INVALID_TOKEN')
    def test_order_scope(self):
        self.assertEqual(module.codes_from_message('/getOrderBook {"errorcode":"AB1004"}')[0]['operation'], 'ORDERS')
    def test_no_arbitrary_message_output(self):
        message = '/getPosition {"errorcode":"AG8001", "privatekey":"do-not-print", "message":"password123"}'
        result = json.dumps(module.codes_from_message(message))
        for forbidden in ('do-not-print','password123','privatekey','message'):
            self.assertNotIn(forbidden,result)
    def test_unknown_codes_are_not_forwarded(self):
        self.assertEqual(module.codes_from_message('/getPosition {"errorcode":"AG9999"}'),[])
    def test_nonstrings_no_route_and_oversize_ignored(self):
        for value in (None, {}, 7, 'AG8001', '{"errorcode":"AG8001"}', 'x'*(module.LIMIT+1)):
            self.assertEqual(module.codes_from_message(value),[])
    def test_code_in_unrelated_text_not_an_error(self):
        self.assertEqual(module.codes_from_message('/getPosition note AG8001'),[])
    def test_success_response_no_error(self):
        self.assertEqual(module.codes_from_message('/getPosition {"status":true,"errorcode":""}'),[])
    def test_login_classification(self):
        self.assertEqual(module.codes_from_message('/loginByPassword {"errorcode":"AB1000"}')[0]['operation'],'LOGIN')
    def test_duplicate_codes_deduplicated(self):
        self.assertEqual(len(module.codes_from_message('/getPosition {"errorcode":"AG8001", "errorcode":"AG8001"}')),1)
    def test_tail_and_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'app.log';p.write_bytes(b'hello\nworld')
            self.assertEqual(module.bounded_tail(p), b'hello\nworld')
            link=Path(tmp)/'link';link.symlink_to(p)
            with self.assertRaises(ValueError):module.bounded_tail(link)
    def test_collect_does_not_claim_live_or_flat(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(module,'ROOT',Path(tmp)), patch.object(module,'journal_evidence',return_value=([], 'UNAVAILABLE')):
            report=module.collect()
        self.assertEqual(report['result'],'NO_RECOGNIZED_CODE_IN_BOUNDED_LOGS')
        self.assertIn('NO BROKER REQUEST',report['actions'])
        self.assertIn('historical', report['warning'])

if __name__=='__main__':unittest.main()
