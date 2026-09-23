"""Verify error-scoping: provider-side transient errors must not cool the
account down. Stubs httpx/proto so the pure logic runs without deps."""
import json
import sys
import time
import types

sys.modules["httpx"] = types.ModuleType("httpx")
import devin_proxy
_proto = types.ModuleType("devin_proxy.proto")
sys.modules["devin_proxy.proto"] = _proto
devin_proxy.proto = _proto

from devin_proxy import accounts, upstream  # noqa: E402

accounts.Account.persist = lambda self: None   # no db in this test

PASS = []


def check(name, cond):
    PASS.append((name, cond))
    print(("PASS " if cond else "FAIL ") + name)


def scope_of(trailer_obj):
    payload = json.dumps(trailer_obj).encode()
    msg, code = upstream._trailer_error(payload)
    trailer_txt = payload.decode()
    return upstream._err_scope(code, trailer_txt), msg, code


# ---- the exact reported error ----
t = {"error": {"code": "unimplemented",
               "message": "The third-party model provider is experiencing "
                          "issues and is currently not available. Please "
                          "try this model again later. (trace ID: abc)"}}
scope, msg, code = scope_of(t)
check("provider-down trailer -> transient", scope == "transient")
check("trailer code extracted", code == "unimplemented")
err = {"kind": "upstream_error", "message": msg, "code": code,
       "soft": scope == "soft", "transient": scope == "transient"}
check("provider-down err -> not hard", not accounts.is_hard_failure(err))

# ---- scope table ----
cases = [
    ("unavailable", "backend down", "transient"),
    ("internal", "internal error", "transient"),
    ("deadline_exceeded", "timed out", "transient"),
    ("unknown", "something broke", "transient"),
    ("resource_exhausted", "quota exceeded", "account"),
    ("unauthenticated", "bad token", "account"),
    ("permission_denied", "no access", "account"),
    ("invalid_argument", "bad request field", "soft"),
    ("unimplemented", "model X is not implemented", "soft"),
    ("invalid_argument", "context length exceeded", "soft"),
    (None, "weird unrecognized failure", "account"),
    (None, "rate limit exceeded, slow down", "account"),
    (None, "quota exceeded, please try again later", "account"),
    (None, "service temporarily unavailable", "transient"),
]
for code_s, m, want in cases:
    scope, _, _ = scope_of({"error": {"code": code_s, "message": m}})
    check(f"scope({code_s},{m!r}) = {want}", scope == want)

# ---- is_hard_failure ----
check("http 503 transient -> not hard",
      not accounts.is_hard_failure(
          {"kind": "http_error", "http_error": 503, "transient": True}))
check("http 429 -> hard",
      accounts.is_hard_failure(
          {"kind": "http_error", "http_error": 429}))
check("http 401 -> hard",
      accounts.is_hard_failure(
          {"kind": "http_error", "http_error": 401}))
check("http 500 no flag -> hard",
      accounts.is_hard_failure(
          {"kind": "http_error", "http_error": 500}))
check("non-dict exception -> hard",
      accounts.is_hard_failure("connection reset"))
check("plain upstream_error (no flags) -> hard",
      accounts.is_hard_failure(
          {"kind": "upstream_error", "message": "mystery"}))
check("soft -> not hard",
      not accounts.is_hard_failure(
          {"kind": "upstream_error", "soft": True}))

# ---- mark_fail behavior ----
def fresh_acct():
    a = accounts.Account()
    a.id, a.name, a.email = 1, "t", "t@x"
    a.token, a.api_server_url = "k", "https://x"
    a.webapp, a.api_url, a.source, a.plan = "", "", "", ""
    a.created = a.last_used = a.last_ok = 0
    a.disabled = False
    a.fail_count = a.consecutive_fails = a.req_count = 0
    a.cooldown_until = 0
    a.last_error = None
    a.max_concurrent = 0
    a.models, a.in_flight = set(), 0
    return a

pool = accounts.Pool.__new__(accounts.Pool)   # skip __init__ (db/thread)

a = fresh_acct()
pool.mark_fail(a, err)                        # transient provider error
check("transient: fail_count++", a.fail_count == 1)
check("transient: no cooldown", a.cooldown_until == 0)
check("transient: no losing streak", a.consecutive_fails == 0)
check("transient: last_error recorded", bool(a.last_error))

pool.mark_fail(a, err)
check("transient x2: still no cooldown", a.cooldown_until == 0)

hard = {"kind": "upstream_error", "message": "weird boom",
        "soft": False, "transient": False}
pool.mark_fail(a, hard)
check("hard: consecutive_fails=1", a.consecutive_fails == 1)
check("hard: cooldown ~30s",
      25 < a.cooldown_until - time.time() <= 30)
pool.mark_fail(a, hard)
check("hard x2: backoff doubles",
      55 < a.cooldown_until - time.time() <= 60)

print()
n_fail = sum(1 for _, c in PASS if not c)
print(f"{len(PASS) - n_fail}/{len(PASS)} passed")
sys.exit(1 if n_fail else 0)
