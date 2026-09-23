"""Admin console: embedded SPA + JSON APIs under /admin.

The console is always locked: /admin serves only a minimal login page,
POST /admin/api/login trades the master key for an HttpOnly session cookie,
and /admin/app (the SPA) plus every /admin/api/* endpoint require that
cookie (or the master key as an Authorization: Bearer credential)."""
import hmac
import json
import os
import sys
import time
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import (HTMLResponse, JSONResponse,
                               RedirectResponse, StreamingResponse)
from pydantic import BaseModel

from .admin_ctx import AdminCtx
from .admin_export import _DECODER, _ZipStreamer, git_head
from .admin_routes import (accounts as acct_routes,
                           keys as key_routes,
                           live as live_routes,
                           models as model_routes,
                           oauth as oauth_routes,
                           play as play_routes,
                           requests as req_routes,
                           status as status_routes)
from . import models as models_mod
from . import store

_HTML = os.path.join(os.path.dirname(__file__), "web", "admin.html")
_LOGIN_HTML = os.path.join(os.path.dirname(__file__), "web", "login.html")

_SESS_COOKIE = "dp_admin"
_SESS_TTL = 7 * 86400
_MAX_EXPORT = 20000          # row cap for CSV/JSONL log export

# bundled into the diagnostic zip — decodes captures/req-<id>.json
def make_router(app):
    router = APIRouter(prefix="/admin")
    started = time.time()
    ctx = AdminCtx(app, started)
    admin_key = ctx.admin_key          # FastAPI dependency
    _authed = ctx.authed               # used by the HTML/static routes

    class LoginBody(BaseModel):
        key: str = ""

    @router.post("/api/login")
    def login(body: LoginBody, request: Request):
        ip = request.client.host if request.client else "?"
        wait = ctx.login_check(ip)
        if wait:
            raise HTTPException(429, f"too many attempts, retry in {wait}s",
                                headers={"Retry-After": str(wait)})
        ok = app.state.proxy_key and hmac.compare_digest(
            body.key, app.state.proxy_key)
        if not ok:
            ctx.login_failed(ip)
            time.sleep(0.4)                 # slow down brute force
            raise HTTPException(401, "invalid key")
        ctx.login_ok(ip)                    # success clears the window
        resp = JSONResponse({"ok": True})
        ctx.set_session_cookie(resp, request)
        return resp

    @router.post("/api/logout")
    def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie("dp_admin", path="/admin")
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

    _WEB = os.path.dirname(_HTML)
    _MIME = {".css": "text/css", ".js": "application/javascript"}
    _STATIC_ROOTS = ("admin.css", "js/")

    @router.get("/static/{name:path}", dependencies=[Depends(admin_key)])
    def static_asset(name: str):
        """SPA assets live next to admin.html — served only to authed
        sessions so the console code isn't exposed pre-login. Whitelisted to
        admin.css and js/*.js; anything else (incl. .. traversal) 404s."""
        name = name.replace("\\", "/").lstrip("/")
        if not (name == "admin.css"
                or (name.startswith("js/") and name.endswith(".js")
                    and ".." not in name)):
            raise HTTPException(404, "not found")
        path = os.path.normpath(os.path.join(_WEB, name))
        if not path.startswith(os.path.abspath(_WEB)):
            raise HTTPException(404, "not found")
        try:
            body = open(path, encoding="utf-8").read()
        except OSError:
            raise HTTPException(404, "not found")
        return Response(body, media_type=_MIME.get(
            os.path.splitext(name)[1], "text/plain"),
            headers={"Cache-Control": "no-cache"})

    @router.get("/api/overview", dependencies=[Depends(admin_key)])
    def overview(hours: int = 24):
        if hours < 0 or hours > 24 * 366:
            raise HTTPException(400, f"hours must be 0..{24*366} (0 = all)")
        d = store.stats_overview(hours or None)
        accs = app.state.pool.accounts()
        now = time.time()
        p = app.state.pool.summary()
        p["in_flight"] = sum(a.in_flight for a in accs)
        p["accounts"] = [{
            "id": a.id, "label": a.display(), "plan": a.plan,
            "state": "disabled" if a.disabled else (
                "cooldown" if a.cooldown_until > now else "ready"),
            "in_flight": a.in_flight,
            "max_concurrent": a.max_concurrent,
            "cooldown_s": max(0, round(a.cooldown_until - now)),
            "fails": a.consecutive_fails,
        } for a in accs]
        d["pool"] = p
        d["uptime_s"] = time.time() - started
        d["disk"] = store.disk_status()
        return d

    @router.get("/api/export", dependencies=[Depends(admin_key)])
    def export_bundle(limit: int = 500, hours: int = 0):
        """Diagnostic bundle zip: full request rows (inbound body, upstream
        event timeline, outbound SSE frames), stored Responses objects,
        account/pool state, model catalog, meta config and env info.
        Credentials are never included — tokens/master key are stripped."""
        import io
        import platform
        import zipfile

        from . import __version__
        from . import app as app_mod

        reqs = store.export_requests(limit=limit, hours=hours)
        env = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "version": __version__,
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "uptime_s": round(time.time() - started, 1),
            "db_path": store._DB_PATH,
            "db_size": os.path.getsize(store._DB_PATH)
            if os.path.exists(store._DB_PATH) else None,
            "max_rows": store._MAX_ROWS,
            "capture_enabled": app_mod._CAPTURE,
            "capture_max_bytes": app_mod._CAP_MAX,
            "capture_keep_successes": store._CAP_KEEP,
            "git_head": git_head(),
            "env": {k: ("***" if any(s in k for s in ("KEY", "TOKEN", "SECRET"))
                    else v)
                    for k, v in os.environ.items() if k.startswith("DEVIN_")},
            "counts": store.counts(),
        }
        models = {
            "entries": models_mod.entries(include_hidden=True),
            "aliases": models_mod.aliases(),
            "user_aliases": models_mod.user_aliases(),
            "default_model": models_mod.default_uid(),
            "default_effort": models_mod.default_effort(),
            "family_efforts": models_mod.family_efforts(),
            "hidden_aliases": sorted(models_mod.hidden_aliases()),
            "hidden_models": sorted(models_mod.hidden_models()),
            "sync": models_mod.sync_info(),
        }
        stats = {"overview": store.stats_overview(None),
                 "pool": app.state.pool.summary(),
                 "per_model": store.list_models(),
                 "per_account": store.account_stats()}

        readme = (
            "devin-proxy diagnostic bundle\n"
            f"generated: {env['generated_at']}  version: {__version__}  "
            f"git: {env['git_head'] or '-'}\n\n"
            "contents:\n"
            "  env.json         runtime, env vars (secrets masked), db info\n"
            "  requests.jsonl   logged requests, ALL columns — one row per\n"
            "                   line: request_json = inbound body, events_json\n"
            "                   = upstream frame timeline (attempt/msg/\n"
            "                   upstream_err/trailer), sse_json = outbound SSE\n"
            "  responses.jsonl  stored /v1/responses objects (input items +\n"
            "                   response) used by previous_response_id chains\n"
            "  accounts.json    upstream pool state — tokens NOT included\n"
            "  keys.json        api key metadata (hashes/secrets excluded)\n"
            "  sessions.json    session_key -> account pins\n"
            "  models.json      model catalog snapshot + alias/effort config\n"
            "  meta.json        persisted settings (master_key excluded)\n"
            "  stats.json       aggregate stats (overview/pool/per-model)\n"
            "  captures/        FULL-FIDELITY packet capture per request\n"
            "                   (untruncated — claude-tap style):\n"
            "                   inbound   = raw request bytes + masked headers\n"
            "                   attempts[]= {account, server, request_pb_b64,\n"
            "                     request (decoded GetChatMessageRequest),\n"
            "                     frames[] = {k: resp_headers|frame|trailer|\n"
            "                     http_body, b64: raw wire payload}}\n"
            "                   downstream = every SSE frame sent to client\n\n"
            "tips: requests.jsonl rows with ok=0 are failures; the upstream\n"
            "trailer error lives in events_json[].detail / .trailer — and\n"
            "verbatim (untruncated) in captures/req-<id>.json trailer frames.\n"
            "Decode captures offline: python decode_capture.py <file>.json\n"
            "(proto_schema.py is this build's proto definition — required).\n")

        def jl(rows):
            return "".join(json.dumps(r, ensure_ascii=False, default=str)
                           + "\n" for r in rows)

        def _write(f):
            with zipfile.ZipFile(f, "w", zipfile.ZIP_DEFLATED,
                                 compresslevel=6) as z:
                # ship the proto schema + a decoder so the packet captures
                # are self-contained:
                # `python decode_capture.py captures/req-1.json`
                try:
                    here = os.path.dirname(os.path.abspath(__file__))
                    z.write(os.path.join(here, "proto.py"),
                            "proto_schema.py")
                except Exception:
                    pass
                z.writestr("decode_capture.py", _DECODER)
                for c in store.export_captures():
                    try:
                        z.writestr(f"captures/req-{c['request_id']}.json",
                                   gzip.decompress(c["data"]))
                    except Exception as e:
                        z.writestr(f"captures/req-{c['request_id']}.err",
                                   f"undecodable capture: {e}")
                z.writestr("README.txt", readme)
                z.writestr("env.json", json.dumps(env, ensure_ascii=False,
                                                  indent=1, default=str))
                z.writestr("requests.jsonl", jl(reqs))
                z.writestr("responses.jsonl", jl(store.export_responses()))
                z.writestr("accounts.json", json.dumps(
                    store.export_accounts(), ensure_ascii=False, indent=1))
                z.writestr("keys.json", json.dumps(
                    store.export_keys(), ensure_ascii=False, indent=1))
                z.writestr("sessions.json", json.dumps(
                    app.state.pool.sessions(), ensure_ascii=False, indent=1,
                    default=str))
                z.writestr("models.json",
                           json.dumps(models, ensure_ascii=False,
                                      indent=1, default=str))
                z.writestr("meta.json",
                           json.dumps(store.export_meta(),
                                      ensure_ascii=False, indent=1,
                                      default=str))
                z.writestr("stats.json",
                           json.dumps(stats, ensure_ascii=False,
                                      indent=1, default=str))
        fn = "devin-proxy-diag-" + time.strftime("%Y%m%d-%H%M%S") + ".zip"
        # stream the archive straight to the client instead of building it
        # in memory — a big capture set can otherwise balloon RSS.
        return StreamingResponse(
            _ZipStreamer(_write), media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{fn}"'})

    # domain routes live in admin_routes/ — registered in the same order
    # they were defined here so route precedence is unchanged.
    req_routes.register(router, ctx, admin_key)
    model_routes.register(router, ctx, admin_key)
    play_routes.register(router, ctx, admin_key)
    key_routes.register(router, ctx, admin_key)
    acct_routes.register(router, ctx, admin_key)
    oauth_routes.register(router, ctx, admin_key)
    status_routes.register(router, ctx, admin_key)
    live_routes.register(router, ctx, admin_key)

    return router
