"""Admin console: embedded SPA + JSON APIs under /admin."""
import os
import sys
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import store, upstream
from .app import KNOWN_UIDS, MODEL_ALIASES

_HTML = os.path.join(os.path.dirname(__file__), "web", "admin.html")


def make_router(app):
    router = APIRouter(prefix="/admin")
    started = time.time()

    def admin_key(request: Request):
        """Admin auth: the --api-key master key, if configured."""
        key = app.state.proxy_key
        if key:
            auth = request.headers.get("authorization", "")
            qk = request.query_params.get("key")
            if auth != f"Bearer {key}" and qk != key:
                raise HTTPException(401, "unauthorized")

    @router.get("", response_class=HTMLResponse)
    @router.get("/", response_class=HTMLResponse)
    def index():
        return HTMLResponse(open(_HTML, encoding="utf-8").read())

    @router.get("/api/overview", dependencies=[Depends(admin_key)])
    def overview():
        return store.stats_overview()

    @router.get("/api/requests", dependencies=[Depends(admin_key)])
    def requests(limit: int = 50, offset: int = 0, model: str = None,
                 ok: int = None, q: str = None):
        limit = max(1, min(limit, 200))
        items, total = store.list_requests(limit, offset, model, ok, q)
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
        try:
            model, req = app.state.build_request(body)
        except Exception as e:
            raise HTTPException(400, f"bad request: {e}")
        if body.get("stream"):
            return StreamingResponse(
                app.state.sse_stream(request, body, model, req, t0),
                media_type="text/event-stream")
        return JSONResponse(app.state.collect(request, body, model, req, t0))

    class NewKey(BaseModel):
        name: str

    class KeyPatch(BaseModel):
        disabled: bool

    @router.get("/api/keys", dependencies=[Depends(admin_key)])
    def keys():
        return {"keys": store.list_keys()}

    @router.post("/api/keys", dependencies=[Depends(admin_key)])
    def new_key(body: NewKey):
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "name required")
        return {"key": store.create_key(name)}

    @router.patch("/api/keys/{kid}", dependencies=[Depends(admin_key)])
    def patch_key(kid: int, body: KeyPatch):
        store.set_key_disabled(kid, body.disabled)
        return {"ok": True}

    @router.delete("/api/keys/{kid}", dependencies=[Depends(admin_key)])
    def del_key(kid: int):
        store.delete_key(kid)
        return {"ok": True}

    @router.get("/api/status", dependencies=[Depends(admin_key)])
    def status():
        cred = app.state.creds
        c = store.counts()
        ov = store.stats_overview()
        return {
            "creds_source": cred.source,
            "upstream": cred.api_server_url,
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
        """Upstream reachability + credential validity via GetUserJwt."""
        cred = app.state.creds
        t0 = time.perf_counter()
        try:
            upstream._jwt_cache.update(jwt="", exp=0)
            upstream.get_user_jwt(app.state.http, cred.api_server_url,
                                  cred.api_key)
            return {"ok": True,
                    "latency_ms": int((time.perf_counter() - t0) * 1000)}
        except Exception as e:
            return {"ok": False, "error": str(e)[:300],
                    "latency_ms": int((time.perf_counter() - t0) * 1000)}

    return router
