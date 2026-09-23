"""Pinned-session + status/maintenance routes."""
import sys
import time

from concurrent.futures import ThreadPoolExecutor
from fastapi import Depends, HTTPException

from .. import store, upstream


def register(router, ctx, admin_key):
    # ---------- pinned sessions ----------

    @router.get("/api/sessions", dependencies=[Depends(admin_key)])
    def sessions():
        return {"sessions": ctx.app.state.pool.sessions()}

    @router.delete("/api/sessions/{key:path}",
                   dependencies=[Depends(admin_key)])
    def session_unpin(key: str):
        ctx.app.state.pool.unpin(key)
        return {"ok": True}

    @router.post("/api/sessions/clear", dependencies=[Depends(admin_key)])
    def sessions_clear():
        for s in ctx.app.state.pool.sessions():
            ctx.app.state.pool.unpin(s["session_key"])
        return {"ok": True}

    # ---------- status ----------

    @router.get("/api/status", dependencies=[Depends(admin_key)])
    def status():
        c = store.counts()
        ov = store.stats_overview()
        # dependency + ws-route self-diagnosis: the live feed broke once on a
        # deploy whose pip-resolved fastapi/starlette combo stopped matching an
        # include_router'ed websocket. Report versions and whether /admin/api/ws
        # actually made it onto the app so a regression is one GET away.
        deps = {}
        for pkg in ("fastapi", "starlette", "uvicorn"):
            try:
                mod = __import__(pkg)
                deps[pkg] = getattr(mod, "__version__", "?")
            except Exception:
                deps[pkg] = None
        ws_routes = [
            getattr(r, "path", "")
            for r in ctx.app.routes
            if "WebSocket" in type(r).__name__
               or "websocket" in type(r).__name__.lower()
        ]
        return {
            "accounts": ctx.app.state.pool.summary(),
            "key_required": bool(ctx.app.state.proxy_key) or store.has_keys(),
            "admin_locked": bool(ctx.app.state.proxy_key),
            "db": store._DB_PATH,
            "db_size": ov["db_size"],
            "max_rows": ov["max_rows"],
            "requests_logged": c["requests"],
            "keys_count": c["keys"],
            "uptime_s": time.time() - ctx.started,
            "python": sys.version.split()[0],
            "deps": deps,
            "ws_routes": ws_routes,
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
        pool = ctx.app.state.pool
        def _probe(a):
            t0 = time.perf_counter()
            try:
                upstream.get_user_jwt(ctx.app.state.http, a.api_server_url,
                                      a.token, force_refresh=True)
                return {"account": a.display(), "ok": True,
                        "latency_ms": int((time.perf_counter() - t0) * 1000)}
            except Exception as e:
                return {"account": a.display(), "ok": False,
                        "error": str(e)[:200],
                        "latency_ms": int((time.perf_counter() - t0) * 1000)}

        # probe every enabled account concurrently — serial checks get slow
        # fast when the pool has more than a couple of accounts.
        from concurrent.futures import ThreadPoolExecutor
        accs = [a for a in pool.accounts() if not a.disabled]
        with ThreadPoolExecutor(max_workers=min(8, len(accs) or 1)) as ex:
            results = list(ex.map(_probe, accs))
        ok = bool(results) and all(r["ok"] for r in results)
        return {"ok": ok, "results": results,
                "latency_ms": max((r["latency_ms"] for r in results), default=0)}

