"""Independent spot-only Move Capture service.

Subscribes resolved F&O underlyings (plus selected indices) over SmartWebSocketV2,
feeds the broker-free detector, and writes bounded shadow snapshots/events.
It never imports the shared executor/order transport and never places an order.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time

from . import audit
from .config import ANGEL_API_KEY, ANGEL_CLIENT_ID, GAMMA_INDICES, IST
from .instrument_master import stock_universe
from .move_capture import MoveCaptureDetector, SpotIdentity
from .session import SESSION
from .utils import in_market_hours, now_ist

MODE_SNAPQUOTE = 3
EXCHANGE_TYPE = {"NSE": 1, "BSE": 3}
INDEX_NAMES = ("NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY", "BANKEX")
SNAPSHOT = Path(os.environ.get("KADIMA_MOVE_CAPTURE_SNAPSHOT",
                               "/var/lib/kadima/move_capture.json"))
EVENTS = Path(os.environ.get("KADIMA_MOVE_CAPTURE_EVENTS",
                             "/var/lib/kadima/move_capture_events.jsonl"))
HEARTBEAT_SECONDS = 12
MAX_SYMBOLS = 512


def universe():
    """Spot identities only; option venue is irrelevant at discovery time."""
    result = {}
    for row in stock_universe():
        try:
            name = str(row["name"])
            exchange = str(row["spot_exchange"])
            token = str(row["spot_token"])
        except (KeyError, TypeError):
            continue
        if exchange not in EXCHANGE_TYPE or not token:
            continue
        result[(exchange, token)] = SpotIdentity(name, exchange, token, "STOCK")
    for name in INDEX_NAMES:
        idx = GAMMA_INDICES.get(name)
        if idx is not None and idx.exchange in EXCHANGE_TYPE:
            result[(idx.exchange, str(idx.token))] = SpotIdentity(
                name, idx.exchange, str(idx.token), "INDEX")
    if not result or len(result) > MAX_SYMBOLS:
        raise ValueError("move_capture_universe_unavailable")
    return result


def _price(raw):
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw) / 100.0
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _volume(raw):
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".move-capture-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, allow_nan=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def append_event(value):
    EVENTS.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(value, allow_nan=False, separators=(",", ":"))
    with EVENTS.open("a") as handle:
        handle.write(line + "\n")


class MoveCaptureService:
    def __init__(self, detector=None):
        self.identities = universe()
        self.detector = detector or MoveCaptureDetector()
        self._ws = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._last_tick_mono = 0.0
        self._connected = False
        self._reconnects = 0

    def token_list(self):
        grouped = {}
        for exchange, token in self.identities:
            grouped.setdefault(str(EXCHANGE_TYPE[exchange]), []).append(token)
        return [{"exchangeType": int(k), "tokens": sorted(v)}
                for k, v in grouped.items()]

    def on_data(self, _wsapp, message):
        token = str(message.get("token", "")).strip()
        exchange_type = message.get("exchange_type")
        # The SDK sometimes omits exchange_type on callbacks; token identity is
        # unique inside this spot-only subscription, so resolve by token then.
        matches = [item for (exchange, tok), item in self.identities.items()
                   if tok == token and (exchange_type is None
                       or EXCHANGE_TYPE[exchange] == exchange_type)]
        if len(matches) != 1:
            return
        price = _price(message.get("last_traded_price"))
        if price is None:
            return
        identity = matches[0]
        volume = _volume(message.get("volume_trade_for_the_day"))
        previous_close = _price(message.get("closed_price"))
        now = now_ist()
        _, promoted = self.detector.ingest(
            identity, now, price, volume=volume, previous_close=previous_close)
        self._last_tick_mono = time.monotonic()
        if promoted:
            row = next((r for r in self.detector.snapshot(now)["hot"]
                        if r["underlying"] == identity.name), None)
            if row:
                event = dict(event="MOVE_CAPTURE_SHADOW_TRIGGER", **row)
                append_event(event)
                audit.log("MOVE_CAPTURE_SHADOW_TRIGGER",
                          underlying=identity.name, score=row["score"],
                          direction=row["direction"], price=row["price"])

    def on_open(self, _wsapp):
        self._connected = True
        self._last_tick_mono = time.monotonic()
        self._reconnects = 0
        self._ws.subscribe("kadima-move-capture", MODE_SNAPQUOTE, self.token_list())
        audit.log("move_capture_ws_open", symbols=len(self.identities))

    def on_error(self, _wsapp, error):
        self._connected = False
        audit.log("move_capture_ws_error", error_type=type(error).__name__)

    def on_close(self, _wsapp):
        self._connected = False
        audit.log("move_capture_ws_close")

    def connect(self):
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2
        SESSION.ensure()
        ws = SmartWebSocketV2(
            SESSION.jwt, ANGEL_API_KEY, ANGEL_CLIENT_ID, SESSION.feed_token,
            max_retry_attempt=1)
        ws.on_open = self.on_open
        ws.on_data = self.on_data
        ws.on_error = self.on_error
        ws.on_close = self.on_close
        with self._lock:
            self._ws = ws
        threading.Thread(target=ws.connect, daemon=True,
                         name="move-capture-ws").start()

    def teardown(self):
        with self._lock:
            ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close_connection()
            except Exception:
                pass
        self._connected = False

    def status(self):
        now = now_ist()
        value = self.detector.snapshot(now)
        silence = None if not self._last_tick_mono else max(
            0.0, time.monotonic() - self._last_tick_mono)
        value.update(
            service="kadima-move-capture",
            subscribed_symbols=len(self.identities),
            websocket_connected=self._connected,
            seconds_since_tick=None if silence is None else round(silence, 2),
            reconnects=self._reconnects,
            market_hours=in_market_hours(),
        )
        return value

    def run(self):
        audit.log("move_capture_starting", symbols=len(self.identities),
                  mode="SHADOW_OBSERVE_ONLY")
        next_snapshot = 0.0
        while not self._stop.is_set():
            market = in_market_hours()
            now_mono = time.monotonic()
            if market:
                silence = now_mono - self._last_tick_mono if self._last_tick_mono else None
                if self._ws is None or (silence is not None and silence > HEARTBEAT_SECONDS):
                    self._reconnects += 1
                    self.teardown()
                    if self._reconnects % 3 == 0:
                        try:
                            SESSION.ensure(force=True)
                        except Exception:
                            pass
                    try:
                        self.connect()
                    except Exception as exc:
                        audit.log("move_capture_reconnect_failed",
                                  error_type=type(exc).__name__)
                    if self._stop.wait(min(30, 2 ** min(self._reconnects, 5))):
                        break
            elif self._ws is not None:
                self.teardown()

            if now_mono >= next_snapshot:
                try:
                    atomic_json(SNAPSHOT, self.status())
                except Exception as exc:
                    audit.log("move_capture_snapshot_failed",
                              error_type=type(exc).__name__)
                next_snapshot = now_mono + 2.0
            self._stop.wait(0.5)
        self.teardown()
        audit.log("move_capture_stopped")

    def stop(self):
        self._stop.set()


def main():
    service = MoveCaptureService()
    try:
        service.run()
    except KeyboardInterrupt:
        service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
