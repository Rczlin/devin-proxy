"""OAuth PKCE login-flow routes."""
from fastapi import Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

from .. import accounts as accounts_mod


def register(router, ctx, admin_key):
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
                ctx.app.state.http, fid, body.code, ctx.app.state.pool)
        except Exception as e:
            raise HTTPException(400, str(e)[:400])
        if body.label:
            acct.name = body.label.strip() or acct.name
            acct.persist()
        return {"account": acct.public()}

    @router.delete("/api/oauth/{fid}", dependencies=[Depends(admin_key)])
    def oauth_cancel(fid: str):
        return {"ok": accounts_mod.cancel_flow(fid)}

