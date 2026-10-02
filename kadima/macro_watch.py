"""
macro_watch.py — real macro/news context for the dashboard's "Macro & News
Watch" panel: India VIX, FII/DII institutional flows, real market-relevant
news headlines, and a verified economic-events calendar.

Same isolation principle as stock_universe_scanner.py: its own process,
its own systemd service (kadima-macro-watch), its own memory space -- a
stuck NSE scrape or a slow news API call here can never touch the
trading-critical engine or the universe scanner. Writes one atomic JSON
snapshot; control_api.py's /macro_snapshot route serves it through as-is,
same hand-off pattern as universe_snapshot.json.

Nothing in this file estimates, interpolates, or fabricates a value to
make a panel look live. Every field is either real fetched data with an
honest "as of" timestamp, or explicitly null/empty if nothing has been
fetched successfully yet -- the dashboard is responsible for showing that
honestly, not for hiding it. See each fetch function's docstring for
exactly how it behaves on failure.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime

from . import audit
from .config import MARKETAUX_API_KEY
from .rate_limiter import QUOTE
from .rate_limiter import quote_priority, SCAN as SCAN_PRIORITY
from .scrip_master import ScripMaster
from .session import SESSION
from .utils import in_market_hours, now_ist

# Snapshot hand-off file -- see control_api.py's /macro_snapshot route.
SNAPSHOT_PATH = os.path.expanduser("~/kadima-py/state/macro_snapshot.json")
_SNAPSHOT_LOCK = threading.Lock()

# ---------------------------------------------------------------- cadences
VIX_POLL_SEC = 60             # matches the 4-index engine's own quote cadence
FIIDII_POLL_SEC = 30 * 60     # NSE publishes this once/day; no need to hammer it
NEWS_POLL_SEC = 30 * 60       # keeps us at ~48 Marketaux calls/day, well under
                              # the free tier's 100/day cap
LOOP_TICK_SEC = 5


# ------------------------------------------------------------- economic calendar
# Fixed, publicly-published policy dates -- not something a live API can
# improve on. Verified directly against the official calendars below on
# 2026-09-04; refresh this list periodically as new official calendars are
# published (RBI publishes its MPC calendar for the next fiscal year every
# ~March; the Fed publishes its full-year FOMC calendar each year).
#   FOMC:    https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
#   RBI MPC: https://www.rbi.org.in/scripts/annualpolicy.aspx
#            (FY2026-27 calendar published 2026-03-23)
ECONOMIC_EVENTS = [
    {"date": "2026-09-15", "end_date": "2026-09-16",
     "name": "US Fed FOMC Meeting", "source": "federalreserve.gov"},
    {"date": "2026-10-05", "end_date": "2026-10-07",
     "name": "RBI MPC Meeting", "source": "rbi.org.in"},
    {"date": "2026-10-27", "end_date": "2026-10-28",
     "name": "US Fed FOMC Meeting", "source": "federalreserve.gov"},
    {"date": "2026-12-02", "end_date": "2026-12-04",
     "name": "RBI MPC Meeting", "source": "rbi.org.in"},
    {"date": "2026-12-08", "end_date": "2026-12-09",
     "name": "US Fed FOMC Meeting", "source": "federalreserve.gov"},
    {"date": "2027-02-03", "end_date": "2027-02-05",
     "name": "RBI MPC Meeting", "source": "rbi.org.in"},
]


def _upcoming_events(today: date, limit: int = 5) -> list[dict]:
    out = []
    for ev in ECONOMIC_EVENTS:
        end = datetime.strptime(ev["end_date"], "%Y-%m-%d").date()
        if end < today:
            continue
        start = datetime.strptime(ev["date"], "%Y-%m-%d").date()
        out.append({**ev, "days_away": (start - today).days})
    out.sort(key=lambda e: e["date"])
    return out[:limit]


# ------------------------------------------------------------------- India VIX
def _resolve_india_vix_ref(scrips: ScripMaster) -> tuple[str, str] | None:
    """Find India VIX's spot (exchange, token) from the LIVE scrip master
    rather than hardcoding a token -- Angel One's tokens are not
    guaranteed stable (see the warning atop config.py's INDICES table).
    Returns None if not found, in which case the VIX field just stays
    null forever rather than ever guessing a token."""
    for r in scrips.rows:
        name = (r.get("name") or "").upper().replace(" ", "")
        symbol = (r.get("symbol") or "").upper().replace(" ", "")
        if name == "INDIAVIX" or symbol == "INDIAVIX":
            exch, token = r.get("exch_seg"), r.get("token")
            if exch and token:
                return str(exch), str(token)
    return None


# ------------------------------------------------------------------- FII/DII
# Full realistic-browser fingerprint (not just User-Agent) -- NSE's site
# sits behind Akamai-style bot protection that scores requests on header
# consistency, not just the UA string. This is the header set real-world
# NSE-scraping tools (e.g. nsepython) actually use successfully against
# this exact endpoint as of 2026.
_NSE_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
              "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "accept-language": "en-US,en;q=0.9,en-IN;q=0.8",
    "accept-encoding": "gzip, deflate",
    "cache-control": "max-age=0",
    "sec-ch-ua": '"Chromium";v="129", "Not=A?Brand";v="8"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/129.0.0.0 Safari/537.36"),
}
_NSE_FIIDII_URL = "https://www.nseindia.com/api/fiidiiTradeReact"


def _decode_body(resp) -> str:
    """Handles the gzip/deflate compression we now advertise accepting --
    urllib does not auto-decompress the way a real browser (or `requests`)
    does."""
    raw = resp.read()
    encoding = resp.headers.get("Content-Encoding", "")
    if "gzip" in encoding:
        import gzip
        raw = gzip.decompress(raw)
    elif "deflate" in encoding:
        import zlib
        raw = zlib.decompress(raw)
    return raw.decode("utf-8", errors="replace")


def _fetch_fiidii() -> list[dict] | None:
    """Scrapes NSE's public FII/DII activity endpoint. NSE has no official
    free/stable API for this -- this follows the same approach real-world
    open-source NSE tooling uses: prime a session with NSE's anti-bot
    cookies via a couple of ordinary page GETs (with a realistic full
    browser header set, not just a User-Agent), then call the JSON
    endpoint with that same session. Inherently fragile -- NSE can change
    this without notice, and if NSE is blocking by IP/ASN reputation
    (common for cloud/datacenter ranges like AWS) rather than by request
    headers, no amount of header tuning fixes it. Returns None on ANY
    failure; the caller keeps showing the last successfully-fetched rows
    with their real reported date rather than blanking or faking today's
    figures. On failure, logs a snippet of the actual response body (not
    just the HTTP status) so a real block page vs. a header mismatch can
    be told apart from the audit log."""
    try:
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        for priming_url in ("https://www.nseindia.com",
                            "https://www.nseindia.com/option-chain"):
            req = urllib.request.Request(priming_url, headers=_NSE_HEADERS)
            opener.open(req, timeout=10).read()
            time.sleep(1.5)   # a real browser doesn't fire these back-to-back

        req = urllib.request.Request(_NSE_FIIDII_URL, headers=_NSE_HEADERS)
        with opener.open(req, timeout=10) as r:
            data = json.loads(_decode_body(r))

        if not isinstance(data, list):
            return None

        rows = []
        for row in data:
            try:
                rows.append({
                    "category": row.get("category"),
                    "date": row.get("date"),
                    "buy_cr": float(row["buyValue"]),
                    "sell_cr": float(row["sellValue"]),
                    "net_cr": float(row["netValue"]),
                })
            except (KeyError, TypeError, ValueError):
                continue
        return rows or None
    except urllib.error.HTTPError as exc:
        try:
            body = _decode_body(exc)[:300]
        except Exception:                                # noqa: BLE001
            body = "<could not read body>"
        audit.log("macro_fiidii_fetch_failed", error=str(exc),
                  status=exc.code, body_snippet=body)
        return None
    except Exception as exc:                            # noqa: BLE001
        audit.log("macro_fiidii_fetch_failed", error=str(exc), error_type=type(exc).__name__)
        return None


# --------------------------------------------------------------------- news
_MARKETAUX_URL = "https://api.marketaux.com/v1/news/all"
# Marketaux (like a lot of APIs sitting behind Cloudflare-style bot
# protection) returned a 403 to Python's default "Python-urllib/3.x"
# User-Agent even with a valid, verified API key -- confirmed 2026-09-04:
# the request was rejected before the API key was ever checked. A normal
# browser-style header is enough to get past that; nothing to do with
# authentication.
_NEWS_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/129.0.0.0 Safari/537.36"),
    "Accept": "application/json",
}


def _fetch_news() -> list[dict] | None:
    """Real headlines from Marketaux's free tier, filtered to India /
    English. Returns None if no API key is configured, the request fails,
    or the response is empty -- caller keeps the last successfully-fetched
    headlines on screen (each with its own real published_at) rather than
    ever inventing one."""
    if not MARKETAUX_API_KEY:
        return None
    params = urllib.parse.urlencode({
        "countries": "in",
        "language": "en",
        "limit": 10,
        "api_token": MARKETAUX_API_KEY,
    })
    url = f"{_MARKETAUX_URL}?{params}"
    try:
        req = urllib.request.Request(url, headers=_NEWS_HEADERS)
        with urllib.request.urlopen(req, timeout=10) as r:
            payload = json.loads(r.read().decode())
        articles = payload.get("data", [])
        out = []
        for a in articles:
            title = a.get("title")
            if not title:
                continue
            out.append({
                "title": title,
                "url": a.get("url"),
                "source": a.get("source"),
                "published_at": a.get("published_at"),
            })
        return out or None
    except Exception as exc:                            # noqa: BLE001
        audit.log("macro_news_fetch_failed", error=str(exc))
        return None


# ------------------------------------------------------------------- watcher
class MacroWatcher:
    def __init__(self) -> None:
        self.scrips = ScripMaster.load()
        self._vix_ref = _resolve_india_vix_ref(self.scrips)
        if self._vix_ref is None:
            audit.log("macro_vix_token_not_found")
        else:
            audit.log("macro_vix_token_resolved", exch=self._vix_ref[0],
                      token=self._vix_ref[1])

        self._last_vix: dict | None = None
        self._last_fiidii: dict | None = None
        self._last_news: list[dict] = []
        self._stop = threading.Event()

    # ---------------------------------------------------------- India VIX
    def _fetch_vix(self) -> None:
        if not self._vix_ref:
            return
        exch, token = self._vix_ref
        if not QUOTE.wait_and_allow(timeout_sec=3.0):
            audit.log("macro_vix_rate_limited")
            return
        try:
            SESSION.ensure()
            with quote_priority(SCAN_PRIORITY):     # macro context yields to trading
                resp = SESSION.api.getMarketData(mode="LTP", exchangeTokens={exch: [token]})
            fetched = (resp or {}).get("data", {}).get("fetched", [])
            if fetched and fetched[0].get("ltp") is not None:
                self._last_vix = {
                    "value": float(fetched[0]["ltp"]),
                    "last_updated": now_ist().isoformat(),
                }
        except Exception as exc:                        # noqa: BLE001
            audit.log("macro_vix_poll_failed", error=str(exc))

    # ---------------------------------------------------------- snapshot
    def _save_snapshot(self) -> None:
        payload = {
            "ts": now_ist().isoformat(),
            "india_vix": self._last_vix,
            "fii_dii": self._last_fiidii,
            "news": self._last_news,
            "economic_calendar": _upcoming_events(now_ist().date()),
        }
        try:
            with _SNAPSHOT_LOCK:
                os.makedirs(os.path.dirname(SNAPSHOT_PATH), exist_ok=True)
                tmp = SNAPSHOT_PATH + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(payload, f, indent=2)
                os.replace(tmp, SNAPSHOT_PATH)
        except Exception as exc:                        # noqa: BLE001
            audit.log("macro_snapshot_write_failed", error=str(exc))

    # ---------------------------------------------------------- main loop
    def run_forever(self) -> None:
        last_vix = 0.0
        last_fiidii = 0.0
        last_news = 0.0
        while not self._stop.is_set():
            now = time.monotonic()

            if in_market_hours() and now - last_vix >= VIX_POLL_SEC:
                self._fetch_vix()
                last_vix = now
                self._save_snapshot()

            if now - last_fiidii >= FIIDII_POLL_SEC:
                rows = _fetch_fiidii()
                if rows:
                    self._last_fiidii = {"rows": rows, "fetched_at": now_ist().isoformat()}
                    audit.log("macro_fiidii_updated", rows=len(rows))
                last_fiidii = now
                self._save_snapshot()

            if now - last_news >= NEWS_POLL_SEC:
                articles = _fetch_news()
                if articles:
                    self._last_news = articles
                    audit.log("macro_news_updated", count=len(articles))
                last_news = now
                self._save_snapshot()

            time.sleep(LOOP_TICK_SEC)

    def stop(self) -> None:
        self._stop.set()


def main() -> int:
    audit.log("macro_watch_starting")
    watcher = MacroWatcher()
    watcher._save_snapshot()   # write an initial snapshot immediately, even
                               # if everything is still null -- so the
                               # dashboard gets a real "no data yet" state
                               # right away instead of a 404 until the
                               # first cycle completes.
    try:
        watcher.run_forever()
    except KeyboardInterrupt:
        watcher.stop()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
