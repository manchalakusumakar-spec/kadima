"""Independent spot-only Move Capture service.

Subscribes resolved F&O underlyings (plus selected indices) over SmartWebSocketV2,
feeds the broker-free detector, publishes fresh hot-stock candidates through a
durable handoff, and writes bounded snapshots/events. It never imports the
shared executor/order transport and never places an order itself.
"""
from __future__ import annotations

import json
import math
import os
import ssl
from pathlib import Path
import tempfile
import threading
import time
from datetime import datetime

from . import audit, celestial
from . import move_capture_handoff as handoff
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


def live_requested():
    return os.environ.get("KADIMA_MOVE_CAPTURE_LIVE", "0") == "1"


def universe():
    """Spot identities only; option venue is irrelevant at discovery time."""
    result = {}
    for row in stock_universe():
        if row.get("opt_exchange") != "NFO":
            continue
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
        self._lock = threading.RLock()
        self.thread = None
        self._last_exchange_stamp = {}
        self._last_tick_mono = 0.0
        self._connected = False
        self._reconnects = 0
        self._last_publish_second = {}
        self._published_names = set()
        self._capacity_blocked = set()

    def token_list(self):
        grouped = {}
        for exchange, token in self.identities:
            grouped.setdefault(str(EXCHANGE_TYPE[exchange]), []).append(token)
        return [{"exchangeType": int(k), "tokens": sorted(v)}
                for k, v in grouped.items()]

    def on_data(self, _wsapp, message):
        try:
            with self._lock:
                self._on_data(message)
        except Exception as exc:
            audit.log('MOVE_CAPTURE_TICK_REJECTED', error_type=type(exc).__name__)

    def _on_data(self, message):
        if self._stop.is_set() or not in_market_hours():
            return
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
        raw_stamp = message.get('exchange_timestamp')
        if type(raw_stamp) not in (int, float) or not math.isfinite(raw_stamp):
            return
        exchange_stamp = datetime.fromtimestamp(raw_stamp/1000, tz=IST)
        if not 0 <= (now-exchange_stamp).total_seconds() <= handoff.FRESH_SECONDS:
            return
        if raw_stamp <= self._last_exchange_stamp.get(identity.name, 0):
            return
        self._last_exchange_stamp[identity.name] = raw_stamp
        candidate, promoted = self.detector.ingest(
            identity, now, price, volume=volume, previous_close=previous_close)
        self._last_tick_mono = time.monotonic()
        if candidate and candidate.get('capacity_blocked'):
            if identity.name not in self._capacity_blocked:
                audit.log('MOVE_CAPTURE_HOT_LIST_FULL', underlying=identity.name,
                          score=candidate['score'], limit=self.detector.max_hot)
            self._capacity_blocked.add(identity.name)
        else:
            self._capacity_blocked.discard(identity.name)

        hot = self.detector.hot.get(identity.name)
        if identity.kind == "STOCK" and hot is not None:
            second = int(now.timestamp())
            if self._last_publish_second.get(identity.name) != second:
                try:
                    handoff.publish(hot)
                    self._last_publish_second[identity.name] = second
                    self._published_names.add(identity.name)
                except (ValueError, TypeError):
                    audit.log("MOVE_CAPTURE_HANDOFF_REJECTED",
                              underlying=identity.name)
        elif identity.name in self._published_names:
            handoff.invalidate(identity.name)
            self._published_names.discard(identity.name)

        if promoted and hot:
            event = dict(event="MOVE_CAPTURE_TRIGGER", live_requested=live_requested(), **hot)
            append_event(event)
            audit.log("MOVE_CAPTURE_TRIGGER",
                      underlying=identity.name, score=hot["score"],
                      direction=hot["direction"], price=hot["price"],
                      live_requested=live_requested())

    def on_open(self, _wsapp):
        self._connected = True
        self._last_tick_mono = time.monotonic()
        self._reconnects = 0
        self._ws.subscribe("kadima-move-capture", MODE_SNAPQUOTE, self.token_list())
        audit.log("move_capture_ws_open", symbols=len(self.identities))

    def on_error(self, _wsapp, error):
        self._connected = False
        audit.log("move_capture_ws_error", error_type=type(error).__name__)

    def on_close(self, _wsapp, *_args):
        self._connected = False
        audit.log("move_capture_ws_close")

    def connect(self):
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2
        import websocket
        class VerifiedSocket(SmartWebSocketV2):
            # SDK 1.5.5 connect() disables certificate verification. Override
            # connect itself so SDK retry paths also retain verified TLS.
            def connect(inner):
                headers = {'Authorization': inner.auth_token,
                    'x-api-key': inner.api_key, 'x-client-code': inner.client_code,
                    'x-feed-token': inner.feed_token}
                inner.wsapp = websocket.WebSocketApp(inner.ROOT_URI, header=headers,
                    on_open=inner._on_open, on_error=inner._on_error,
                    on_close=inner._on_close, on_data=inner._on_data,
                    on_ping=inner._on_ping, on_pong=inner._on_pong)
                inner.wsapp.run_forever(sslopt={'cert_reqs': ssl.CERT_REQUIRED,
                    'check_hostname': True}, ping_interval=inner.HEART_BEAT_INTERVAL)
        SESSION.ensure()
        ws = VerifiedSocket(
            SESSION.jwt, ANGEL_API_KEY, ANGEL_CLIENT_ID, SESSION.feed_token,
            max_retry_attempt=1)
        ws.on_open = self.on_open
        # A replaced socket must never publish late buffered observations.
        ws.on_data = lambda app, msg: self.on_data(app, msg) if self._ws is ws else None
        ws.on_error = self.on_error
        ws.on_close = self.on_close
        with self._lock:
            self._ws = ws
            self._last_tick_mono = time.monotonic()
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
        with self._lock:
            return self._status()

    def _status(self):
        now = now_ist()
        value = self.detector.snapshot(now)
        active_stock_hot = {
            row["underlying"] for row in value["hot"]
            if row.get("asset_scope") == "STOCK"
        }
        for name in list(self._published_names - active_stock_hot):
            handoff.invalidate(name)
            self._published_names.discard(name)
        silence = None if not self._last_tick_mono else max(
            0.0, time.monotonic() - self._last_tick_mono)
        value.update(
            mode="LIVE_READY" if live_requested() else "OBSERVE_ONLY",
            execution_enabled=live_requested(),
            service="kadima-move-capture",
            subscribed_symbols=len(self.identities),
            websocket_connected=self._connected,
            seconds_since_tick=None if silence is None else round(silence, 2),
            reconnects=self._reconnects,
            market_hours=in_market_hours(),
            handoff_active=len(active_stock_hot),
            execution_path="central_guarded_executor",
        )
        celestial.health('move_capture_feed', dict(
            ok=self._connected and silence is not None and silence <= handoff.FRESH_SECONDS,
            subscribed_symbols=len(self.identities), symbols_seen=value['symbols_seen'],
            websocket_connected=self._connected, seconds_since_tick=value['seconds_since_tick'],
            market_hours=value['market_hours']))
        return value

    def run(self):
        audit.log("move_capture_starting", symbols=len(self.identities),
                  mode="LIVE_READY" if live_requested() else "OBSERVE_ONLY",
                  live_requested=live_requested())
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
        with self._lock:
            for name in list(self._published_names):
                handoff.invalidate(name)
            self._published_names.clear()
        audit.log("move_capture_stopped")

    def stop(self):
        self._stop.set()
        if self.thread is not None:
            self.thread.join(timeout=5)

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self.run, daemon=True,
                                           name='move-capture-supervisor')
            self.thread.start()


def main():
    service = MoveCaptureService()
    try:
        service.run()
    except KeyboardInterrupt:
        service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
