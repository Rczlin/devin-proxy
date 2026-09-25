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
        return {
            "accounts": ctx.app.state.pool.summary(),
            "key_required": bool(ctx.app.state.proxy_key) or store.has_keys(),
            "admin_locked": bool(ctx.app.state.proxy_key),
            "db": store._DB_PATH,
            "log_db": store._LOG_DB,
            "diag_db": store._DIAG_DB,
            "db_size": ov["db_size"],
            "max_rows": ov["max_rows"],
            "requests_logged": c["requests"],
            "keys_count": c["keys"],
            "uptime_s": time.time() - ctx.started,
            "python": sys.version.split()[0],
            "disk": store.disk_status(),
        }

    @router.post("/api/maintenance/cleanup", dependencies=[Depends(admin_key)])
    def maintenance_cleanup(aggressive: bool = False, drop_diag: bool = False,
                            drop_log: bool = False):
        """Manually reclaim disk space. aggressive prunes to emergency rows;
        drop_diag/drop_log delete the whole diagnostic/request-log database
        files (works even at 0 bytes free — the proxy keeps serving, stats
        just reset). Same recovery path used automatically at low disk."""
        result = store.free_space(aggressive=aggressive, vacuum=True,
                                  drop_diag=drop_diag)
        if drop_log:
            result["log_db"] = store.drop_log_db()
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

