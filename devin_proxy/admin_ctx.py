"""Shared admin-router context: auth/session state, the login rate
limiter, and the `admin_key` dependency every /admin/api route uses.

These used to be closures inside admin.make_router; extracting them into a
context object lets the route modules live in their own files without
re-plumbing app/state through every signature."""
import collections
import hashlib
import hmac
import os
import threading
import time

from fastapi import HTTPException, Request, Response

_SESS_COOKIE = "dp_admin"
_SESS_TTL = 7 * 86400
_WS_TICKET_TTL = 60           # seconds to use a minted ws ticket


class AdminCtx:
    def __init__(self, app, started):
        self.app = app
        self.started = started
        self._login_max = int(os.environ.get("DEVIN_PROXY_LOGIN_MAX", "5"))
        self._login_win = int(os.environ.get("DEVIN_PROXY_LOGIN_WIN", "300"))
        self._login_hits = collections.defaultdict(list)
        self._login_lock = threading.Lock()

    # ---- login rate limit ----
    def login_check(self, ip):
        """-> retry-after s if this IP is over the failed-login budget."""
        now = time.time()
        with self._login_lock:
            hits = self._login_hits.get(ip, [])
            while hits and hits[0] <= now - self._login_win:
                hits.pop(0)
            return int(hits[0] + self._login_win - now) + 1 \
                if len(hits) >= self._login_max else 0

    def login_failed(self, ip):
        with self._login_lock:
            self._login_hits[ip].append(time.time())
            if len(self._login_hits) > 10000:
                for k, v in list(self._login_hits.items()):
                    if not v:
                        self._login_hits.pop(k, None)

    def login_ok(self, ip):
        self._login_hits.pop(ip, None)

    # ---- session cookie ----
    def sess_token(self):
        exp = int(time.time()) + _SESS_TTL
        sig = hmac.new(self.app.state.proxy_key.encode(), str(exp).encode(),
                       hashlib.sha256).hexdigest()
        return f"{exp}.{sig}"

    def sess_ok(self, token):
        try:
            exp, sig = token.split(".", 1)
            exp = int(exp)
        except (ValueError, AttributeError):
            return False
        if exp < time.time():
            return False
        want = hmac.new(self.app.state.proxy_key.encode(),
                        str(exp).encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, want)

    # ---- ws ticket: short-lived, single-purpose credential for the live
    # feed handshake. Minted only by an already-authed caller — a cross-site
    # page can't get one (Lax cookie isn't sent cross-site), so a valid
    # ticket proves an authenticated same-origin session existed and the
    # ws Origin check can be skipped (robust under proxies that mangle
    # Host/X-Forwarded-*).
    def ws_ticket(self):
        exp = int(time.time()) + _WS_TICKET_TTL
        sig = hmac.new(self.app.state.proxy_key.encode(),
                       f"ws:{exp}".encode(), hashlib.sha256).hexdigest()
        return f"{exp}.{sig}"

    def ws_ticket_ok(self, token):
        try:
            exp, sig = token.split(".", 1)
            exp = int(exp)
        except (ValueError, AttributeError):
            return False
        if exp < time.time():
            return False
        want = hmac.new(self.app.state.proxy_key.encode(),
                        f"ws:{exp}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, want)

    def set_session_cookie(self, response, request):
        response.set_cookie(
            _SESS_COOKIE, self.sess_token(), max_age=_SESS_TTL,
            path="/admin", httponly=True, samesite="lax",
            secure=request.url.scheme == "https")

    def authed(self, request: Request):
        if self.sess_ok(request.cookies.get(_SESS_COOKIE, "")):
            return True
        key = self.app.state.proxy_key
        if not key:
            return False
        auth = request.headers.get("authorization", "")
        return auth.startswith("Bearer ") \
            and hmac.compare_digest(auth[7:], key)

    def admin_key(self, request: Request, response: Response):
        """FastAPI dependency: gate every /admin/api route, and slide-renew
        the session cookie once it passes half its TTL."""
        if not self.authed(request):
            raise HTTPException(401, "unauthorized")
        tok = request.cookies.get(_SESS_COOKIE, "")
        try:
            exp = int(tok.split(".", 1)[0])
            if 0 < exp - time.time() < _SESS_TTL / 2:
                self.set_session_cookie(response, request)
        except (ValueError, IndexError):
            pass
