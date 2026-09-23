"""Model-catalog admin routes: list/hide/aliases/settings/refresh."""
import json

from fastapi import Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

from .. import models as models_mod
from .. import store


def register(router, ctx, admin_key):
    @router.get("/api/models", dependencies=[Depends(admin_key)])
    def models():
        models_mod.maybe_refresh(ctx.app.state.http, ctx.app.state.pool)
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
        uid = uid.strip()
        if not uid:
            raise HTTPException(400, "empty model uid")
        hid = models_mod.hidden_models() | {uid}
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
        accs = [a for a in ctx.app.state.pool.accounts() if not a.disabled]
        return models_mod.refresh(ctx.app.state.http, accs, force=True)

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
                # starts like JSON but won't parse — reject rather than fall
                # through to line-mode (which would silently wipe aliases)
                raise HTTPException(400, "invalid aliases JSON")
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
                # alias names become request-facing model names — keep them
                # url/token-safe so clients can actually send them
                if not k.replace("-", "").replace("_", "").replace(".", "").isalnum():
                    raise HTTPException(
                        400, f"{k}: alias may only contain letters, digits, '-', '_', '.'")
                if not v.split("@")[0].strip():
                    raise HTTPException(400, f"{k}: empty target model")
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

