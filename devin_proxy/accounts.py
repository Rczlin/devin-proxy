"""Multi-account pool: scheduling, session pinning, failover, OAuth login.

Accounts live in SQLite (store.accounts). Runtime state (in-flight count) is
kept on the Account objects in memory. Scheduling policy:

- sticky: a session_key is pinned to the account that last served it
- ready pool: enabled accounts not in cooldown, ordered by
  (in_flight, consecutive_fails, last_used) — least-busy first
- failover: hard failures (auth/quota/5xx/network) cool an account down with
  exponential backoff; the request is retried on the next account
"""
import base64
import hashlib
import secrets
import threading
import time
import urllib.parse
import uuid

from . import store, upstream

DEFAULT_API_SERVER = "https://server.codeium.com"
DEFAULT_WEBAPP = "https://app.devin.ai"
DEFAULT_API_URL = "https://api.devin.ai"

COOLDOWN_BASE = 30        # seconds; doubles per consecutive hard failure
COOLDOWN_MAX = 900
SESSION_TTL = 86400       # pinned sessions pruned after this much idle time
RESPONSE_TTL = 86400
MAX_ATTEMPTS = 4          # accounts tried per request (bounded by pool size)
PIN_FLUSH_S = 30          # dirty pin `updated` stamps batch-flushed this often


class Account:
    __slots__ = ("id", "name", "email", "token", "api_server_url", "webapp",
                 "api_url", "source", "plan", "created", "disabled",
                 "fail_count", "consecutive_fails", "cooldown_until",
                 "last_error", "last_used", "last_ok", "req_count",
                 "max_concurrent", "models", "in_flight")

    @classmethod
    def from_row(cls, r):
        a = cls()
        a.id = r["id"]
        a.name = r["name"]
        a.email = r["email"]
        a.token = normalize_token(r["token"])
        a.api_server_url = r["api_server_url"] or DEFAULT_API_SERVER
        a.webapp = r["devin_webapp_host"] or DEFAULT_WEBAPP
        a.api_url = r["devin_api_url"] or DEFAULT_API_URL
        a.source = r["source"]
        a.plan = r["plan"]
        a.created = r["created"]
        a.disabled = bool(r["disabled"])
        a.fail_count = r["fail_count"] or 0
        a.consecutive_fails = r["consecutive_fails"] or 0
        a.cooldown_until = r["cooldown_until"] or 0
        a.last_error = r["last_error"]
        a.last_used = r["last_used"]
        a.last_ok = r["last_ok"]
        a.req_count = r["req_count"] or 0
        a.max_concurrent = r["max_concurrent"] or 0
        a.models = {m for m in (r["models"] or "").split(",") if m}
        a.in_flight = 0
        return a

    def display(self):
        return self.name or self.email or f"acct-{self.id}"

    def serves(self, models):
        """True if this account may serve any of `models` (requested name or
        resolved uid). Empty allowlist = all models."""
        return not self.models or not models or bool(self.models & models)

    def at_cap(self):
        return bool(self.max_concurrent) and self.in_flight >= self.max_concurrent

    def public(self):
        tail = self.token[-6:] if len(self.token) > 6 else "***"
        now = time.time()
        state = "disabled" if self.disabled else (
            "cooldown" if self.cooldown_until > now else "ready")
        return {"id": self.id, "name": self.name, "email": self.email,
                "label": self.display(), "token_tail": tail,
                "api_server_url": self.api_server_url, "source": self.source,
                "plan": self.plan, "state": state, "disabled": self.disabled,
                "fail_count": self.fail_count,
                "consecutive_fails": self.consecutive_fails,
                "cooldown_s": max(0, round(self.cooldown_until - now)),
                "last_error": self.last_error, "last_used": self.last_used,
                "last_ok": self.last_ok, "req_count": self.req_count,
                "max_concurrent": self.max_concurrent,
                "models": sorted(self.models),
                "in_flight": self.in_flight, "created": self.created}

    def persist(self):
        store.update_account(
            self.id, name=self.name, email=self.email, plan=self.plan,
            disabled=int(self.disabled), fail_count=self.fail_count,
            consecutive_fails=self.consecutive_fails,
            cooldown_until=self.cooldown_until, last_error=self.last_error,
            last_used=self.last_used, last_ok=self.last_ok,
            req_count=self.req_count, max_concurrent=self.max_concurrent,
            models=",".join(sorted(self.models)) or None)


