"""Offline test for the 09-Nov autofix (no broker, no network): nightly bhavcopy retry.

NSE publishes the F&O bhavcopy in the evening, after the 16:05 nightly run, so every session
29 Oct-09 Nov was 'both_archives_404' once and never asked again (bhav_options empty). The
previous sessions are now retried the next evening until 'ok'.

    KADIMA_DATA_DIR=$(mktemp -d) python3 tools/test_forecast_autofix_20261109.py
"""
import io
import os
import sqlite3
import sys
import tempfile
import urllib.error
import zipfile
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('KADIMA_DATA_DIR', tempfile.mkdtemp())
sys.path.insert(0, '.')
from kadima import hourly_forecast as HF                                 # noqa: E402
from kadima import forecast_chain_data as F                              # noqa: E402
from kadima import forecast_bhavcopy as B                                # noqa: E402
from kadima import market_session_gate as G                              # noqa: E402

FAILS = []


def check(name, cond):
    print(('PASS ' if cond else 'FAIL ') + name)
    if not cond:
        FAILS.append(name)


def database():
    c = sqlite3.connect(':memory:')
    c.row_factory = sqlite3.Row
    c.executescript(F.SCHEMA)
    return c


POLICY = {'holidays': []}

# ---------------------------------------------------------------- 2. bhavcopy retry
def bhav_zip(day):
    header = ('TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,'
              'StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,'
              'SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,'
              'Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4')
    d = day.isoformat()
    line = (f'{d},{d},FO,NSE,IDO,1,,NIFTY,,2026-11-10,2026-11-10,25000,CE,NIFTY25000CE,10,12,9,11,11,10,'
            f'25044,11,5000,100,200,1,1,F1,75,,,,,')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('bhav.csv', header + '\n' + line + '\n')
    return buf.getvalue()


def bhav_run(available, cached):
    c = database()
    c.execute('CREATE TABLE IF NOT EXISTS nightly_status (step TEXT NOT NULL, day TEXT NOT NULL, status TEXT NOT NULL, '
              'rows_saved INTEGER NOT NULL, checked_at TEXT NOT NULL, detail TEXT NOT NULL, PRIMARY KEY(step,day))')
    c.execute("INSERT INTO nightly_status VALUES ('bhavcopy_backfill','2026-10-20','ok',0,'2026-10-20T16:05:00+05:30','x')")
    for d, status, checked in cached:
        c.execute('INSERT INTO nightly_status VALUES (?,?,?,?,?,?)', ('bhavcopy', d, status, 0, checked, 'both_archives_404'))
    asked = []

    class Resp:
        def __init__(self, data):
            self.data, self.headers = data, {'Content-Type': 'application/zip'}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return self.data

    def opener(req, timeout=20):
        url = req.full_url
        asked.append(url)
        for d in available:
            if d.strftime('%Y%m%d') in url:
                return Resp(bhav_zip(d))
        raise urllib.error.HTTPError(url, 404, 'nf', {}, None)
    now = datetime(2026, 11, 10, 16, 5, 3, tzinfo=HF.IST)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now
    with patch.object(G, 'read_json', return_value=POLICY), \
            patch.object(G, 'validate_policy', side_effect=lambda p: p), \
            patch.object(B, 'datetime', FrozenDatetime), \
            patch('builtins.print'):
        results = B.run_nightly(c, now, sleeper=lambda s: None, opener=opener)
    status = {r['day']: r['status'] for r in c.execute("SELECT day,status FROM nightly_status WHERE step='bhavcopy'")}
    return status, asked, c.execute('SELECT COUNT(*) FROM bhav_options').fetchone()[0], results


# Live 09/10-Nov: 06 Nov and 09 Nov were each checked once at 16:05 (404); both are on the
# archive a day later. Today's (10 Nov) is not published yet.
status, asked, saved, _ = bhav_run(
    available=[date(2026, 11, 6), date(2026, 11, 9)],
    cached=[('2026-11-06', 'missing', '2026-11-06T16:05:03+05:30'),
            ('2026-11-09', 'missing', '2026-11-09T16:05:03+05:30')])
check('previous sessions missing at 16:05 are fetched the next evening: %s' % status,
      status.get('2026-11-09') == 'ok' and status.get('2026-11-06') == 'ok' and saved >= 2)
check("today's unpublished file stays 'missing' (retried tomorrow)", status.get('2026-11-10') == 'missing')

status, asked, saved, _ = bhav_run(
    available=[date(2026, 11, 9)],
    cached=[('2026-11-06', 'ok', '2026-11-07T16:05:03+05:30'),
            ('2026-11-09', 'missing', '2026-11-10T10:00:00+05:30')])
check('an ok day is never fetched again, a day already checked today is not re-asked (%d requests)' % len(asked),
      not any('20261106' in u for u in asked) and not any('20261109' in u for u in asked))

print('ALL FORECAST AUTOFIX 20261109 TESTS PASS' if not FAILS else 'FAILED: %d' % len(FAILS))
sys.exit(1 if FAILS else 0)
