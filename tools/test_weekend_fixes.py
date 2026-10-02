"""Offline checks for the 2026-10-02 safety fixes. No network, no broker.

    KADIMA_DATA_DIR=/tmp/kadima-test python tools/test_weekend_fixes.py
"""
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

os.environ.setdefault("KADIMA_DATA_DIR", tempfile.mkdtemp(prefix="kadima-test-"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

FAILED = []


def check(name, cond):
    print(("  [ok] " if cond else "  [FAIL] ") + name)
    if not cond:
        FAILED.append(name)


# ------------------------------------------------------------ quote priority
print("shared quote queue serves exits first")
from kadima import rate_limiter as rl

lim = rl.SharedQuoteLimiter(path=os.path.join(tempfile.mkdtemp(), "q.sqlite3"), interval=0.3)
check("first call granted at once", lim.wait_and_allow(timeout_sec=1.0, priority=rl.SCAN))
order, threads = [], []


def waiter(tag, prio, delay):
    time.sleep(delay)
    if lim.wait_and_allow(timeout_sec=5.0, priority=prio):
        order.append(tag)


# Three scanners queue first; an exit arrives later but must jump ahead.
for i in range(3):
    threads.append(threading.Thread(target=waiter, args=(f"scan{i}", rl.SCAN, 0.01 * i)))
threads.append(threading.Thread(target=waiter, args=("exit", rl.EXIT, 0.1)))
threads.append(threading.Thread(target=waiter, args=("chain", rl.CHAIN, 0.05)))
for t in threads:
    t.start()
for t in threads:
    t.join()
check(f"exit served first (order={order})", order[0] == "exit")
check("chain served before scanners", order.index("chain") < min(order.index(f"scan{i}") for i in range(3)))
check("all five eventually served", len(order) == 5)

with rl.quote_priority(rl.EXIT):
    check("context sets thread priority", rl.current_priority() == rl.EXIT)
check("priority restored after block", rl.current_priority() == rl.DEFAULT_PRIORITY)
check("zero timeout never grants", lim.wait_and_allow(timeout_sec=0) is False)

# ----------------------------------------------------------- batched polls
print("same-exchange ladders share quote calls")
from kadima import option_chain as oc


def fake_chain(name, exch, n, start):
    idx = SimpleNamespace(name=name, opt_exchange=exch)
    ch = oc.OptionChain.__new__(oc.OptionChain)
    ch.index = idx
    ch.quotes = {str(start + i): oc.OptionQuote(str(start + i), f"{name}{i}", 100.0 + i,
                                                "CE" if i % 2 else "PE") for i in range(n)}
    ch.velocity = oc.OIVelocityTracker(name)
    ch._expiry, ch._last_poll = None, 0.0
    return ch


nifty, bank, midcp = (fake_chain(n, "NFO", 22, k) for n, k in
                      (("NIFTY", 1000), ("BANKNIFTY", 2000), ("MIDCPNIFTY", 3000)))
sensex = fake_chain("SENSEX", "BFO", 22, 4000)
big = fake_chain("BIG", "NFO", 60, 5000)
batches = oc.plan_batches([nifty, sensex, bank, midcp, big])
names = [[c.index.name for c in b] for b in batches]
check(f"NFO pairs packed, BFO separate ({names})",
      names == [["NIFTY", "BANKNIFTY"], ["SENSEX"], ["MIDCPNIFTY"], ["BIG"]])
check("no batch exceeds 50 tokens", all(sum(len(c.quotes) for c in b) <= 50
                                        for b in batches if len(b) > 1))

calls = []


def rows_for(tokens):
    return [dict(symbolToken=t, exchange="NFO", ltp=10.0, opnInterest=1000,
                 netChangeOpnInterest=5, tradeVolume=500,
                 depth=dict(buy=[dict(price=9.9, quantity=50)],
                            sell=[dict(price=10.1, quantity=50)])) for t in tokens]


class FakeAPI:
    def getMarketData(self, mode=None, exchangeTokens=None):
        calls.append(exchangeTokens)
        toks = [t for v in exchangeTokens.values() for t in v]
        return {"status": True, "data": {"fetched": rows_for(toks)}}


oc.SESSION.ensure = lambda *a, **k: oc.SESSION
oc.SESSION.api = FakeAPI()
res = oc.poll_many([nifty, bank])
check("one broker call for two ladders", len(calls) == 1 and len(calls[0]["NFO"]) == 44)
check("both ladders accepted", res[id(nifty)] and res[id(bank)])
check("each ladder keeps only its own tokens",
      set(nifty.quotes) == {str(1000 + i) for i in range(22)} and nifty.quotes["1001"].ltp == 10.0)


class DropAPI(FakeAPI):
    def getMarketData(self, mode=None, exchangeTokens=None):
        toks = [t for v in exchangeTokens.values() for t in v if t != "2005"]
        return {"status": True, "data": {"fetched": rows_for(toks)}}


oc.SESSION.api = DropAPI()
res = oc.poll_many([nifty, bank])
check("incomplete ladder rejected, complete one still accepted",
      res[id(nifty)] is True and res[id(bank)] is False)


class BoomAPI:
    def getMarketData(self, *a, **k):
        raise TimeoutError("budget")


oc.SESSION.api = BoomAPI()
res = oc.poll_many([nifty, bank])
check("failed call degrades both ladders", res == {id(nifty): False, id(bank): False}
      and nifty.velocity.degraded_until and bank.velocity.degraded_until)

# ------------------------------------------------------------ candles
print("forming candles stay in memory")
from kadima import candles, storage

writes = []
real_upsert = storage.upsert_candle
candles.storage.upsert_candle = lambda *a, **k: writes.append(a[-1])
closed = []
b = candles.CandleBuilder("T1", on_close=closed.append)
t0 = datetime(2026, 10, 5, 9, 15, 5, tzinfo=oc.IST)
for i in range(40):                     # 40 ticks, 3s apart, inside 09:15-09:17
    b.add_tick(t0 + timedelta(seconds=3 * i), 100 + i, 1000 + i)
check("no SQLite writes for forming bars (only closed ones)", False not in writes)
b.add_tick(t0 + timedelta(minutes=5), 200, 5000)   # 09:20 closes 1m/3m/5m bars
check("closing bars are written, flagged closed", writes and all(w is True for w in writes))
check("on_close still fires for each closed interval",
      sorted({c.interval for c in closed}) == [1, 3, 5])
candles.storage.upsert_candle = real_upsert

# -------------------------------------------------------- signal worker
print("candle-close work leaves the tick thread")
from kadima import engine as eng

e = eng.Engine.__new__(eng.Engine)
e._stop = threading.Event()
e._signal_q = eng.queue.Queue(maxsize=1000)
e._shield_lock = threading.Lock()
seen = []


def slow_handler(candle):
    seen.append((threading.current_thread().name, candle.interval))
    time.sleep(0.5)                     # stand-in for a broker round trip


worker = threading.Thread(target=e._signal_loop, name="signal-worker", daemon=True)
worker.start()
c3 = candles.Candle("T1", 3, datetime.now(oc.IST) - timedelta(minutes=3), 1, 1, 1, 1)
c5 = candles.Candle("T1", 5, datetime.now(oc.IST) - timedelta(minutes=5), 1, 1, 1, 1)
started = time.monotonic()
e._enqueue(slow_handler, c3)
e._enqueue(slow_handler, c5)
returned_in = time.monotonic() - started
check(f"tick thread returns immediately ({returned_in*1000:.1f} ms)", returned_in < 0.05)
time.sleep(1.3)
check(f"bars handled in order on the worker ({seen})",
      seen == [("signal-worker", 3), ("signal-worker", 5)])


def boom(candle):
    raise RuntimeError("handler failure")


e._enqueue(boom, c3)
e._enqueue(slow_handler, c5)
time.sleep(0.8)
check("worker survives a failing handler", seen[-1] == ("signal-worker", 5))
e._stop.set()

print("-" * 40)
print("ALL PASS" if not FAILED else f"{len(FAILED)} FAILED: {FAILED}")
sys.exit(1 if FAILED else 0)
