"""Upstream-account admin routes: pool CRUD, test/refresh, import, bulk."""
import json
import time

from fastapi import Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

from .. import accounts as accounts_mod
from .. import creds as creds_mod
from .. import store, upstream


def register(router, ctx, admin_key):
    # ---------- upstream accounts ----------

    class NewAccount(BaseModel):
        name: str = ""
        token: str

    class AccountPatch(BaseModel):
        name: Optional[str] = None
        disabled: Optional[bool] = None
        max_concurrent: Optional[int] = None
        models: Optional[str] = None        # "" clears the allowlist
        quota_limit_pct: Optional[int] = None  # soft weekly-limit %, 0 = unset

    @router.get("/api/accounts", dependencies=[Depends(admin_key)])
    def list_accounts():
        pool = ctx.app.state.pool
        stats = store.account_stats()
        out = []
        for a in pool.accounts():
            d = a.public()
            d["usage"] = stats.get(a.display()) or stats.get(a.name) or {}
            out.append(d)
        return {"accounts": out, "pool": pool.summary()}

    @router.post("/api/accounts", dependencies=[Depends(admin_key)])
    def add_account(body: NewAccount):
        pool = ctx.app.state.pool
        acct, err = pool.add(body.token, name=body.name.strip() or None,
                             source="manual", manual=True)
        if not acct:
            raise HTTPException(400, err or "not added")
        info = accounts_mod.fetch_identity(ctx.app.state.http,
                                           acct.api_server_url, acct.token)
        if info:
            acct.name = acct.name or info.get("name") or info.get("email")
            acct.email = info.get("email") or acct.email
            acct.plan = info.get("plan") or acct.plan
            acct.persist()
        return {"account": acct.public(), "identity": info}

    @router.patch("/api/accounts/{aid}", dependencies=[Depends(admin_key)])
    def patch_account(aid: int, body: AccountPatch):
        a = ctx.app.state.pool.get(aid)
        if not a:
            raise HTTPException(404, "not found")
        fields = {}
        if body.name is not None:
            fields["name"] = body.name.strip()
        if body.disabled is not None:
            fields["disabled"] = int(body.disabled)
        if body.max_concurrent is not None:
            if body.max_concurrent < 0:
                raise HTTPException(400, "max_concurrent must be >= 0")
            fields["max_concurrent"] = body.max_concurrent
        if body.models is not None:
            fields["models"] = body.models
        if body.quota_limit_pct is not None:
            if not (0 <= body.quota_limit_pct <= 100):
                raise HTTPException(400, "quota_limit_pct must be 0-100")
            fields["quota_limit_pct"] = body.quota_limit_pct
        ctx.app.state.pool.update(aid, **fields)
        return {"account": ctx.app.state.pool.get(aid).public()}

    class BulkAccounts(BaseModel):
        ids: list[int]
        disabled: Optional[bool] = None

    @router.post("/api/accounts/bulk", dependencies=[Depends(admin_key)])
    def bulk_accounts(body: BulkAccounts):
        """Enable/disable a set of accounts in one call."""
        if body.disabled is None:
            raise HTTPException(400, "disabled required")
        n = 0
        for aid in body.ids:
            if ctx.app.state.pool.get(aid):
                ctx.app.state.pool.update(aid, disabled=int(body.disabled))
                n += 1
        return {"ok": True, "updated": n}

    @router.delete("/api/accounts/{aid}", dependencies=[Depends(admin_key)])
    def delete_account(aid: int):
        if not ctx.app.state.pool.get(aid):
            raise HTTPException(404, "not found")
        ctx.app.state.pool.remove(aid)
        return {"ok": True}

    @router.post("/api/accounts/{aid}/test", dependencies=[Depends(admin_key)])
    def test_account(aid: int):
        a = ctx.app.state.pool.get(aid)
        if not a:
            raise HTTPException(404, "not found")
        t0 = time.perf_counter()
        try:
            upstream.get_user_jwt(ctx.app.state.http, a.api_server_url, a.token,
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
        a = ctx.app.state.pool.get(aid)
        if not a:
            raise HTTPException(404, "not found")
        info = accounts_mod.fetch_identity(ctx.app.state.http,
                                           a.api_server_url, a.token)
        if not info:
            raise HTTPException(502, "identity fetch failed")
        a.email = info.get("email") or a.email
        a.plan = info.get("plan") or a.plan
        if info.get("quota"):
            a.quota_json = json.dumps(info["quota"], separators=(",", ":"))
            a.quota_fetched = time.time()
        a.persist()
        return {"account": a.public(), "identity": info}

    @router.post("/api/accounts/quota-all", dependencies=[Depends(admin_key)])
    def refresh_quota_all():
        """Refresh upstream quota for every enabled account; a per-account
        failure is reported but doesn't abort the batch."""
        out = []
        for a in ctx.app.state.pool.accounts():
            if a.disabled:
                continue
            try:
                q = accounts_mod.fetch_quota(ctx.app.state.http, a)
                out.append({"id": a.id, "ok": q is not None})
            except Exception as e:
                out.append({"id": a.id, "ok": False, "error": str(e)[:200]})
        return {"results": out,
                "accounts": [x.public() for x in ctx.app.state.pool.accounts()]}

    @router.post("/api/accounts/{aid}/quota", dependencies=[Depends(admin_key)])
    def refresh_quota(aid: int):
        """Force-refresh one account's upstream quota snapshot."""
        a = ctx.app.state.pool.get(aid)
        if not a:
            raise HTTPException(404, "not found")
        q = accounts_mod.fetch_quota(ctx.app.state.http, a)
        if q is None:
            raise HTTPException(502, "no quota data (plan may not expose it)")
        return {"account": a.public(), "quota": q}

    @router.post("/api/accounts/import", dependencies=[Depends(admin_key)])
    def import_detected():
        detected = creds_mod.detect_all()
        added, skipped = [], []
        for c in detected:
            a, err = ctx.app.state.pool.add(
                c.api_key, name=c.source.split(":", 1)[0] if c.source else "auto",
                source=c.source, api_server_url=c.api_server_url,
                webapp=c.webapp_host, api_url=c.api_url)
            (added if a else skipped).append(
                {"source": c.source, "reason": err})
        return {"added": len(added), "skipped": skipped,
                "accounts": [a.public() for a in ctx.app.state.pool.accounts()]}

