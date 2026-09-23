"""Admin console: embedded SPA + JSON APIs under /admin.

The console is always locked: /admin serves only a minimal login page,
POST /admin/api/login trades the master key for an HttpOnly session cookie,
and /admin/app (the SPA) plus every /admin/api/* endpoint require that
cookie (or the master key as Bearer / ?key=)."""
import collections
import gzip
import hashlib
import hmac
import io
import json
import os
import sys
import threading
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse,
                               StreamingResponse)
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import accounts as accounts_mod
from . import creds as creds_mod
from . import models as models_mod
from . import store, upstream

_HTML = os.path.join(os.path.dirname(__file__), "web", "admin.html")
_LOGIN_HTML = os.path.join(os.path.dirname(__file__), "web", "login.html")

_SESS_COOKIE = "dp_admin"
_SESS_TTL = 7 * 86400

# bundled into the diagnostic zip — decodes captures/req-<id>.json
_DECODER = '''\
"""Decode a request capture from a devin-proxy diagnostic bundle.
Usage: python decode_capture.py captures/req-<id>.json
(proto_schema.py must sit next to this script.)"""
import base64, json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from proto_schema import GetChatMessageRequest, GetChatMessageResponse

cap = json.load(open(sys.argv[1], encoding="utf-8"))
inb = cap.get("inbound") or {}
print("inbound:", inb.get("method"), inb.get("url"))
body = base64.b64decode(inb.get("body_b64") or "")
print("  body:", body[:500])
for att in cap.get("attempts") or []:
    req = GetChatMessageRequest()
    req.ParseFromString(base64.b64decode(att["request_pb_b64"]))
    print(f"--- attempt {att.get('attempt')} acct={att.get('account')} "
          f"server={att.get('server')} model={req.chat_model_uid} "
          f"tools={[t.name for t in req.tools]} "
          f"prompts={len(req.chat_message_prompts)}")
    for f in att.get("frames") or []:
        raw = base64.b64decode(f["b64"])
        if f["k"] == "frame":
            m = GetChatMessageResponse(); m.ParseFromString(raw)
            chunk = m.delta_text or m.delta_thinking or ""
            print("  frame:", repr(chunk[:120]),
                  "stop=%s" % m.stop_reason if m.stop_reason else "")
        else:
            print(f"  {f['k']}:", raw[:400])
for i, s in enumerate(cap.get("downstream") or []):
    print(f"down[{i}]:", s[:200])
'''


class _ZipStreamer(io.RawIOBase):
    """File-like object that yields zip bytes as the archive is written.

    zipfile needs a seekable target for the central directory, so we stream
    into a temp file in chunks and yield as we go — peak memory stays bounded
    to one chunk instead of holding the whole archive in RAM."""

    def __init__(self, writer):
        self._writer = writer          # callable(fileobj) — writes the zip

    def readable(self):
        return True

    def __iter__(self):
        import tempfile
        with tempfile.TemporaryFile() as f:
            self._writer(f)
            f.seek(0)
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    return
                yield chunk


