import os, tempfile, json
os.environ["DEVIN_PROXY_DB"] = tempfile.mktemp(suffix=".db")
from fastapi.testclient import TestClient
from devin_proxy.app import create_app

app = create_app(api_key="sk-test-master")
c = TestClient(app)

# --- unauthenticated ---
r = c.get("/admin/api/overview")
assert r.status_code == 401, r.status_code
# spa assets are behind auth too
assert c.get("/admin/static/js/util.js").status_code == 401
assert c.get("/admin/static/admin.css").status_code == 401
print("unauth overview + assets -> 401 OK")

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
# authed asset fetch works
for n in ("admin.css", "js/util.js", "js/dash.js", "js/conf.js"):
    r2 = c.get("/admin/static/" + n, cookies={"dp_admin": cookie})
    assert r2.status_code == 200 and len(r2.content) > 500, n
# traversal blocked
assert c.get("/admin/static/js/../admin.py",
             cookies={"dp_admin": cookie}).status_code == 404
print("login + session + assets OK")

# bearer master key works too
r = c.get("/admin/api/overview",
          headers={"Authorization": "Bearer sk-test-master"})
assert r.status_code == 200
print("bearer auth OK")

# master key in the URL query must NOT authenticate (leak vector).
# use a fresh client so the session cookie doesn't mask the result.
c_fresh = TestClient(app)
r = c_fresh.get("/admin/api/overview?key=sk-test-master")
assert r.status_code == 401, r.status_code
print("?key= rejected OK")

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

# --- request log: seed, filtered prune, csv/jsonl export ---
from devin_proxy import store as _st
for i in range(6):
    _st.log_request("m-a" if i % 2 else "m-b", "m-a", 0,
                    0 if i < 2 else 1, 200,
                    "boom" if i < 2 else None,
                    0, 0, 5, None, "test", "ci", "[]",
                    account="acct-1")
cookie_hdr = {"dp_admin": cookie}
r = c.get("/admin/api/requests?ok=0", cookies=cookie_hdr)
assert r.json()["total"] == 2, r.json()["total"]

# export CSV of just the failures
r = c.get("/admin/api/requests/export?fmt=csv&ok=0", cookies=cookie_hdr)
assert r.status_code == 200
assert "text/csv" in r.headers["content-type"]
lines = [l for l in r.text.strip().splitlines()]
assert len(lines) == 3          # header + 2 rows
print("csv export OK")

# jsonl export of everything
r = c.get("/admin/api/requests/export?fmt=jsonl", cookies=cookie_hdr)
assert "x-ndjson" in r.headers["content-type"]
rows = [json.loads(l) for l in r.text.strip().splitlines()]
assert len(rows) == 6
print("jsonl export OK")

# filtered prune: delete only the failures
r = c.post("/admin/api/requests/clear?ok=0", cookies=cookie_hdr)
assert r.json()["deleted"] == 2, r.json()
r = c.get("/admin/api/requests", cookies=cookie_hdr)
assert r.json()["total"] == 4
# unfiltered clear wipes the rest
r = c.post("/admin/api/requests/clear", cookies=cookie_hdr)
assert r.json()["deleted"] == "all"
r = c.get("/admin/api/requests", cookies=cookie_hdr)
assert r.json()["total"] == 0
print("filtered prune + clear OK")

# --- error rollup ---
for i in range(3):
    _st.log_request("m", "m", 0, 0, 500, f"timeout after {i}.5s",
                    0, 0, 1, None, "t", "ci", "[]")
r = c.get("/admin/api/requests/errors?hours=24", cookies=cookie_hdr)
errs = r.json()["errors"]
agg = [e for e in errs if e["sig"].startswith("timeout after")]
assert agg and agg[0]["n"] >= 3 and "#" in agg[0]["sig"], errs
print("error rollup OK")

# --- keyset pagination ---
for i in range(5):
    _st.log_request("pg", "pg", 0, 1, 200, None, 0, 0, 1, None, "t", "ci", "[]")
r = c.get("/admin/api/requests?limit=2", cookies=cookie_hdr)
d = r.json()
assert len(d["items"]) == 2 and d["next_cursor"] == d["items"][-1]["id"]
page1_ids = [x["id"] for x in d["items"]]
r = c.get(f"/admin/api/requests?limit=2&before={d['next_cursor']}",
          cookies=cookie_hdr)
d2 = r.json()
assert [x["id"] for x in d2["items"]] != page1_ids
assert all(x["id"] < page1_ids[-1] for x in d2["items"])
print("keyset pagination OK")

# --- input validation edges (must not 500 or silently misfire) ---
H = {"Authorization": "Bearer sk-test-master"}
assert c.get("/admin/api/overview?hours=-5", headers=H).status_code == 400
assert c.get("/admin/api/overview?hours=99999999", headers=H).status_code == 400
assert c.get("/admin/api/overview?hours=24", headers=H).status_code == 200
assert c.get("/admin/api/requests/export?fmt=xml", headers=H).status_code == 400
assert c.get("/admin/api/requests/export?fmt=csv", headers=H).status_code == 200
assert c.post("/admin/api/accounts/bulk",
              json={"ids": [{}], "disabled": True},
              headers=H).status_code == 422
