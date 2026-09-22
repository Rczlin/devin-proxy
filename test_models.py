"""Functional test: remote model catalog sync, aliases, effort mapping,
admin endpoints. Run: python test_models.py"""
import gzip
import json
import os
import tempfile

os.environ["DEVIN_PROXY_DB"] = tempfile.mktemp(suffix=".db")

import httpx
from fastapi.testclient import TestClient

from devin_proxy import app as app_mod
from devin_proxy import models as M
from devin_proxy import proto, store, upstream


class FakeResp:
    def __init__(self, content, status=200):
        self.content, self.status_code = content, status

    def raise_for_status(self):
        if self.status_code != 200:
            raise httpx.HTTPStatusError("bad", request=None,
                                        response=httpx.Response(
                                            self.status_code))


def cfg(uid, label="", disabled=False, images=False, ctx=200000,
        thinking=False, credit=0.0, family="", max_out=0):
    c = proto.ClientModelConfig(
        label=label, model_uid=uid, disabled=disabled,
        supports_images=images, max_tokens=ctx, credit_cost=credit)
    mi = proto.ModelInfo(max_output_tokens=max_out, family=family)
    mi.model_features.supports_thinking = thinking
    c.model_info.CopyFrom(mi)
    return c


REMOTE_A = proto.GetCliModelConfigsResponse(client_model_configs=[
    cfg("claude-sonnet-5-medium", "Claude Sonnet 5 Medium", images=True,
        ctx=400000, thinking=True, credit=1.0, family="claude-sonnet-5",
        max_out=64000),
    cfg("claude-sonnet-5-high", "Claude Sonnet 5 High", images=True,
        ctx=400000, thinking=True, credit=1.5),
    cfg("claude-opus-5-high", "Claude Opus 5 High", ctx=400000,
        thinking=True, credit=3.0),
    cfg("swe-2-high", "SWE 2 High", thinking=True),
    cfg("kimi-k3-medium", "Kimi K3", ctx=256000),          # unknown family
    cfg("dead-model", "Dead", disabled=True),               # skipped
]).SerializeToString()

REMOTE_B = proto.GetCliModelConfigsResponse(client_model_configs=[
    cfg("claude-sonnet-5-medium", "Claude Sonnet 5 Medium", images=True,
        ctx=400000, thinking=True, credit=1.0),
    cfg("swe-2-high", "SWE 2 High", thinking=True),
    cfg("acct-b-only", "B Only Model"),
]).SerializeToString()


class FakeAcct:
    def __init__(self, i):
        self.id, self.disabled = i, False
        self.api_server_url = "https://server.test"
        self.token = f"tok-{i}"

    def display(self):
        return f"acct-{self.id}"


calls = []


def fake_fetch(client, base_url, api_key, timeout=20):
    # alternate payload per distinct token so two accounts union cleanly
    if api_key not in calls:
        calls.append(api_key)
    payload = REMOTE_A if calls.index(api_key) % 2 == 0 else REMOTE_B
    return [  # mimic upstream.fetch_model_configs output shape
        {"uid": c.model_uid, "label": c.label or None,
         "context": c.max_tokens or None,
         "credit": c.credit_cost or None,
         "images": bool(c.supports_images),
         "max_output": c.model_info.max_output_tokens or None,
         "family_slug": c.model_info.family or None,
         "thinking": (bool(c.model_info.model_features.supports_thinking)
                      if c.HasField("model_info")
                      and c.model_info.HasField("model_features") else None)}
        for c in proto.GetCliModelConfigsResponse.FromString(payload
        ).client_model_configs
        if c.model_uid and not c.disabled]


# ---- proto round-trip via a fake httpx client ----
class FakeHTTP:
    def __init__(self, payload):
        self.payload = payload

    def post(self, url, content=None, headers=None, timeout=None):
        assert url.endswith("/exa.api_server_pb.ApiServerService/"
                            "GetCliModelConfigs")
        req = proto.GetCliModelConfigsRequest.FromString(content)
        assert req.metadata.api_key
        return FakeResp(gzip.compress(self.payload))   # gzip w/o header