def normalize_token(token):
    """The OAuth exchange returns a bare session JWT; upstream calls need it
    as `devin-session-token$<jwt>` (the format the Devin CLI stores)."""
    token = (token or "").strip()
    if "$" not in token and token.count(".") == 2 and token.startswith("eyJ"):
        return "devin-session-token$" + token
    return token


def is_hard_failure(err):
    """True if the upstream error likely means this account can't serve —
    auth/quota/rate-limit/5xx/network. 4xx request-shape errors are soft."""
    if not isinstance(err, dict):
        return True                       # exceptions: network/protocol
    if err.get("soft"):
        return False
    code = err.get("http_error")
    if code is None:
        return True
    if code in (401, 402, 403, 408, 409, 425, 429) or code >= 500:
        return True
    msg = (err.get("message") or "").lower()
    return any(s in msg for s in (
        "quota", "limit reached", "usage paused", "unauthorized",
        "forbidden", "not authenticated", "rate limit", "credit"))


class Pool:
    def __init__(self):
        self._lock = threading.Lock()
        self._accs = {}
        self._pins = {}          # session_key -> [account_id, updated]
        self._pins_dirty = set() # keys whose `updated` needs flushing to db
        self.reload()
        store.prune_pins(SESSION_TTL)
        store.prune_responses(RESPONSE_TTL)
        for r in store.list_pins():
            self._pins[r["session_key"]] = [r["account_id"], r["updated"] or 0]
        threading.Thread(target=self._pin_flush_loop, daemon=True).start()

    def reload(self):
        with self._lock:
            prev = self._accs
            self._accs = {}
            for r in store.list_accounts():
                a = Account.from_row(r)
                if a.id in prev:
                    a.in_flight = prev[a.id].in_flight
                self._accs[a.id] = a

    # ---------- lookup ----------

    def accounts(self):
        with self._lock:
            return sorted(self._accs.values(), key=lambda a: a.id)

    def get(self, aid):
        with self._lock:
            return self._accs.get(aid)

    def by_name(self, name):
        for a in self.accounts():
            if a.display() == name or a.name == name or a.email == name:
                return a
        return None

    # ---------- mutation ----------

    def add(self, token, name=None, email=None, api_server_url=None,
            webapp=None, api_url=None, source=None, plan=None, manual=False):
        token = normalize_token(token)
        if not token:
            return None, "empty token"
        th = "tomb:" + hashlib.sha256(token.encode()).hexdigest()
        if manual:
            store.meta_set(th, "0")
        elif store.meta_get(th) == "1":
            return None, "previously removed"
        for r in store.list_accounts():
            if normalize_token(r["token"]) == token:
                return None, "duplicate token"
        row = store.add_account(
            token, name=name, email=email, api_server_url=api_server_url,
            devin_webapp_host=webapp, devin_api_url=api_url,
            source=source, plan=plan)
        if not row:
            return None, "duplicate token"
        self.reload()
        return self.get(row["id"]), None

    def remove(self, aid):
        a = self.get(aid)
        if a:
            store.meta_set(
                "tomb:" + hashlib.sha256(a.token.encode()).hexdigest(), "1")
        store.delete_account(aid)
        with self._lock:
            self._accs.pop(aid, None)
            for k in [k for k, v in self._pins.items() if v[0] == aid]:
                self._pins.pop(k, None)
                self._pins_dirty.discard(k)

    def update(self, aid, **fields):
        store.update_account(aid, **fields)
        self.reload()

    # ---------- scheduling ----------

    def pick(self, session_key=None, exclude=frozenset(), force_id=None,
             models=None, remote=None):
        with self._lock:
            now = time.time()
            if force_id is not None:
                a = self._accs.get(force_id)
                if a and not a.disabled:
                    a.in_flight += 1
                    return a
                return None
            avail = [a for a in self._accs.values()
                     if not a.disabled and a.id not in exclude
                     and not a.at_cap() and a.serves(models)
                     and (remote is None or a.id in remote[0]
                          or a.id not in remote[1])]
            if not avail:
                return None
            if session_key:
                ent = self._pins.get(session_key)
                a = self._accs.get(ent[0]) if ent else None
                if a in avail and a.cooldown_until <= now:
                    a.in_flight += 1
                    ent[1] = now
                    self._pins_dirty.add(session_key)
                    return a
            ready = [a for a in avail if a.cooldown_until <= now]
            cands = ready or avail          # all cooling -> least-bad anyway
            cands.sort(key=lambda a: (a.in_flight, a.consecutive_fails,
                                      a.last_used or 0))
            a = cands[0]
            a.in_flight += 1
            return a

    def release(self, acct):
        if acct is None:
            return
        with self._lock:
            acct.in_flight = max(0, acct.in_flight - 1)

    def mark_ok(self, acct):
        now = time.time()
        acct.req_count += 1
        acct.last_used = acct.last_ok = now
        acct.consecutive_fails = 0
        acct.cooldown_until = 0
        acct.last_error = None
        acct.persist()

    def mark_fail(self, acct, err):
        now = time.time()
        acct.fail_count += 1
        acct.last_used = now
        msg = err.get("message") if isinstance(err, dict) else str(err)
        acct.last_error = (msg or "unknown error")[:300]
        if is_hard_failure(err):
            acct.consecutive_fails += 1
            acct.cooldown_until = now + min(
                COOLDOWN_BASE * (2 ** (acct.consecutive_fails - 1)),
                COOLDOWN_MAX)
        acct.persist()

    def pin(self, session_key, acct):
        if not (session_key and acct):
            return
        now = time.time()
        with self._lock:
            ent = self._pins.get(session_key)
            if ent and ent[0] == acct.id:
                ent[1] = now
                self._pins_dirty.add(session_key)
                return
            self._pins[session_key] = [acct.id, now]
            self._pins_dirty.discard(session_key)
        store.set_pin(session_key, acct.id)

    def sessions(self):
        with self._lock:
            rows = []
            for k, (aid, ts) in self._pins.items():
                a = self._accs.get(aid)
                rows.append({"session_key": k, "account_id": aid,
                             "updated": ts,
                             "aname": a.name if a else None,
                             "email": a.email if a else None})
            rows.sort(key=lambda r: r["updated"] or 0, reverse=True)
            return rows

    def unpin(self, session_key):
        with self._lock:
            self._pins.pop(session_key, None)
            self._pins_dirty.discard(session_key)
        store.unpin(session_key)

    def _pin_flush_loop(self):
        while True:
            time.sleep(PIN_FLUSH_S)
            try:
                self._flush_pins()
            except Exception:
                pass

    def _flush_pins(self):
        now = time.time()
        with self._lock:
            for k in [k for k, v in self._pins.items()
                      if now - (v[1] or 0) > SESSION_TTL]:
                self._pins.pop(k, None)
                self._pins_dirty.discard(k)
            dirty = [(v[1], k) for k, v in self._pins.items()
                     if k in self._pins_dirty]
            self._pins_dirty.clear()
        store.touch_pins(dirty)
        store.prune_pins(SESSION_TTL)

    # ---------- bootstrap ----------

    def import_detected(self, detected):
        """Import creds found on this machine (env/token-file/CLI/Desktop)."""
        n = 0
        for c in detected or []:
            name = c.source.split(":", 1)[0] if c.source else "auto"
            a, _ = self.add(c.api_key, name=name, source=c.source,
                            api_server_url=c.api_server_url,
                            webapp=c.webapp_host, api_url=c.api_url)
            if a:
                n += 1
        return n

    def summary(self):
        accs = self.accounts()
        now = time.time()
        return {
            "total": len(accs),
            "enabled": sum(1 for a in accs if not a.disabled),
            "ready": sum(1 for a in accs
                         if not a.disabled and a.cooldown_until <= now),
            "cooldown": sum(1 for a in accs
                            if not a.disabled and a.cooldown_until > now),
            "sessions": len(self.sessions()),
        }


