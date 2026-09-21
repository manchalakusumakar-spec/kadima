"""Regression: API timestamps are produced after the request begins."""
from datetime import timedelta
import unittest
import test_session_auto as suite


class ReceiptTimeTests(unittest.TestCase):
    def test_receipt_clock_avoids_false_future_timestamp(self):
        case=suite.RunnerTests(methodName='runTest')
        case.setUp()
        self.addCleanup(case.doCleanups)
        old=case.client.call
        def call(action,payload=None):
            if action in ('global','stock'):
                suite.Clock.current=suite.MON+timedelta(seconds=5)
                case.client.gd['ts']=case.client.sd['ts']=suite.Clock.current.isoformat()
                case.client.sd['worker']['ts']=suite.Clock.current.isoformat()
                for record in case.client.gd['system'].values():
                    record['ts']=suite.Clock.current.isoformat()
            return old(action,payload)
        case.client.call=call
        self.assertEqual(case.run_once(),'AUTO_AND_STOCK_ARMED_SUBJECT_TO_ENTRY_GATES')

if __name__=='__main__':unittest.main()
