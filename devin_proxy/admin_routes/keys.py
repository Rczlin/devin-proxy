"""API-key admin routes: list/create/patch/delete."""
from typing import Optional

from fastapi import Depends, HTTPException
from pydantic import BaseModel

from .. import store


def register(router, ctx, admin_key):
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
        if body.max_concurrent < 0:
            raise HTTPException(400, "max_concurrent must be >= 0")
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
            if body.max_concurrent < 0:
                raise HTTPException(400, "max_concurrent must be >= 0")
            fields["max_concurrent"] = body.max_concurrent
        if fields and not store.update_key(kid, **fields):
            raise HTTPException(404, "key not found")
        return {"ok": True}

    @router.delete("/api/keys/{kid}", dependencies=[Depends(admin_key)])
    def del_key(kid: int):
        if not store.delete_key(kid):
            raise HTTPException(404, "key not found")
        return {"ok": True}