# ---------- OAuth (PKCE) login flow ----------

_flows = {}
_flow_lock = threading.Lock()
FLOW_TTL = 900


def _b64url(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def start_flow(webapp=DEFAULT_WEBAPP, label=None):
    """Begin a manual PKCE login. Returns {id, url} — user opens the url,
    signs in, and pastes the code the page shows back into the admin UI."""
    verifier = _b64url(uuid.uuid4().bytes + uuid.uuid4().bytes)
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state = str(uuid.uuid4())
    fid = secrets.token_urlsafe(8)
    webapp = (webapp or DEFAULT_WEBAPP).rstrip("/")
    url = (f"{webapp}/auth/cli/continue?state={urllib.parse.quote(state)}"
           f"&prompt=select_account&code_challenge={challenge}"
           f"&code_challenge_method=S256&cli_pkce_marker=1")
    with _flow_lock:
        for k in [k for k, f in _flows.items()
                  if time.time() - f["created"] > FLOW_TTL]:
            _flows.pop(k, None)
        _flows[fid] = {"state": state, "verifier": verifier,
                       "created": time.time(), "label": label,
                       "webapp": webapp}
    return {"id": fid, "url": url, "state": state, "expires_in": FLOW_TTL}


def list_flows():
    with _flow_lock:
        return [{"id": k, "label": f["label"], "age_s": int(time.time() - f["created"])}
                for k, f in _flows.items()]


def cancel_flow(fid):
    with _flow_lock:
        return _flows.pop(fid, None) is not None


def _exchange_code(client, code, verifier, webapp):
    """authorization code -> session credentials. /auth/cli/token on
    api.devin.ai is the live endpoint ({code, code_verifier} -> {"token"});
    the Connect-JSON RPC and the webapp path are kept as fallbacks."""
    payload = {"code": code, "code_verifier": verifier}
    attempts = [
        (DEFAULT_API_URL + "/auth/cli/token", payload),
        (DEFAULT_API_URL + "/exa.seat_management_pb.SeatManagementService/"
         "ExchangePKCEAuthorizationCode",
         dict(payload, redirect_uri="")),
        (webapp.rstrip("/") + "/auth/cli/token", payload),
    ]
    last = "exchange failed"
    for url, body in attempts:
        try:
            r = client.post(url, json=body, headers={
                "Content-Type": "application/json",
                "Connect-Protocol-Version": "1",
                "Accept": "application/json"}, timeout=30)
            if r.status_code != 200:
                last = f"{url.rsplit('/', 1)[-1]}: HTTP {r.status_code} {r.text[:200]}"
                continue
            d = r.json()
            token = (d.get("api_key") or d.get("apiKey")
                     or d.get("session_token") or d.get("sessionToken")
                     or d.get("token"))
            if token:
                return {"token": token,
                        "api_server_url": (d.get("api_server_url")
                                           or d.get("apiServerUrl")),
                        "webapp": (d.get("devin_webapp_host")
                                   or d.get("devinWebappHost")),
                        "api_url": (d.get("devin_api_url") or d.get("devinApiUrl"))}
            last = f"{url.rsplit('/', 1)[-1]}: no token in response"
        except Exception as e:
            last = str(e)
    raise RuntimeError(last)


def complete_flow(client, fid, code, pool):
    """Exchange a pasted code -> add account -> fetch identity. -> Account."""
    with _flow_lock:
        flow = _flows.pop(fid, None)
    if not flow:
        raise RuntimeError("login session expired or unknown — start a new one")
    if time.time() - flow["created"] > FLOW_TTL:
        raise RuntimeError("login session expired — start a new one")
    cred = _exchange_code(client, code.strip(), flow["verifier"], flow["webapp"])
    acct, err = pool.add(
        cred["token"], name=flow["label"], source="oauth",
        api_server_url=cred.get("api_server_url") or DEFAULT_API_SERVER,
        webapp=cred.get("webapp") or flow["webapp"],
        api_url=cred.get("api_url") or DEFAULT_API_URL, manual=True)
    if not acct:
        raise RuntimeError(f"account not added: {err}")
    info = fetch_identity(client, acct.api_server_url, acct.token)
    if info:
        acct.name = acct.name or info.get("name") or info.get("email")
        acct.email = info.get("email") or acct.email
        acct.plan = info.get("plan") or acct.plan
        acct.persist()
    return acct


def fetch_identity(client, api_server_url, token):
    """GetUserStatus via Connect-JSON -> {email, name, plan, tier, ...} or None."""
    try:
        r = client.post(
            api_server_url.rstrip("/")
            + "/exa.seat_management_pb.SeatManagementService/GetUserStatus",
            json={"metadata": {
                "apiKey": token, "ideName": "devin-cli",
                "ideVersion": upstream.CLIENT_VERSION,
                "extensionName": "devin-cli",
                "extensionVersion": upstream.CLIENT_VERSION,
                "locale": "en"}},
            headers={"Content-Type": "application/json",
                     "Connect-Protocol-Version": "1",
                     "Accept": "application/json"}, timeout=20)
        if r.status_code != 200:
            return None
        d = r.json()
        us = d.get("userStatus") or d.get("user_status") or {}
        pi = d.get("planInfo") or d.get("plan_info") or {}
        return {
            "email": us.get("email"),
            "user_id": us.get("userId") or us.get("user_id"),
            "team_id": us.get("teamId") or us.get("team_id"),
            "plan": pi.get("planName") or pi.get("plan_name"),
            "tier": pi.get("teamsTier") or pi.get("teams_tier"),
        }
    except Exception:
        return None
