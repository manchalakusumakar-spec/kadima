
"""
Angel One SmartAPI session: TOTP login, token cache, renewal.

Session tokens expire. The engine must survive that without a human, so this
module owns login state and every caller goes through `ensure()`.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

from . import audit
from .rate_limiter import protect_quote_api
from .config import (ANGEL_API_KEY, ANGEL_CLIENT_ID, ANGEL_PASSWORD,
                     ANGEL_TOTP_SECRET, IST)

# SmartAPI feed tokens are good for the trading day; we refresh defensively.
_REFRESH_AFTER = timedelta(hours=6)

# This EC2 instance's IPs -- see the long comment in _login() for why
# these get force-set after construction rather than passed as constructor
# kwargs (the constructor kwargs for this exist but are silently broken
# in Angel One's own SDK).
_INSTANCE_PUBLIC_IP = "18.61.12.184"  # updated 2026-08-27 -- the instance's
                                     # public IP changed (was 18.61.70.143),
                                     # almost certainly from a stop/start
                                     # cycle -- no Elastic IP attached, so
                                     # AWS reassigns a new one each time.
                                     # MUST match whatever's currently
                                     # registered as Primary Static IP in
                                     # the SmartAPI dashboard, or order
                                     # placement risks silently failing
                                     # again, same symptom as the original
                                     # SDK bug this workaround exists for,
                                     # different cause. Consider allocating
                                     # an Elastic IP to this instance so
                                     # this stops recurring.
_INSTANCE_LOCAL_IP = "172.31.41.95"   # unchanged -- private IPs typically
                                     # persist across stop/start within the
                                     # same VPC/subnet, confirmed still
                                     # matching on 2026-08-27


class Session:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.api: SmartConnect | None = None
        self.jwt: str | None = None
        self.refresh_token: str | None = None
        self.feed_token: str | None = None
        self._logged_in_at: datetime | None = None
        self._retry_after = 0.0

    # ------------------------------------------------------------------ login
    def _login(self) -> None:
        if not all([ANGEL_API_KEY, ANGEL_CLIENT_ID, ANGEL_PASSWORD,
                    ANGEL_TOTP_SECRET]):
            raise RuntimeError(
                "Angel One credentials missing. Export ANGEL_API_KEY, "
                "ANGEL_CLIENT_ID, ANGEL_PASSWORD (MPIN), ANGEL_TOTP_SECRET.")

        # imported lazily so the pure-logic modules stay testable without the SDK
        import pyotp
        from SmartApi import SmartConnect

        api = SmartConnect(api_key=ANGEL_API_KEY)

        # WORKAROUND FOR A CONFIRMED BUG IN ANGEL ONE'S OWN smartapi-python SDK
        # (verified directly against their published source, 2026-08-23):
        # SmartConnect's class body computes clientPublicIp/clientLocalIp
        # inside a try/except/finally -- and the `finally` clause
        # UNCONDITIONALLY overwrites both to hardcoded placeholder values
        # ("106.193.147.98" / "127.0.0.1"), regardless of whether the try
        # block succeeded. Confirmed this is exactly the value KADIMA's own
        # requests were sending in the X-ClientPublicIP header, which
        # doesn't match the registered static IP.
        #
        # Worse: __init__ DOES accept a clientPublicIP constructor kwarg,
        # but internally does `self.clientPublicIP = self.clientPublicIp`
        # -- reading the class-level HARDCODED attribute (lowercase 'p'),
        # not the parameter that was passed in (which is silently
        # discarded). There is no working way to override this through
        # the public constructor API in this SDK version.
        #
        # requestHeaders() reads the lowercase-p instance attributes
        # directly, so setting them here, after construction, is the one
        # code path that actually reaches the outgoing request headers.
        # This is a workaround for a third-party bug, not a KADIMA design
        # choice -- remove it if/when Angel One ships a fixed SDK release.
        api.clientPublicIp = _INSTANCE_PUBLIC_IP
        api.clientLocalIp = _INSTANCE_LOCAL_IP

        totp = pyotp.TOTP(ANGEL_TOTP_SECRET).now()
        resp = api.generateSession(ANGEL_CLIENT_ID, ANGEL_PASSWORD, totp)

        if not resp or not resp.get("status"):
            audit.log("login_failed", response=resp)
            raise RuntimeError(f"SmartAPI login failed: {resp}")

        data = resp["data"]
        protect_quote_api(api)
        self.api = api
        self.jwt = data["jwtToken"]
        self.refresh_token = data["refreshToken"]
        self.feed_token = api.getfeedToken()
        self._logged_in_at = datetime.now(IST)
        audit.log("login_ok", client=ANGEL_CLIENT_ID)

    def _stale(self) -> bool:
        if self._logged_in_at is None:
            return True
        return datetime.now(IST) - self._logged_in_at > _REFRESH_AFTER

    def ensure(self, force: bool = False) -> "Session":
        # One login attempt per caller, with a shared cooldown after failure.
        # Avoid sleeping through 31 seconds of retries while holding the lock.
        if not self._lock.acquire(timeout=1.0):
            raise RuntimeError('SmartAPI session refresh in progress')
        try:
            if force or self.api is None or self._stale():
                if time.monotonic() < self._retry_after:
                    raise RuntimeError('SmartAPI login retry cooldown')
                try:
                    self._login()
                except Exception:
                    self._retry_after = time.monotonic() + 5.0
                    audit.log('login_retry_deferred', wait_sec=5)
                    raise RuntimeError('SmartAPI login unavailable') from None
            return self
        finally:
            self._lock.release()

    # --------------------------------------------------------------- teardown
    def logout(self) -> None:
        with self._lock:
            if self.api:
                try:
                    self.api.terminateSession(ANGEL_CLIENT_ID)
                    audit.log("logout_ok")
                except Exception as exc:                # noqa: BLE001
                    audit.log("logout_failed", error=str(exc))
            self.api = None
            self._logged_in_at = None


SESSION = Session()