def make_router(app):
    router = APIRouter(prefix="/admin")
    started = time.time()

    # ---- hardening ----
    # A per-IP rate limit on the login endpoint so the master key can't
    # be brute-forced online. (Security headers are added app-wide by the
    # _stealth middleware in app.py.)
    _LOGIN_MAX = 5            # attempts allowed per window
    _LOGIN_WIN = 300          # seconds
    _login_hits = collections.defaultdict(list)
    _login_lock = threading.Lock()

    def _login_check(ip):
        """-> retry-after s if this IP is over the failed-login budget."""
        now = time.time()
        with _login_lock:
            hits = _login_hits.get(ip, [])
            while hits and hits[0] <= now - _LOGIN_WIN:
                hits.pop(0)
            return int(hits[0] + _LOGIN_WIN - now) + 1 \
                if len(hits) >= _LOGIN_MAX else 0

    def _login_failed(ip):
        """Record one failed attempt for the IP."""
        with _login_lock:
            _login_hits[ip].append(time.time())
            # bound the map so a spray of source IPs can't grow it forever
            if len(_login_hits) > 10000:
                for k, v in list(_login_hits.items()):
                    if not v:
                        _login_hits.pop(k, None)

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
        ip = request.client.host if request.client else "?"
        wait = _login_check(ip)
        if wait:
            raise HTTPException(429, f"too many attempts, retry in {wait}s",
                                headers={"Retry-After": str(wait)})
        ok = app.state.proxy_key and hmac.compare_digest(
            body.key, app.state.proxy_key)
        if not ok:
            _login_failed(ip)
            time.sleep(0.4)                 # slow down brute force
            raise HTTPException(401, "invalid key")
        _login_hits.pop(ip, None)           # success clears the window
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
    def overview(hours: int = 24):
        d = store.stats_overview(hours if 0 < hours <= 24 * 366 else None)
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
        return d

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

    @router.get("/api/requests/{rid}/capture",
                dependencies=[Depends(admin_key)])
    def request_capture(rid: int):
        """Download the full-fidelity capture for one request — raw inbound
        body/headers, exact upstream protobuf + every wire frame, outbound
        SSE. Untruncated."""
        c = store.get_capture(rid)
        if c is None:
            raise HTTPException(404, "no capture stored for this request")
        body = json.dumps(c, ensure_ascii=False, indent=1, default=str)
        return Response(
            content=body, media_type="application/json",
            headers={"Content-Disposition":
                     f'attachment; filename="req-{rid}-capture.json"'})

    @router.post("/api/requests/clear", dependencies=[Depends(admin_key)])
    def clear_requests():
        store.clear_requests()
        return {"ok": True}

    def _git_head():
        try:
            import subprocess
            here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                                 cwd=here, capture_output=True, timeout=3,
                                 text=True)
            return out.stdout.strip() or None
        except Exception:
            return None

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
            "git_head": _git_head(),
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

    @router.get("/api/models", dependencies=[Depends(admin_key)])
    def models():
        models_mod.maybe_refresh(app.state.http, app.state.pool)
        return {"families": models_mod.grouped(include_hidden=True),
                "models": [e["uid"]
                           for e in models_mod.entries()],
                "aliases": models_mod.aliases(),
                "user_aliases": models_mod.user_aliases(),
                "default_model": models_mod.default_uid(),
                "default_effort": models_mod.default_effort(),
                "family_efforts": models_mod.family_efforts(),
                "hidden_aliases": sorted(models_mod.hidden_aliases()),
                "hidden_models": sorted(models_mod.hidden_models()),
                "sync": models_mod.sync_info(),
                "stats": store.list_models()}

    @router.delete("/api/models/entries/{uid}",
                   dependencies=[Depends(admin_key)])
    def model_hide(uid: str):
        hid = models_mod.hidden_models() | {uid.strip()}
        store.meta_set("model_hide", json.dumps(sorted(hid)))
        return {"ok": True,
                "hidden_models": sorted(models_mod.hidden_models())}

    @router.post("/api/models/entries/unhide",
                 dependencies=[Depends(admin_key)])
    def model_unhide(body: Optional[dict] = None):
        uid = str((body or {}).get("uid") or "").strip()
        hid = models_mod.hidden_models() - {uid}
        store.meta_set("model_hide", json.dumps(sorted(hid)))
        return {"ok": True, "hidden_models": sorted(hid)}

    @router.delete("/api/models/aliases/{name}",
                   dependencies=[Depends(admin_key)])
    def alias_delete(name: str):
        """Delete a user alias, or hide a builtin/remote one."""
        name = name.strip()
        ua = models_mod.user_aliases()
        if name in ua:
            ua.pop(name)
            store.meta_set("model_aliases", json.dumps(ua))
        else:
            hid = models_mod.hidden_aliases() | {name}
            store.meta_set("alias_hide", json.dumps(sorted(hid)))
        return {"ok": True, "aliases": models_mod.aliases(),
                "hidden_aliases": sorted(models_mod.hidden_aliases())}

    @router.post("/api/models/aliases/unhide",
                 dependencies=[Depends(admin_key)])
    def alias_unhide(body: Optional[dict] = None):
        name = str((body or {}).get("name") or "").strip()
        hid = models_mod.hidden_aliases() - {name}
        store.meta_set("alias_hide", json.dumps(sorted(hid)))
        return {"ok": True, "aliases": models_mod.aliases(),
                "hidden_aliases": sorted(hid)}

    @router.post("/api/models/refresh", dependencies=[Depends(admin_key)])
    def models_refresh():
        accs = [a for a in app.state.pool.accounts() if not a.disabled]
        return models_mod.refresh(app.state.http, accs, force=True)

    class ModelSettings(BaseModel):
        default_model: Optional[str] = None
        default_effort: Optional[str] = None
        models_url: Optional[str] = None
        aliases: Optional[str] = None     # "alias=uid[@effort]" per line, or JSON
        family_efforts: Optional[dict] = None   # {family: effort|""} merged

    @router.patch("/api/models/settings", dependencies=[Depends(admin_key)])
    def models_settings(body: ModelSettings):
        if body.default_model is not None:
            d = body.default_model.strip()
            if d and d not in models_mod.uids() \
                    and d not in models_mod.aliases():
                raise HTTPException(400, "unknown model")
            store.meta_set("default_model", d)
        if body.default_effort is not None:
            e = body.default_effort.strip().lower()
            if e and e not in models_mod.EFFORT_SUFFIX:
                raise HTTPException(400, "unknown effort")
            store.meta_set("default_effort", e)
        if body.models_url is not None:
            u = body.models_url.strip()
            if u and not u.startswith(("http://", "https://")):
                raise HTTPException(400, "url must be http(s)")
            store.meta_set("models_url", u)
        if body.aliases is not None:
            parsed = {}
            txt = body.aliases.strip()
            try:
                d = json.loads(txt) if txt.startswith("{") else None
            except Exception:
                d = None
            if isinstance(d, dict):
                parsed = {str(k).strip(): str(v).strip()
                          for k, v in d.items()
                          if str(k).strip() and str(v).strip()}
            else:
                for line in txt.splitlines():
                    if "=" in line:
                        k, v = line.split("=", 1)
                        if k.strip() and v.strip():
                            parsed[k.strip()] = v.strip()
            for k, v in parsed.items():
                eff = v.rpartition("@")[2].strip().lower()
                if "@" in v and eff not in models_mod.EFFORT_SUFFIX:
                    raise HTTPException(400, f"{k}: unknown effort @{eff}")
            store.meta_set("model_aliases", json.dumps(parsed))
        if body.family_efforts is not None:
            cur = models_mod.family_efforts()
            for k, v in body.family_efforts.items():
                fam, e = str(k).strip(), str(v).strip().lower()
                if not fam:
                    continue
                if not e:
                    cur.pop(fam.replace(".", "-"), None)
                elif e in models_mod.EFFORT_SUFFIX:
                    cur[fam.replace(".", "-")] = e
                else:
                    raise HTTPException(400, f"{fam}: unknown effort")
            store.meta_set("family_efforts", json.dumps(cur))
        return {"ok": True, "default_model": models_mod.default_uid(),
                "default_effort": models_mod.default_effort(),
                "family_efforts": models_mod.family_efforts(),
                "aliases": models_mod.aliases(),
                "models_url": models_mod.models_url()}

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
            "disk": store.disk_status(),
        }

    @router.post("/api/maintenance/cleanup", dependencies=[Depends(admin_key)])
    def maintenance_cleanup(aggressive: bool = False):
        """Manually reclaim disk space: prune old request/capture/response/
        session history, checkpoint the WAL and VACUUM. Same recovery path
        used automatically when the disk fills up."""
        result = store.free_space(aggressive=aggressive, vacuum=True)
        result["disk"] = store.disk_status()
        return result

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
