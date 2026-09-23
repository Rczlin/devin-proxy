import os, tempfile
os.environ["DEVIN_PROXY_DB"] = tempfile.mktemp(suffix=".db")
from fastapi.testclient import TestClient
from devin_proxy.app import create_app

app = create_app(api_key="sk-test-master")
c = TestClient(app)

# --- unauthenticated ---
r = c.get("/admin/api/overview")
assert r.status_code == 401, r.status_code
print("unauth overview -> 401 OK")

# security headers present on every /admin response
r = c.get("/admin")
for h in ("x-content-type-options", "x-frame-options",
          "referrer-policy", "cache-control"):
    assert h in r.headers, (h, dict(r.headers))
assert r.headers["x-frame-options"] == "DENY"
print("security headers OK")

# --- login flow ---
r = c.post("/admin/api/login", json={"key": "wrong"})
assert r.status_code == 401
r = c.post("/admin/api/login", json={"key": "sk-test-master"})
assert r.status_code == 200
cookie = r.cookies.get("dp_admin")
assert cookie
r = c.get("/admin/api/overview", cookies={"dp_admin": cookie})
assert r.status_code == 200 and "pool" in r.json()
print("login + session OK")

# bearer master key works too
r = c.get("/admin/api/overview",
          headers={"Authorization": "Bearer sk-test-master"})
assert r.status_code == 200
print("bearer auth OK")

# --- rate limit: 5 fails -> 429 ---
c2 = TestClient(app)   # same app, same IP (testclient)
for i in range(6):
    r = c2.post("/admin/api/login", json={"key": "bad"})
    if i < 4:
        assert r.status_code == 401, (i, r.status_code)
assert r.status_code == 429, r.status_code
assert "retry" in r.json()["detail"]
assert r.headers.get("retry-after")
# correct key is also blocked while limited (attacker can't bypass)
r = c2.post("/admin/api/login", json={"key": "sk-test-master"})
assert r.status_code == 429
print("login rate-limit OK")

# --- keys CRUD ---
r = c.post("/admin/api/keys", json={"name": "ci", "max_concurrent": 2},
           cookies={"dp_admin": cookie})
assert r.status_code == 200, r.text
token = r.json()["key"]
assert token.startswith("sk-")
r = c.get("/admin/api/keys", cookies={"dp_admin": cookie})
kid = [k["id"] for k in r.json()["keys"] if k["name"] == "ci"][0]
r = c.patch(f"/admin/api/keys/{kid}", json={"disabled": True},
            cookies={"dp_admin": cookie})
assert r.status_code == 200
r = c.delete(f"/admin/api/keys/{kid}", cookies={"dp_admin": cookie})
assert r.status_code == 200
print("keys CRUD OK")

# --- models settings validation ---
r = c.patch("/admin/api/models/settings",
            json={"default_effort": "bogus"},
            cookies={"dp_admin": cookie})
assert r.status_code == 400
r = c.patch("/admin/api/models/settings",
            json={"default_effort": "high"},
            cookies={"dp_admin": cookie})
assert r.status_code == 200
print("model settings validation OK")

# --- status + ping shape ---
r = c.get("/admin/api/status", cookies={"dp_admin": cookie})
d = r.json()
assert "uptime_s" in d and "db" in d and "disk" in d
print("status OK")

# --- export bundle is a valid streaming zip ---
r = c.get("/admin/api/export?limit=10", cookies={"dp_admin": cookie})
assert r.status_code == 200
assert r.headers["content-type"] == "application/zip"
import zipfile, io, json
z = zipfile.ZipFile(io.BytesIO(r.content))
names = z.namelist()
for n in ("README.txt", "env.json", "requests.jsonl", "models.json",
          "stats.json", "decode_capture.py"):
    assert n in names, n
env = json.loads(z.read("env.json"))
# master key must never appear anywhere in the bundle
blob = r.content.decode("utf-8", "replace")
assert "sk-test-master" not in blob
print("export bundle OK:", len(names), "entries")

print("ALL ADMIN TESTS PASSED")
