"""
Tick collector: Angel One SmartWebSocketV2.

Design notes that matter more than the code:

1. A collector without backoff will silently gap your candles. Every reconnect
   is logged and every gap longer than one candle interval is flagged, because
   an indicator computed over a gapped series is wrong in a way that looks
   right on a chart.

2. The watchdog does not trust the socket's own callbacks. If no tick has
   arrived in WS_HEARTBEAT_SEC during market hours, we tear down and rebuild
   rather than waiting for the library to notice. A silently dead socket is
   the most common live failure and the hardest to see.

3. Ticks are queued, not written inline. A slow disk must never stall the
   socket read loop.
"""
from __future__ import annotations

import queue
import threading
import time
from datetime import datetime

from . import audit, storage
from .config import (ANGEL_API_KEY, ANGEL_CLIENT_ID, IST,
                     WS_HEARTBEAT_SEC, WS_MAX_RETRIES,
                     WS_RECONNECT_BASE_SEC, WS_RECONNECT_MAX_SEC)
from .session import SESSION
from .utils import in_market_hours

MODE_QUOTE = 2      # ltp + volume + bid/ask depth top
MODE_SNAPQUOTE = 3


class TickFeed:
    """Subscribes to a token list and pushes normalised ticks to a callback."""

    def __init__(self, on_tick) -> None:
        self.on_tick = on_tick
        self._subs: dict[str, list[str]] = {}   # exchange_type -> tokens
        self._ws = None
        self._last_tick_at: float = 0.0
        self._stop = threading.Event()
        self._writer_q: "queue.Queue[tuple]" = queue.Queue(maxsize=100_000)
        self._retries = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------ subscription
    def subscribe(self, exchange_type: int, tokens: list[str]) -> None:
        self._subs.setdefault(str(exchange_type), []).extend(tokens)

    def _token_list(self) -> list[dict]:
        return [{"exchangeType": int(k), "tokens": v}
                for k, v in self._subs.items() if v]

    # ------------------------------------------------------------- ws handlers
    def _handle_tick(self, wsapp, message) -> None:
        self._last_tick_at = time.monotonic()
        try:
            token = str(message.get("token", "")).strip()
            # SmartAPI sends prices in paise for most segments
            ltp = message.get("last_traded_price")
            ltp = float(ltp) / 100.0 if ltp is not None else None
            vol = message.get("volume_trade_for_the_day")
            bid = message.get("best_5_buy_data", [{}])
            ask = message.get("best_5_sell_data", [{}])
            bid_px = (float(bid[0].get("price", 0)) / 100.0) if bid else None
            ask_px = (float(ask[0].get("price", 0)) / 100.0) if ask else None
            ts = datetime.now(IST)

            tick = {"ts": ts, "token": token, "ltp": ltp, "volume": vol,
                    "bid": bid_px, "ask": ask_px}

            try:
                self._writer_q.put_nowait(
                    (ts.isoformat(), token, None, ltp, vol, bid_px, ask_px))
            except queue.Full:
                audit.log("tick_queue_full")

            self.on_tick(tick)
        except Exception as exc:                        # noqa: BLE001
            audit.log("tick_parse_error", error=str(exc), raw=str(message)[:400])

    def _handle_open(self, wsapp) -> None:
        self._retries = 0
        self._last_tick_at = time.monotonic()
        audit.log("ws_open", subscriptions=self._token_list())
        try:
            self._ws.subscribe("kadima", MODE_SNAPQUOTE, self._token_list())
        except Exception as exc:                        # noqa: BLE001
            audit.log("ws_subscribe_failed", error=str(exc))

    def _handle_error(self, wsapp, error) -> None:
        audit.log("ws_error", error=str(error))

    def _handle_close(self, wsapp) -> None:
        audit.log("ws_close")

    # ------------------------------------------------------------------- loops
    def _writer_loop(self) -> None:
        batch: list[tuple] = []
        while not self._stop.is_set():
            try:
                batch.append(self._writer_q.get(timeout=1.0))
            except queue.Empty:
                pass
            if batch and (len(batch) >= 500 or self._writer_q.empty()):
                try:
                    storage.insert_ticks(batch)
                except Exception as exc:                # noqa: BLE001
                    audit.log("tick_write_failed", error=str(exc), n=len(batch))
                batch = []

    def _connect(self) -> None:
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2

        SESSION.ensure()
        self._ws = SmartWebSocketV2(
            SESSION.jwt, ANGEL_API_KEY, ANGEL_CLIENT_ID, SESSION.feed_token,
            max_retry_attempt=1)
        self._ws.on_open = self._handle_open
        self._ws.on_data = self._handle_tick
        self._ws.on_error = self._handle_error
        self._ws.on_close = self._handle_close
        t = threading.Thread(target=self._ws.connect, daemon=True,
                             name="ws-connect")
        t.start()

    def _teardown(self) -> None:
        with self._lock:
            if self._ws:
                try:
                    self._ws.close_connection()
                except Exception:                       # noqa: BLE001
                    pass
                self._ws = None

    def _watchdog_loop(self) -> None:
        """Rebuild the socket whenever ticks stop during market hours."""
        while not self._stop.is_set():
            time.sleep(1.0)
            if not in_market_hours():
                continue
            silence = time.monotonic() - self._last_tick_at
            if self._ws is None or silence > WS_HEARTBEAT_SEC:
                self._retries += 1
                if WS_MAX_RETRIES and self._retries > WS_MAX_RETRIES:
                    audit.log("ws_giving_up", retries=self._retries)
                    self._stop.set()
                    return
                backoff = min(WS_RECONNECT_BASE_SEC * (2 ** (self._retries - 1)),
                              WS_RECONNECT_MAX_SEC)
                audit.log("ws_reconnecting", silence_sec=round(silence, 1),
                          attempt=self._retries, backoff_sec=backoff,
                          gap_warning=silence > 60)
                self._teardown()
                time.sleep(backoff)
                # a dead socket is often a dead session
                if self._retries % 3 == 0:
                    SESSION.ensure(force=True)
                try:
                    self._connect()
                except Exception as exc:                # noqa: BLE001
                    audit.log("ws_reconnect_failed", error=str(exc))

    # ------------------------------------------------------------------ public
    def start(self) -> None:
        threading.Thread(target=self._writer_loop, daemon=True,
                         name="tick-writer").start()
        self._connect()
        threading.Thread(target=self._watchdog_loop, daemon=True,
                         name="ws-watchdog").start()
        audit.log("feed_started")

    def stop(self) -> None:
        self._stop.set()
        self._teardown()
        audit.log("feed_stopped")