real_fetch = upstream.fetch_model_configs
got = real_fetch(FakeHTTP(REMOTE_A), "https://server.test", "tok-1")
uids_a = {m["uid"] for m in got}
assert "claude-sonnet-5-medium" in uids_a and "dead-model" not in uids_a
m = next(x for x in got if x["uid"] == "claude-sonnet-5-medium")
assert m["label"] == "Claude Sonnet 5 Medium" and m["context"] == 400000
assert m["images"] and m["thinking"] and m["max_output"] == 64000
assert m["family_slug"] == "claude-sonnet-5" and m["credit"] == 1.0
print("fetch_model_configs (gzipped proto) OK:", len(got), "models")

# ---- catalog merge across two accounts ----
upstream.fetch_model_configs = fake_fetch
try:
    r = M.refresh(httpx.Client(), [FakeAcct(1), FakeAcct(2)], force=True)
finally:
    upstream.fetch_model_configs = real_fetch
assert r["count"] == 6, r
ents = M.entries()
by_uid = {e["uid"]: e for e in ents}
assert by_uid["claude-sonnet-5-medium"]["remote_accounts"] == 2
assert by_uid["acct-b-only"]["remote_accounts"] == 1
assert by_uid["kimi-k3-medium"]["family"] == "kimi-k3"
assert by_uid["claude-sonnet-5-medium"]["family"] == "claude-sonnet-5"
assert by_uid["swe-2-high"]["effort"] == "high"
print("merged catalog:", [e["uid"] for e in ents])

# ---- resolve / aliases / effort ----
assert M.resolve("claude") == "claude-sonnet-5-medium"
assert M.resolve("swe") == "swe-2-high"
assert M.resolve("swe-2") == "swe-2-high"          # bare family
assert M.resolve("kimi-k3") == "kimi-k3-medium"    # remote-only family
assert M.resolve("brand-new-uid") == "brand-new-uid"   # pass-through
assert M.resolve(None) is None
assert M.apply_effort("claude-sonnet-5-medium", "high") \
    == "claude-sonnet-5-high"                       # variant in catalog
assert M.apply_effort("claude-sonnet-5-medium", "max") \
    == "claude-sonnet-5-medium"                     # not in catalog -> keep
assert M.default_uid() == "claude-sonnet-5-medium"
al = M.aliases()
assert al["opus"] == "claude-opus-5-high" and "default" in al
print("resolve/aliases OK:", al)

# ---- auto effort: global default + per-alias @effort ----
store.meta_set("default_effort", "")
assert M.auto_effort("claude-sonnet-5") is None       # unset -> no remap
assert M.auto_effort("claude-sonnet-5-high") is None  # explicit variant
store.meta_set("default_effort", "high")
assert M.auto_effort("claude-sonnet-5") == "high"     # family -> default
assert M.auto_effort("opus") == "high"                # builtin alias
assert M.auto_effort("claude-sonnet-5-low") is None   # pinned variant
store.meta_set("model_aliases", '{"fast": "claude-sonnet-5@low",'
                                ' "big": "claude-opus-5-max"}')
assert M.auto_effort("fast") == "low"                 # per-alias effort
assert M.auto_effort("big") is None                   # alias pins variant
assert M.resolve("fast") == "claude-sonnet-5-medium"  # family target
store.meta_set("default_effort", "")
store.meta_set("model_aliases", "{}")
print("auto_effort OK")

# ---- served_by (scheduling restriction) ----
allowed, known = M.served_by({"acct-b-only"})
assert allowed == {2} and known == {1, 2}
assert M.served_by({"never-seen"}) is None
print("served_by OK:", allowed, known)

# ---- persistence: fresh module state reads meta ----
snap = json.loads(store.meta_get("model_catalog_v1"))
assert len(snap["models"]) == 6
print("persisted snapshot OK")

