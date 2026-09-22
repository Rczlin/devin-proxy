"""Functional test: mock upstream connect-RPC stream, verify logging +
truncation detection + buffered retry. Run with repo venv python."""
import gzip
import json
import os
import sys
import tempfile

os.environ["DEVIN_PROXY_DB"] = tempfile.mktemp(suffix=".db")

import httpx
from fastapi.testclient import TestClient

from devin_proxy import app as app_mod
from devin_proxy import proto, store, upstream

END = 0x02


def frame(body, flags=0, compress=True):
    payload = gzip.compress(body) if compress else body
    if compress:
        flags |= 0x01
    return bytes([flags]) + len(payload).to_bytes(4, "big") + payload


def msg(**kw):
    return proto.GetChatMessageResponse(**kw).SerializeToString()


def trailer(err=None):
    d = {"error": err} if err else {}
    return frame(json.dumps(d).encode(), END, compress=False)


SCENARIO = {"bytes": b"", "calls": 0}


def handler(req):
    if req.url.path.endswith("GetUserJwt"):
        return httpx.Response(
            200, content=proto.GetUserJwtResponse(jwt="x.y.z").SerializeToString(),
            headers={"Content-Type": "application/proto"})
    if req.url.path.endswith("GetChatMessage"):
        SCENARIO["calls"] += 1
        body = SCENARIO["bytes"]
        return httpx.Response(200, content=iter([body]),
                              headers={"Content-Type": "application/connect+proto"})
    return httpx.Response(404)


def fresh_client():
    app = app_mod.create_app(api_key="k")
    app.state.http = httpx.Client(transport=httpx.MockTransport(handler))
    acct, err = app.state.pool.add("tok-" + os.urandom(4).hex(), name="t",
                                   manual=True)
    assert acct, err
    return TestClient(app), app


def sse_lines(r):
    return [ln[6:] for ln in r.text.split("\n") if ln.startswith("data: ")]


def last_req():
    items, _ = store.list_requests(1, 0)
    return store.get_request(items[0]["id"])


def reset():
    store.clear_requests()
    SCENARIO["calls"] = 0


# --- 1. clean stream ------------------------------------------------------
c, app = fresh_client()
reset()
SCENARIO["bytes"] = (frame(msg(message_id="m1", delta_text="hi "))
                     + frame(msg(delta_text="there", stop_reason=5))
                     + trailer())
r = c.post("/v1/chat/completions", json={"model": "claude", "stream": True,
           "messages": [{"role": "user", "content": "hi"}]},
           headers={"Authorization": "Bearer k"})
lines = sse_lines(r)
assert r.status_code == 200, r.text
assert '"finish_reason":"stop"' in lines[-2] or '"stop"' in lines[-2], lines[-2:]
row = last_req()
assert row["ok"] == 1 and row["events_json"] and row["sse_json"], row.keys()
ev = json.loads(row["events_json"])
assert any(e["t"] == "end" and e.get("stop_reason") for e in ev), ev
print("1. clean stream        OK  flags=%s evs=%d" % (row["flags"], len(ev)))

# --- 2. truncated stream (no END_STREAM frame) ----------------------------
SCENARIO["bytes"] = (frame(msg(message_id="m2", delta_text="I will continue "))
                     + frame(msg(delta_text="with a tool call")))
reset()
r = c.post("/v1/chat/completions", json={"model": "claude", "stream": True,
           "messages": [{"role": "user", "content": "hi"}]},
           headers={"Authorization": "Bearer k"})
lines = sse_lines(r)
err = [json.loads(x) for x in lines if '"error"' in x]
assert err and err[0]["error"]["code"] == "truncated", lines
assert not any('"finish_reason":"stop"' in x for x in lines), \
    "fake stop emitted before error!"
row = last_req()
assert row["ok"] == 0 and "truncated" in (row["flags"] or ""), dict(row)
print("2. truncated stream    OK  error surfaced, flags=%s" % row["flags"])

# --- 3. non-stream truncated -> retry same account succeeds ---------------
calls = {"n": 0}
def flaky(req):
    if req.url.path.endswith("GetUserJwt"):
        return httpx.Response(
            200, content=proto.GetUserJwtResponse(jwt="x.y.z").SerializeToString())
    calls["n"] += 1
    if calls["n"] == 1:
        # first attempt: partial content then TCP drop (no trailer)
        return httpx.Response(200, content=iter([frame(msg(delta_text="partial "))]))
    return httpx.Response(200, content=iter([
        frame(msg(message_id="m3", delta_text="full answer", stop_reason=5)),
        trailer()]))
app.state.http = httpx.Client(transport=httpx.MockTransport(flaky))
reset()
r = c.post("/v1/chat/completions",
           json={"model": "claude", "messages": [{"role": "user", "content": "hi"}]},
           headers={"Authorization": "Bearer k"})
j = r.json()
assert j["choices"][0]["message"]["content"] == "full answer", j
assert calls["n"] == 2, calls
row = last_req()
assert row["ok"] == 1 and "truncated" in row["flags"] and "retried" in row["flags"], row["flags"]
print("3. buffered retry      OK  2 upstream calls, flags=%s" % row["flags"])

# --- 4. trailer error -> upstream_error with detail ------------------------
SCENARIO["bytes"] = frame(msg(delta_text="x")) + trailer(
    {"code": "internal", "message": "backend exploded"})
app.state.http = httpx.Client(transport=httpx.MockTransport(handler))
reset()
r = c.post("/v1/chat/completions", json={"model": "claude", "stream": True,
           "messages": [{"role": "user", "content": "hi"}]},
           headers={"Authorization": "Bearer k"})
err = [json.loads(x) for x in sse_lines(r) if '"error"' in x]
assert err and "backend exploded" in err[0]["error"]["message"], sse_lines(r)
row = last_req()
ev = json.loads(row["events_json"])
assert any(e["t"] == "upstream_err" and "backend exploded"
           in json.dumps(e) for e in ev), ev
print("4. trailer error       OK  detail logged")

# --- 5. no stop_reason -> flag ---------------------------------------------
SCENARIO["bytes"] = frame(msg(delta_text="no stop here")) + trailer()
reset()
r = c.post("/v1/chat/completions", json={"model": "claude", "stream": True,
           "messages": [{"role": "user", "content": "hi"}]},
           headers={"Authorization": "Bearer k"})
row = last_req()
assert "no_stop_reason" in (row["flags"] or ""), row["flags"]
print("5. no_stop_reason      OK  flags=%s" % row["flags"])

# --- 6. http error ----------------------------------------------------------
def http500(req):
    if req.url.path.endswith("GetUserJwt"):
        return httpx.Response(
            200, content=proto.GetUserJwtResponse(jwt="x.y.z").SerializeToString())
    return httpx.Response(503, content=b"upstream unavailable")
app.state.http = httpx.Client(transport=httpx.MockTransport(http500))
reset()
r = c.post("/v1/chat/completions",
           json={"model": "claude", "messages": [{"role": "user", "content": "hi"}]},
           headers={"Authorization": "Bearer k"})
assert r.status_code == 503, r.status_code
row = last_req()
assert row["ok"] == 0 and "unavailable" in (row["error"] or "")
print("6. http error          OK")

print("\nAll tests passed.")
