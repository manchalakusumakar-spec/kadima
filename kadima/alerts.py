"""
Telegram dispatch. Debounced so a flapping tier does not spam you, with
CONFIRMED and INVALIDATED bypassing the cooldown.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

from . import audit
from .config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

_COOLDOWN_SEC = 180
_last_sent: dict[str, float] = {}


def send(text: str, key: str | None = None, bypass: bool = False) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        audit.log("alert_not_configured", text=text[:120])
        return False

    if key and not bypass:
        last = _last_sent.get(key, 0)
        if time.time() - last < _COOLDOWN_SEC:
            return False
        _last_sent[key] = time.time()

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": "true"}).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=10) as r:
            ok = json.loads(r.read().decode()).get("ok", False)
        audit.log("alert_sent", ok=ok, key=key, text=text[:160])
        return ok
    except Exception as exc:                            # noqa: BLE001
        audit.log("alert_failed", error=str(exc), text=text[:160])
        return False


def send_signal(signal_id: int, p: dict) -> None:
    s = p["strike"]
    side = "CALL" if p["direction"] == "CE" else "PUT"
    text = (
        f"<b>🛡 SHIELD {p['tier']} — {p['index']} {side}</b>\n"
        f"Score <b>{p['score']}%</b> · agreement {int(p['agreement']*100)}%\n"
        f"Spot {p['spot']}\n\n"
        f"<b>{s['symbol']}</b>\n"
        f"LTP {s['ltp']} · Δ {s['delta']} · IV {s['iv']}\n"
        f"OI {s['oi']:,} · vol {s['volume']:,} · spread {s['spread_pct']}%\n"
        f"PCR {round(p['pcr'], 2) if p['pcr'] else '—'} · "
        f"Max pain {p['max_pain']}\n\n"
        f"<i>Ranked on liquidity and structure, not on expected profit. "
        f"Size per your own rules; the engine does not place orders.</i>\n"
        f"<code>signal #{signal_id}</code>"
    )
    enqueue(text, key=f"{p['index']}:{p['tier']}",
         bypass=p["tier"] == "CONFIRMED")


def send_invalidation(index: str, reason: str) -> None:
    enqueue(f"⚠️ <b>SHIELD INVALIDATED — {index}</b>\n{reason}",
         key=f"{index}:INVALID", bypass=True)


def send_prealert(name: str, tier: str, direction: str, score: float,
                   spot: float, strike: dict, pcr, max_pain) -> None:
    """Early warning: a single index or stock has entered ACTIVE tier --
    building toward a possible gamma blast, not yet confirmed. Shares
    formatting with send_signal but uses its own debounce key so it
    never collides with a real CONFIRMED signal's cooldown."""
    side = "CALL" if direction == "CE" else "PUT"
    text = (
        f"<b>\u26A1 PRE-BLAST {tier} -- {name} {side}</b>\n"
        f"Score <b>{score}%</b>\n"
        f"Spot {spot}\n"
        f"<b>{strike['symbol']}</b>\n"
        f"LTP {strike['ltp']} \u00b7 \u0394 {strike['delta']} \u00b7 IV {strike['iv']}\n"
        f"OI {strike['oi']:,} \u00b7 vol {strike['volume']:,} \u00b7 spread {strike['spread_pct']}%\n"
        f"PCR {round(pcr, 2) if pcr else '--'} \u00b7 Max pain {max_pain}\n\n"
        f"<i>Early signal -- building toward a possible gamma blast. "
        f"Not a confirmed entry; watch for CONFIRMED tier.</i>"
    )
    enqueue(text, key=f"{name}:{tier}:prealert", bypass=False)


def send_prealert(name: str, tier: str, direction: str, score: float,
                   spot: float, pick: dict, pcr, max_pain) -> None:
    """Early-warning alert with a specific strike already attached, fired
    per instrument (index or F&O stock) the moment gamma.score() marks it
    ACTIVE (pre-blast, building) or CONFIRMED (blast). Distinct from
    send_signal(), which only fires from engine.py's strict cross-index-
    agreed Bull Shield path -- this needs no cross-index agreement, so it
    also covers gamma blasts on individual stocks and on indices that
    don't participate in that vote (see config.py's GAMMA_EXTRA_INDICES)."""
    side = "CALL" if direction == "CE" else "PUT"
    label = "\u26A1 PRE-BLAST" if tier == "ACTIVE" else "\U0001F3AF GAMMA BLAST"
    text = (f"<b>{label} -- {name} {side}</b>\n"
        f"Score <b>{score}%</b>\nSpot {spot}\n<b>{pick['symbol']}</b>\n"
        f"LTP {pick['ltp']} \u00b7 \u0394 {pick['delta']} \u00b7 IV {pick['iv']}\n"
        f"OI {pick['oi']:,} \u00b7 vol {pick['volume']:,} \u00b7 spread {pick['spread_pct']}%\n"
        f"PCR {round(pcr, 2) if pcr else '--'} \u00b7 Max pain {max_pain}\n\n"
        f"<i>Early warning -- ranked on liquidity/structure via strike_selector, "
        f"not a trade recommendation.</i>")
    enqueue(text, key=f"{name}:{tier}:prealert", bypass=(tier == "CONFIRMED"))



# Delivery happens off the trading thread. Queue acceptance is not delivery.
import queue
import threading
_queue = queue.Queue(maxsize=128)
_worker_lock = threading.Lock()
_worker_started = False


def _deliver():
    while True:
        text, key, bypass = _queue.get()
        try:
            send(text, key=key, bypass=bypass)
        except Exception:
            audit.log('alert_worker_error')
        finally:
            _queue.task_done()


def enqueue(text, key=None, bypass=False):
    global _worker_started
    with _worker_lock:
        if not _worker_started:
            threading.Thread(target=_deliver, daemon=True, name='alert-delivery').start()
            _worker_started = True
    try:
        _queue.put_nowait((text, key, bypass))
        return True
    except queue.Full:
        audit.log('alert_queue_full', key=key)
        return False