# ---- URL source ----
class UrlHTTP:
    def get(self, url, timeout=None):
        return type("R", (), {"status_code": 200,
                              "raise_for_status": lambda s: None,
                              "json": lambda s: {"models": [
                                  {"uid": "url-model-1",
                                   "label": "URL Model",
                                   "context_window": 128000},
                                  "url-model-2"]}})()

store.meta_set("models_url", "https://example.com/models.json")
r = M.refresh(UrlHTTP(), [], force=True)
by_uid = {e["uid"]: e for e in M.entries()}
assert by_uid["url-model-1"]["url"] and \
    by_uid["url-model-1"]["context"] == 128000
assert by_uid["url-model-2"]["url"]
print("URL source OK")
store.meta_set("models_url", "")          # detach the URL source again

# ---- admin endpoints over TestClient ----
upstream.fetch_model_configs = fake_fetch   # startup refresh uses the fake
app = app_mod.create_app(api_key="sk-test")
tc = TestClient(app)
H = {"Authorization": "Bearer sk-test"}

r = tc.post("/admin/api/models/refresh", headers=H).json()
assert r["count"] >= 5, r
d = tc.get("/admin/api/models", headers=H).json()
assert d["models"] and d["families"] and d["sync"]["count"] >= 5
fam_labels = {f["label"] for f in d["families"]}
assert "Claude Sonnet 5" in fam_labels and "Kimi K3" in fam_labels
print("admin /api/models OK:", len(d["models"]), "models,",
      len(d["families"]), "families")

d = tc.get("/v1/models", headers=H).json()
ids = {m["id"] for m in d["data"]}
# collapsed: one entry per family, variants hidden behind reasoning.effort
assert "claude-sonnet-5" in ids and "claude-sonnet-5-medium" not in ids
assert "sonnet" in ids
one = next(m for m in d["data"] if m["id"] == "claude-sonnet-5")
assert one["owned_by"] == "Anthropic" and one["capabilities"]["thinking"]
assert one["context_window"] == 400000 and one["source"] == "remote"
assert "medium" in one["efforts"] and "high" in one["efforts"]
al = next(m for m in d["data"] if m["id"] == "sonnet")
assert al["alias_of"] == "claude-sonnet-5-medium"
print("/v1/models OK:", len(d["data"]), "entries")
# ?variants=1 exposes the full uid list
d = tc.get("/v1/models?variants=1", headers=H).json()
ids = {m["id"] for m in d["data"]}
assert "claude-sonnet-5-medium" in ids
print("/v1/models?variants=1 OK:", len(d["data"]), "entries")

# settings: default model + default effort + user alias
r = tc.patch("/admin/api/models/settings", headers=H, json={
    "default_model": "swe-2-high", "default_effort": "low",
    "aliases": "mine=swe-2@high\n"}).json()
assert r["default_model"] == "swe-2-high" and r["default_effort"] == "low"
assert r["aliases"]["mine"] == "swe-2-high"
assert M.resolve("mine") == "swe-2-high"
assert M.alias_effort("mine") == "high"
r = tc.patch("/admin/api/models/settings", headers=H,
             json={"default_model": "no-such-model-xyz"})
assert r.status_code == 400
r = tc.patch("/admin/api/models/settings", headers=H,
             json={"default_effort": "bogus"})
assert r.status_code == 400
r = tc.patch("/admin/api/models/settings", headers=H,
             json={"aliases": "bad=swe-2@bogus"})
assert r.status_code == 400
tc.patch("/admin/api/models/settings", headers=H,
         json={"default_model": "", "default_effort": "", "aliases": ""})
print("settings OK")

# per-key model allowlist still filters /v1/models
k = tc.post("/admin/api/keys", headers=H,
            json={"name": "t", "models": "swe-2-high"}).json()["key"]
d = tc.get("/v1/models",
           headers={"Authorization": f"Bearer {k}"}).json()
assert {m["id"] for m in d["data"]} == {"swe-2"}
d = tc.get("/v1/models?variants=1",
           headers={"Authorization": f"Bearer {k}"}).json()
assert {m["id"] for m in d["data"]} == {"swe-2-high"}
print("key allowlist filter OK")

print("ALL MODEL TESTS PASSED")
