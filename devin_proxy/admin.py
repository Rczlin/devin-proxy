"""Admin console: embedded SPA + JSON APIs under /admin.

The console is always locked: /admin serves only a minimal login page,
POST /admin/api/login trades the master key for an HttpOnly session cookie,
and /admin/app (the SPA) plus every /admin/api/* endpoint require that
cookie (or the master key as Bearer / ?key=)."""
import hashlib
import hmac
import os
import sys
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse,
                               StreamingResponse)
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import accounts as accounts_mod
from . import creds as creds_mod
from . import store, upstream
from .app import KNOWN_UIDS, MODEL_ALIASES

_HTML = os.path.join(os.path.dirname(__file__), "web", "admin.html")
_LOGIN_HTML = os.path.join(os.path.dirname(__file__), "web", "login.html")

_SESS_COOKIE = "dp_admin"
_SESS_TTL = 7 * 86400


def make_router(app):
    router = APIRouter(prefix="/admin")
    started = time.time()

    def _sess_token():
        exp = int(time.time()) + _SESS_TTL
        sig = hmac.new(app.state.proxy_key.encode(), str(exp).encode(),
                       hashlib.sha256).hexdigest()
        return f"{exp}.{sig}"

    def _sess_ok(token):
        try:
            exp, sig = token.split(".", 1)
            exp = int(exp)
        except (ValueError, AttributeError):
            return False
        if exp < time.time():
            return False
        want = hmac.new(app.state.proxy_key.encode(), str(exp).encode(),
                        hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, want)

    def _authed(request: Request):
        if _sess_ok(request.cookies.get(_SESS_COOKIE, "")):
            return True
        key = app.state.proxy_key
        auth = request.headers.get("authorization", "")
        return bool(key) and (auth == f"Bearer {key}"
                              or request.query_params.get("key") == key)

    def admin_key(request: Request):
        if not _authed(request):
            raise HTTPException(401, "unauthorized")

    class LoginBody(BaseModel):
        key: str = ""

    @router.post("/api/login")
    def login(body: LoginBody, request: Request):
        if not app.state.proxy_key or body.key != app.state.proxy_key:
            time.sleep(0.4)                 # slow down brute force
            raise HTTPException(401, "invalid key")
        resp = JSONResponse({"ok": True})
        resp.set_cookie(_SESS_COOKIE, _sess_token(), max_age=_SESS_TTL,
                        httponly=True, samesite="lax",
                        secure=request.url.scheme == "https")
        return resp

    @router.post("/api/logout")
    def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(_SESS_COOKIE)
        return resp

    @router.get("", response_class=HTMLResponse)
    @router.get("/", response_class=HTMLResponse)
    def index():
        return HTMLResponse(open(_LOGIN_HTML, encoding="utf-8").read())

    @router.get("/app", response_class=HTMLResponse)
    def console(request: Request):
        if not _authed(request):
            return RedirectResponse("/admin")
        return HTMLResponse(open(_HTML, encoding="utf-8").read())

    @router.get("/api/overview", dependencies=[Depends(admin_key)])
    def overview():
        return store.stats_overview()

    @router.get("/api/requests", dependencies=[Depends(admin_key)])
    def requests(limit: int = 50, offset: int = 0, model: str = None,
                 ok: int = None, q: str = None, account: str = None,
                 flag: str = None):
        limit = max(1, min(limit, 200))
        items, total = store.list_requests(limit, offset, model, ok, q,
                                           account, flag)
        return {"items": items, "total": total}

    @router.get("/api/requests/{rid}", dependencies=[Depends(admin_key)])
    def request_detail(rid: int):
        r = store.get_request(rid)
        if not r:
            raise HTTPException(404, "not found")
        return r

    @router.post("/api/requests/clear", dependencies=[Depends(admin_key)])
    def clear_requests():
        store.clear_requests()
        return {"ok": True}

    @router.get("/api/models", dependencies=[Depends(admin_key)])
    def models():
        return {"models": KNOWN_UIDS + sorted(MODEL_ALIASES),
                "stats": store.list_models()}

    @router.post("/api/playground", dependencies=[Depends(admin_key)])
    async def playground(request: Request):
        """Chat test routed through the normal pipeline (logged, no key needed)."""
        body = await request.json()
        t0 = time.perf_counter()
        force = None
        if body.get("account_id"):
            force = app.state.pool.get(int(body["account_id"]))
            if not force:
                raise HTTPException(404, "account not found")
        request.state.key_name = "admin"
        if body.get("stream"):
            return StreamingResponse(
                app.state.sse_stream(request, body,
                                     app.state.resolve_model(body),
                                     "admin:playground", t0, force),
                media_type="text/event-stream")
        resp = await run_in_threadpool(
            app.state.collect, request, body,
            app.state.resolve_model(body), "admin:playground", t0, force)
        return JSONResponse(resp)

    class NewKey(BaseModel):
        name: str
        models: Optional[str] = None        # comma list; empty = all models
        max_concurrent: int = 0             # 0 = unlimited

    class KeyPatch(BaseModel):
        name: Optional[str] = None
        disabled: Optional[bool] = None
        models: Optional[str] = None        # "" clears the allowlist
        max_concurrent: Optional[int] = None

    @router.get("/api/keys", dependencies=[Depends(admin_key)])
    def keys():
        return {"keys": store.list_keys()}

    @router.post("/api/keys", dependencies=[Depends(admin_key)])
    def new_key(body: NewKey):
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "name required")
        return {"key": store.create_key(name, models=body.models,
                                        max_concurrent=body.max_concurrent)}

    @router.patch("/api/keys/{kid}", dependencies=[Depends(admin_key)])
    def patch_key(kid: int, body: KeyPatch):
        fields = {}
        if body.name is not None:
            fields["name"] = body.name.strip()
        if body.disabled is not None:
            fields["disabled"] = body.disabled
        if body.models is not None:
            fields["models"] = body.models
        if body.max_concurrent is not None:
            fields["max_concurrent"] = body.max_concurrent
        store.update_key(kid, **fields)
        return {"ok": True}

    @router.delete("/api/keys/{kid}", dependencies=[Depends(admin_key)])
    def del_key(kid: int):
        store.delete_key(kid)
        return {"ok": True}

    # ---------- upstream accounts ----------

    class NewAccount(BaseModel):
        name: str = ""
        token: str

    class AccountPatch(BaseModel):
        name: Optional[str] = None
        disabled: Optional[bool] = None
        max_concurrent: Optional[int] = None
        models: Optional[str] = None        # "" clears the allowlist

    @router.get("/api/accounts", dependencies=[Depends(admin_key)])
    def list_accounts():
        pool = app.state.pool
        stats = store.account_stats()
        out = []
        for a in pool.accounts():
            d = a.public()
            d["usage"] = stats.get(a.display()) or stats.get(a.name) or {}
            out.append(d)
        return {"accounts": out, "pool": pool.summary()}

    @router.post("/api/accounts", dependencies=[Depends(admin_key)])
    def add_account(body: NewAccount):
        pool = app.state.pool
        acct, err = pool.add(body.token, name=body.name.strip() or None,
                             source="manual", manual=True)
        if not acct:
            raise HTTPException(400, err or "not added")
        info = accounts_mod.fetch_identity(app.state.http,
                                           acct.api_server_url, acct.token)
        if info:
            acct.name = acct.name or info.get("name") or info.get("email")
            acct.email = info.get("email") or acct.email
            acct.plan = info.get("plan") or acct.plan
            acct.persist()
        return {"account": acct.public(), "identity": info}

    @router.patch("/api/accounts/{aid}", dependencies=[Depends(admin_key)])
    def patch_account(aid: int, body: AccountPatch):
        a = app.state.pool.get(aid)
        if not a:
            raise HTTPException(404, "not found")
        fields = {}
        if body.name is not None:
            fields["name"] = body.name.strip()
        if body.disabled is not None:
            fields["disabled"] = int(body.disabled)
        if body.max_concurrent is not None:
            fields["max_concurrent"] = body.max_concurrent
        if body.models is not None:
            fields["models"] = body.models
        app.state.pool.update(aid, **fields)
        return {"account": app.state.pool.get(aid).public()}

    @router.delete("/api/accounts/{aid}", dependencies=[Depends(admin_key)])
    def delete_account(aid: int):
        if not app.state.pool.get(aid):
            raise HTTPException(404, "not found")
        app.state.pool.remove(aid)
        return {"ok": True}

    @router.post("/api/accounts/{aid}/test", dependencies=[Depends(admin_key)])
    def test_account(aid: int):
        a = app.state.pool.get(aid)
        if not a:
            raise HTTPException(404, "not found")
        t0 = time.perf_counter()
        try:
            upstream.get_user_jwt(app.state.http, a.api_server_url, a.token,
                                  force_refresh=True)
            ms = int((time.perf_counter() - t0) * 1000)
            a.consecutive_fails = 0
            a.cooldown_until = 0
            a.last_error = None
            a.persist()
            return {"ok": True, "latency_ms": ms}
        except Exception as e:
            return {"ok": False, "error": str(e)[:300],
                    "latency_ms": int((time.perf_counter() - t0) * 1000)}

    @router.post("/api/accounts/{aid}/refresh", dependencies=[Depends(admin_key)])
    def refresh_account(aid: int):
        a = app.state.pool.get(aid)
        if not a:
            raise HTTPException(404, "not found")
        info = accounts_mod.fetch_identity(app.state.http,
                                           a.api_server_url, a.token)
        if not info:
            raise HTTPException(502, "identity fetch failed")
        a.email = info.get("email") or a.email
        a.plan = info.get("plan") or a.plan
        a.persist()
        return {"account": a.public(), "identity": info}

    @router.post("/api/accounts/import", dependencies=[Depends(admin_key)])
    def import_detected():
        detected = creds_mod.detect_all()
        added, skipped = [], []
        for c in detected:
            a, err = app.state.pool.add(
                c.api_key, name=c.source.split(":", 1)[0] if c.source else "auto",
                source=c.source, api_server_url=c.api_server_url,
                webapp=c.webapp_host, api_url=c.api_url)
            (added if a else skipped).append(
                {"source": c.source, "reason": err})
        return {"added": len(added), "skipped": skipped,
                "accounts": [a.public() for a in app.state.pool.accounts()]}

    # ---------- OAuth login ----------

    class OauthStart(BaseModel):
        label: str = ""
        webapp: Optional[str] = None

    class OauthComplete(BaseModel):
        code: str
        label: Optional[str] = None

    @router.post("/api/oauth/start", dependencies=[Depends(admin_key)])
    def oauth_start(body: OauthStart):
        f = accounts_mod.start_flow(
            webapp=body.webapp or accounts_mod.DEFAULT_WEBAPP,
            label=body.label.strip() or None)
        return f

    @router.get("/api/oauth", dependencies=[Depends(admin_key)])
    def oauth_list():
        return {"flows": accounts_mod.list_flows()}

    @router.post("/api/oauth/{fid}/complete", dependencies=[Depends(admin_key)])
    def oauth_complete(fid: str, body: OauthComplete):
        try:
            acct = accounts_mod.complete_flow(
                app.state.http, fid, body.code, app.state.pool)
        except Exception as e:
            raise HTTPException(400, str(e)[:400])
        if body.label:
            acct.name = body.label.strip() or acct.name
            acct.persist()
        return {"account": acct.public()}

    @router.delete("/api/oauth/{fid}", dependencies=[Depends(admin_key)])
    def oauth_cancel(fid: str):
        return {"ok": accounts_mod.cancel_flow(fid)}

    # ---------- pinned sessions ----------

    @router.get("/api/sessions", dependencies=[Depends(admin_key)])
    def sessions():
        return {"sessions": app.state.pool.sessions()}

    @router.delete("/api/sessions/{key}", dependencies=[Depends(admin_key)])
    def session_unpin(key: str):
        app.state.pool.unpin(key)
        return {"ok": True}

    @router.post("/api/sessions/clear", dependencies=[Depends(admin_key)])
    def sessions_clear():
        for s in app.state.pool.sessions():
            app.state.pool.unpin(s["session_key"])
        return {"ok": True}

    # ---------- status ----------

    @router.get("/api/status", dependencies=[Depends(admin_key)])
    def status():
        c = store.counts()
        ov = store.stats_overview()
        return {
            "accounts": app.state.pool.summary(),
            "key_required": bool(app.state.proxy_key) or store.has_keys(),
            "admin_locked": bool(app.state.proxy_key),
            "db": store._DB_PATH,
            "db_size": ov["db_size"],
            "max_rows": ov["max_rows"],
            "requests_logged": c["requests"],
            "keys_count": c["keys"],
            "uptime_s": time.time() - started,
            "python": sys.version.split()[0],
        }

    @router.get("/api/ping", dependencies=[Depends(admin_key)])
    def ping():
        """Connectivity check across every enabled account."""
        pool = app.state.pool
        results = []
        for a in pool.accounts():
            if a.disabled:
                continue
            t0 = time.perf_counter()
            try:
                upstream.get_user_jwt(app.state.http, a.api_server_url,
                                      a.token, force_refresh=True)
                results.append({"account": a.display(), "ok": True,
                                "latency_ms": int((time.perf_counter() - t0) * 1000)})
            except Exception as e:
                results.append({"account": a.display(), "ok": False,
                                "error": str(e)[:200],
                                "latency_ms": int((time.perf_counter() - t0) * 1000)})
        ok = bool(results) and all(r["ok"] for r in results)
        return {"ok": ok, "results": results,
                "latency_ms": max((r["latency_ms"] for r in results), default=0)}

    return router