assert c.post("/admin/api/keys",
              json={"name": "bad-conc", "max_concurrent": -1},
              headers=H).status_code == 400
assert c.post("/admin/api/requests/clear?older_than_hours=-1",
              headers=H).status_code == 400
print("input validation OK")

# --- bulk account enable/disable ---
accs = c.get("/admin/api/accounts", headers=H).json()["accounts"]
if accs:
    ids = [a["id"] for a in accs]
    r = c.patch(f"/admin/api/accounts/{ids[0]}",
                json={"max_concurrent": -1}, headers=H)
    assert r.status_code == 400
    r = c.post("/admin/api/accounts/bulk",
               json={"ids": ids, "disabled": True}, headers=H)
    assert r.json()["updated"] == len(ids)
    assert all(a["disabled"] for a in
               c.get("/admin/api/accounts", headers=H).json()["accounts"])
    c.post("/admin/api/accounts/bulk",
           json={"ids": ids, "disabled": False}, headers=H)
    assert not any(a["disabled"] for a in
                   c.get("/admin/api/accounts", headers=H).json()["accounts"])
    print("bulk account toggle OK")

# --- sliding session renewal ---
import hmac as _h, hashlib as _hh, time as _t
exp = int(_t.time()) + 86400      # 1 day left < half of 7d TTL
sig = _h.new(b"sk-test-master", str(exp).encode(), _hh.sha256).hexdigest()
r = c.get("/admin/api/overview", cookies={"dp_admin": f"{exp}.{sig}"})
assert "dp_admin" in r.headers.get("set-cookie", ""), "stale cookie not renewed"
exp2 = int(_t.time()) + 6 * 86400   # fresh — no renewal
sig2 = _h.new(b"sk-test-master", str(exp2).encode(), _hh.sha256).hexdigest()
r = c.get("/admin/api/overview", cookies={"dp_admin": f"{exp2}.{sig2}"})
assert "dp_admin" not in r.headers.get("set-cookie", "")
print("sliding session renewal OK")

# --- status + ping shape ---
r = c.get("/admin/api/status", cookies={"dp_admin": cookie})
d = r.json()
assert "uptime_s" in d and "db" in d and "disk" in d
print("status OK")

# --- live ws feed ---
# unauthenticated handshake is refused before accept
try:
    with c_fresh.websocket_connect("/admin/api/ws") as ws:
        ws.receive_text()
    assert False, "unauth ws connected"
except Exception:
    pass
# a valid cookie sent from a foreign Origin is refused too (CSWSH guard)
try:
    with c.websocket_connect(
            "/admin/api/ws",
            headers={"cookie": f"dp_admin={cookie}",
                     "origin": "https://evil.example"}) as ws:
        ws.receive_text()
    assert False, "foreign-origin ws connected"
except Exception:
    pass
# proxied handshake: proxy rewrote Host, real client host survives in
# X-Forwarded-Host -> still accepted
with c.websocket_connect(
        "/admin/api/ws",
        headers={"cookie": f"dp_admin={cookie}",
                 "host": "internal-upstream:8317",
                 "x-forwarded-host": "public.example.com",
                 "origin": "https://public.example.com"}) as ws:
    assert json.loads(ws.receive_text())["type"] == "live"
# ...but a Host/XFH that doesn't match the Origin is still refused
try:
    with c.websocket_connect(
            "/admin/api/ws",
            headers={"cookie": f"dp_admin={cookie}",
                     "host": "internal-upstream:8317",
                     "x-forwarded-host": "public.example.com",
                     "origin": "https://attacker.example"}) as ws:
        ws.receive_text()
    assert False, "mismatched-origin ws connected"
except Exception:
    pass
# same-origin session -> live frame with pool/in_flight
with c.websocket_connect(
        "/admin/api/ws",
        headers={"cookie": f"dp_admin={cookie}",
                 "origin": "http://testserver"}) as ws:
    m = json.loads(ws.receive_text())
    assert m["type"] == "live" and "pool" in m, m
    assert "in_flight" in m["pool"] and "accounts" in m["pool"]
    assert "data_v" in m and "uptime_s" in m
    # a pool mutation pushes a fresh frame immediately (no polling)
    pool = app.state.pool
    if pool.accounts():
        a = pool.accounts()[0]
        a.in_flight += 1
        pool._notify()
        m2 = json.loads(ws.receive_text())
        assert m2["pool"]["in_flight"] == m["pool"]["in_flight"] + 1, m2
        a.in_flight -= 1
# bearer master key authenticates the ws handshake as well
with c.websocket_connect(
        "/admin/api/ws",
        headers={"authorization": "Bearer sk-test-master"}) as ws:
    assert json.loads(ws.receive_text())["type"] == "live"
print("live ws feed OK")

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
