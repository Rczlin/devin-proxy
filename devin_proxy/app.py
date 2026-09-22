"""FastAPI app: OpenAI /v1/chat/completions + /v1/responses -> Devin Cascade."""
import hashlib
import json
import os
import secrets
import threading
import time
import uuid

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from . import accounts as accounts_mod
from . import creds as creds_mod
from . import store
from . import upstream

MODEL_ALIASES = {
    # friendly name -> wire model_uid
    "claude": "claude-sonnet-5-medium",
    "sonnet": "claude-sonnet-5-medium",
    "opus": "claude-opus-5-high",
    "gemini": "gemini-3-8-flash-medium",
    "gpt": "gpt-5-6-sol-medium",
    "swe": "swe-2-high",
    "default": "claude-sonnet-5-medium",
    "auto": "claude-sonnet-5-medium",
}

KNOWN_UIDS = [
    "claude-opus-5-low", "claude-opus-5-medium", "claude-opus-5-high",
    "claude-opus-5-xhigh", "claude-opus-5-max",
    "claude-sonnet-5-low", "claude-sonnet-5-medium", "claude-sonnet-5-high",
    "claude-sonnet-5-xhigh", "claude-sonnet-5-max",
    "claude-fable-5-1-low", "claude-fable-5-1-medium", "claude-fable-5-1-high",
    "gpt-5-6-sol-none", "gpt-5-6-sol-low", "gpt-5-6-sol-medium",
    "gpt-5-6-sol-high", "gpt-5-6-sol-xhigh", "gpt-5-6-sol-max",
    "gpt-5-6-luna-none", "gpt-5-6-luna-low", "gpt-5-6-luna-medium",
    "gemini-3-8-flash-low", "gemini-3-8-flash-medium", "gemini-3-8-flash-high",
    "swe-2-high", "swe-1-7-medium", "swe-1-7-lightning-medium",
]

_EFFORT_SUFFIX = {"minimal": "none", "low": "low", "medium": "medium",
                  "high": "high"}
_UID_SUFFIXES = {"none", "low", "medium", "high", "xhigh", "max"}

# Upstream read timeout: Devin's agent stream can pause for a long time while
# the server runs tools. 300s was too short and looked like a silent 断流.
_READ_TIMEOUT = float(os.environ.get("DEVIN_PROXY_READ_TIMEOUT", "1800"))
# Per-blob char caps for the per-request debug record (request body /
# upstream event timeline / outbound SSE frames).
_LOG_CAP = int(os.environ.get("DEVIN_PROXY_LOG_CAP", "200000"))
_EV_CAP = 12000          # one event / one SSE payload
_REQ_CAP = 60000         # raw request body

try:
    import h2  # noqa: F401
    _HTTP2 = True
except Exception:
    _HTTP2 = False


class ReqLog:
    """Per-request debug recorder: request body, upstream event timeline and
    every SSE payload sent downstream. Attached to request.state and persisted
    into the requests row by _record. Never raises."""

    def __init__(self, body=None):
        self.t0 = time.perf_counter()
        self.events, self.sse, self.flags = [], [], set()
        self.request_json = None
        self._ev_sz = self._sse_sz = 0
        self._ev_full = self._sse_full = False
        if body is not None:
            try:
                self.request_json = json.dumps(
                    body, ensure_ascii=False, default=str)[:_REQ_CAP]
            except Exception:
                pass

    def _ms(self):
        return int((time.perf_counter() - self.t0) * 1000)

    def ev(self, kind, **data):
        """Append an upstream/pipeline event to the timeline."""
        try:
            if self._ev_full:
                return
            e = {"i": len(self.events) + 1, "t": kind, "ms": self._ms()}
            e.update(data)
            s = json.dumps(e, ensure_ascii=False, default=str)
            if len(s) > _EV_CAP:
                for k, v in list(e.items()):
                    vs = json.dumps(v, ensure_ascii=False, default=str)
                    if len(vs) > 4000:
                        e[k] = vs[:4000] + "…"
                e["data_truncated"] = True
                s = json.dumps(e, ensure_ascii=False, default=str)
            if self._ev_sz + len(s) > _LOG_CAP:
                self.events.append({"i": len(self.events) + 1, "t": "log_cap",
                                    "ms": self._ms(),
                                    "note": "event log size cap reached"})
                self._ev_full = True
                return
            self.events.append(e)
            self._ev_sz += len(s)
        except Exception:
            pass

    def out(self, payload):
        """Record one outbound SSE frame (or the final response body).
        Returns the payload so `yield rl.out(s)` stays transparent."""
        try:
            if self._sse_full:
                return payload
            s = payload if isinstance(payload, str) else json.dumps(
                payload, ensure_ascii=False, default=str)
            s = s.strip()
            if s.startswith("data:"):
                s = s[5:].strip()
            if len(s) > _EV_CAP:
                s = s[:_EV_CAP] + "…"
            if self._sse_sz + len(s) > _LOG_CAP:
                self.sse.append("… output log size cap reached …")
                self._sse_full = True
                return payload
            self.sse.append(s)
            self._sse_sz += len(s)
        except Exception:
            pass
        return payload

    def flag(self, f):
        try:
            self.flags.add(f)
        except Exception:
            pass

    def _dump(self, items):
        if not items:
            return None
        try:
            return json.dumps(items, ensure_ascii=False, default=str)
        except Exception:
            return None

    def events_dump(self):
        return self._dump(self.events)

    def sse_dump(self):
        return self._dump(self.sse)

    def flags_str(self):
        return ",".join(sorted(self.flags)) or None


def reqlog_for(request, body=None):
    """Get (or lazily create) the ReqLog for this request."""
    rl = getattr(request.state, "reqlog", None)
    if rl is None:
        rl = ReqLog(body)
        request.state.reqlog = rl
        try:
            rl.ev("request", ua=(request.headers.get("user-agent") or "")[:160],
                  path=str(request.url.path))
        except Exception:
            pass
    return rl


def _msg_summary(ev):
    """GetChatMessageResponse -> compact dict for the event timeline."""
    d = {}
    if ev.message_id:
        d["mid"] = ev.message_id
    if ev.delta_text:
        d["text"] = ev.delta_text
    if ev.delta_thinking:
        d["think"] = ev.delta_thinking
    if len(ev.delta_tool_calls):
        d["tool_calls"] = [{"id": tc.id, "name": tc.name,
                            "arguments": tc.arguments}
                           for tc in ev.delta_tool_calls]
    if ev.stop_reason:
        d["stop"] = int(ev.stop_reason)
    if ev.HasField("usage"):
        d["usage"] = upstream.extract_usage(ev)
    if ev.thinking_signature:
        d["think_sig"] = ev.thinking_signature[:200]
    return d


class ToolAgg:
    """Aggregates streamed upstream tool-call deltas into complete calls."""

    def __init__(self):
        self.calls, self.order, self.active = {}, [], None

    def feed(self, tc):
        cid = tc.id or self.active
        if not cid:
            return None
        self.active = cid
        if cid not in self.calls:
            self.calls[cid] = {"id": cid, "name": "", "args": ""}
            self.order.append(cid)
        agg = self.calls[cid]
        new_name = bool(tc.name) and not agg["name"]
        if tc.name:
            agg["name"] = tc.name
        delta = None
        if tc.arguments:
            new = tc.arguments
            if new.startswith(agg["args"]):
                delta = new[len(agg["args"]):]
                agg["args"] = new
            else:
                agg["args"] += new
                delta = new
        return self.order.index(cid), agg, new_name, delta


def _master_key(api_key):
    """The master key is mandatory: explicit --api-key/env wins; otherwise a
    generated key is persisted in the db so the console and /v1 are never
    left open."""
    if api_key:
        return api_key, "env"
    saved = store.meta_get("master_key")
    if saved:
        return saved, "stored"
    api_key = "sk-dp-" + secrets.token_urlsafe(24)
    store.meta_set("master_key", api_key)
    return api_key, "generated"


def create_app(api_key=None):
    api_key, key_src = _master_key(api_key)
    app = FastAPI(title="api", docs_url=None, redoc_url=None,
                  openapi_url=None)
    pool = accounts_mod.Pool()
    imported = pool.import_detected(creds_mod.detect_all())
    app.state.pool = pool
    app.state.proxy_key = api_key
    app.state.http = httpx.Client(
        timeout=httpx.Timeout(connect=15, read=_READ_TIMEOUT, write=60,
                              pool=30),
        http2=_HTTP2,
        limits=httpx.Limits(max_keepalive_connections=20,
                            keepalive_expiry=120))
    app.state.key_slots = {}
    app.state.slot_lock = threading.Lock()
    if imported:
        print(f"accounts: imported {imported} detected credential(s)")
    if not pool.accounts():
        print("accounts: none configured — add one in the admin console "
              "or via DEVIN_SESSION_TOKEN")
    if key_src == "generated":
        print(f"admin key (generated, saved): {api_key}")
    elif key_src == "stored":
        print(f"admin key (from db): {api_key}")

    @app.middleware("http")
    async def _stealth(request, call_next):
        resp = await call_next(request)
        if "server" in resp.headers:
            del resp.headers["server"]
        return resp

    def check_key(request: Request):
        """Bearer auth: master key, or any key created in the admin UI."""
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        request.state.key_name = None
        request.state.key_row = None
        if token and token == app.state.proxy_key:
            request.state.key_name = "master"
            return
        if token:
            info = store.key_info(token)
            if info and not info["disabled"]:
                request.state.key_name = info["name"]
                request.state.key_row = info
                return
        raise HTTPException(401, "unauthorized")

    def _key_models(request):
        """-> set of allowed model names for the caller's key, or None."""
        row = getattr(request.state, "key_row", None)
        if row and row.get("models"):
            return set(row["models"])
        return None

    def model_allowed(request, requested, resolved):
        allowed = _key_models(request)
        return (allowed is None or requested in allowed
                or resolved in allowed)

    def acquire_key_slot(request):
        """Per-key concurrency guard -> release fn, or None if at the cap."""
        row = getattr(request.state, "key_row", None)
        limit = (row or {}).get("max_concurrent") or 0
        if not row or not limit:
            return lambda: None
        kid = row["id"]
        with app.state.slot_lock:
            n = app.state.key_slots.get(kid, 0)
            if n >= limit:
                return None
            app.state.key_slots[kid] = n + 1

        def release():
            with app.state.slot_lock:
                app.state.key_slots[kid] = max(
                    0, app.state.key_slots.get(kid, 0) - 1)
        return release

    def _release_gen(gen, release):
        try:
            yield from gen
        finally:
            release()

    def _record(request, body, model, ok, status, error, usage, t0, ttft,
                account=None, endpoint=None):
        try:
            rl = reqlog_for(request, body)
            msgs = body.get("messages") or body.get("input") or []
            store.log_request(
                model=body.get("model"), resolved_model=model,
                stream=bool(body.get("stream")), ok=ok, status=status,
                error=(error or "")[:500],
                prompt_tokens=(usage or {}).get("prompt_tokens", 0),
                completion_tokens=(usage or {}).get("completion_tokens", 0),
                latency_ms=int((time.perf_counter() - t0) * 1000),
                ttft_ms=ttft,
                client=request.client.host if request.client else None,
                key_name=getattr(request.state, "key_name", None),
                account=account, endpoint=endpoint,
                messages_json=json.dumps(msgs, ensure_ascii=False)[:20000],
                request_json=rl.request_json,
                events_json=rl.events_dump(),
                sse_json=rl.sse_dump(),
                flags=rl.flags_str(),
            )
        except Exception:
            pass

    # ---------- session routing ----------

    def session_key_for(request, body):
        """Sticky-session key: explicit id > user/prompt_cache_key >
        conversation fingerprint (first messages)."""
        sid = (body.get("session_id") or body.get("conversation_id")
               or request.headers.get("x-session-id")
               or request.headers.get("x-conversation-id"))
        if sid:
            return "s:" + str(sid)[:128]
        user = body.get("user") or body.get("prompt_cache_key")
        if user:
            return "u:" + str(user)[:128]
        msgs = body.get("messages") or []
        parts = []
        for m in msgs[:2]:
            if not isinstance(m, dict):
                continue
            c = m.get("content")
            if isinstance(c, list):
                c = " ".join(str(p.get("text") or "") for p in c
                             if isinstance(p, dict))
            parts.append(f"{m.get('role')}:{str(c)[:2000]}")
        if not parts:
            return None
        return "h:" + hashlib.sha256(
            "|".join(parts).encode()).hexdigest()[:32]

    # ---------- OpenAI -> upstream mapping ----------

    def msg_parts(content):
        """content -> (text, [images])"""
        if content is None:
            return "", []
        if isinstance(content, str):
            return content, []
        texts, images = [], []
        for p in content:
            if not isinstance(p, dict):
                continue
            if p.get("type") in ("text", "input_text", "output_text"):
                texts.append(p.get("text") or "")
            elif p.get("type") in ("image_url", "input_image"):
                url = p.get("image_url") or ""
                if isinstance(url, dict):
                    url = url.get("url", "")
                if url.startswith("data:") and "," in url:
                    mime = url[5:url.index(";")] if ";" in url[:30] else "image/png"
                    images.append({"mime": mime, "base64": url.split(",", 1)[1]})
        return "\n".join(texts), images

    def map_messages(messages):
        system, prompts = [], []
        for m in messages or []:
            role = m.get("role")
            if role in ("system", "developer"):
                text, _ = msg_parts(m.get("content"))
                if text:
                    system.append(text)
            elif role == "user":
                text, images = msg_parts(m.get("content"))
                prompts.append(upstream.make_prompt("user", text, images=images))
            elif role == "assistant":
                text, _ = msg_parts(m.get("content"))
                tcs = [{"id": tc.get("id", ""),
                        "name": (tc.get("function") or {}).get("name", ""),
                        "arguments": (tc.get("function") or {}).get("arguments", "")}
                       for tc in m.get("tool_calls") or []]
                prompts.append(upstream.make_prompt(
                    "assistant", text, tool_calls=tcs,
                    thinking=m.get("reasoning_content")))
            elif role == "tool":
                text, _ = msg_parts(m.get("content"))
                prompts.append(upstream.make_prompt(
                    "tool", text, tool_call_id=m.get("tool_call_id", "")))
        return "\n\n".join(system) or None, prompts

    def map_tools(tools):
        return [{"name": (t.get("function") or {}).get("name", ""),
                 "description": (t.get("function") or {}).get("description", ""),
                 "parameters": (t.get("function") or {}).get("parameters") or {}}
                for t in tools or [] if t.get("type") == "function"]

    def resolve_model(body):
        uid = MODEL_ALIASES.get(body.get("model"),
                              body.get("model") or "claude-sonnet-5-medium")
        effort = ((body.get("reasoning") or {}).get("effort")
                  or body.get("reasoning_effort"))
        suffix = _EFFORT_SUFFIX.get(str(effort or "").lower())
        if suffix and uid.rsplit("-", 1)[-1] in _UID_SUFFIXES:
            cand = uid.rsplit("-", 1)[0] + "-" + suffix
            if cand in KNOWN_UIDS:
                uid = cand
        return uid

    def build_request(body, model, acct):
        system, prompts = map_messages(body.get("messages"))
        tools = map_tools(body.get("tools"))
        if body.get("tool_choice") == "none":
            tools = []
        jwt = upstream.get_user_jwt(app.state.http, acct.api_server_url,
                                    acct.token)
        return upstream.build_request(
            acct.token, jwt, model, system, prompts, tools,
            max_tokens=body.get("max_tokens") or body.get("max_output_tokens")
            or body.get("max_completion_tokens"),
            temperature=body.get("temperature"), top_p=body.get("top_p"))

    def iter_chat(body, session_key=None, force_account=None, stream=True,
                  log=None):
        """Failover driver. Yields (acct, event). Terminal upstream error is
        yielded as (acct_or_None, dict). On success returns quietly.

        stream=False buffers each attempt's events instead of forwarding them
        live, so a mid-stream failure can be retried on the next account —
        safe because nothing has reached the client yet."""
        model = resolve_model(body)
        want = {m for m in (body.get("model"), model) if m}
        tried, last_err = set(), {"message": "no accounts configured",
                                  "http_error": 503, "kind": "no_account"}
        force_id = force_account.id if force_account else None
        attempts = 1 if force_account else accounts_mod.MAX_ATTEMPTS
        transient_retries = 0       # 断流/异常允许重试同一账号（连接闪断≠账号坏）
        for attempt in range(1, attempts + 1):
            acct = pool.pick(session_key, exclude=tried, force_id=force_id,
                             models=want)
            if acct is None:
                if not tried and pool.accounts():
                    last_err = {"message": "no eligible account (all busy, "
                                "cooling down, or model-restricted)",
                                "http_error": 503, "kind": "no_account"}
                break
            tried.add(acct.id)
            force_id = None
            if log:
                log.ev("attempt", n=attempt, account=acct.display(),
                       account_id=acct.id)
            try:
                req = build_request(body, model, acct)
            except Exception as e:
                last_err = {"message": f"{acct.display()}: {e}",
                            "http_error": getattr(
                                getattr(e, "response", None), "status_code",
                                502),
                            "kind": "build_error"}
                if log:
                    log.ev("build_error", account=acct.display(),
                           error=str(e)[:500])
                pool.release(acct)
                pool.mark_fail(acct, last_err)
                continue
            got_content, saw_stop, terminal, buf = False, False, None, []
            try:
                for ev in upstream.stream_chat(
                        app.state.http, acct.api_server_url, acct.token, req):
                    if isinstance(ev, dict):
                        if log:
                            if ev.get("kind") == "truncated":
                                log.flag("truncated")
                            log.ev("upstream_err", account=acct.display(),
                                   detail=ev)
                        if not got_content:
                            terminal = ev
                            break
                        if stream:
                            pool.mark_fail(acct, ev)
                            yield acct, ev
                            return
                        terminal = ev       # buffered: retry on next account
                        break
                    got_content = True
                    if ev.stop_reason:
                        saw_stop = True
                    if log:
                        log.ev("msg", **_msg_summary(ev))
                    if stream:
                        yield acct, ev
                    else:
                        buf.append(ev)
            except Exception as e:
                e2 = {"kind": "exception", "message": str(e)}
                if log:
                    log.ev("exception", account=acct.display(),
                           error=repr(e)[:800])
                if got_content and stream:
                    pool.mark_fail(acct, e2)
                    yield acct, e2
                    return
                terminal = e2
            finally:
                pool.release(acct)
            if terminal is None:
                if log:
                    if got_content and not saw_stop:
                        log.flag("no_stop_reason")
                    log.ev("end", account=acct.display(), clean=True,
                           stop_reason=saw_stop)
                for ev in buf:
                    yield acct, ev
                pool.mark_ok(acct)
                pool.pin(session_key, acct)
                return
            last_err = terminal
            if terminal.get("kind") in ("truncated", "exception",
                                        "protocol_error") \
                    and transient_retries < 2 and attempt < attempts:
                transient_retries += 1
                tried.discard(acct.id)      # transient cut: same acct may retry
                if log:
                    log.flag("retry_same_account")
            if log:
                log.ev("failover", account=acct.display(), detail=terminal,
                       had_content=got_content)
                log.flag("retried_partial" if got_content else "retried")
            pool.mark_fail(acct, terminal)
        if log:
            log.ev("failed", detail=last_err)
        yield None, last_err

    def oai_usage(u):
        return {"prompt_tokens": u.get("prompt_tokens", 0),
                "completion_tokens": u.get("completion_tokens", 0),
                "total_tokens": u.get("prompt_tokens", 0)
                + u.get("completion_tokens", 0)}

    # ---------- endpoints ----------

    @app.get("/v1/models", dependencies=[Depends(check_key)])
    def list_models(request: Request):
        allowed = _key_models(request)
        names = KNOWN_UIDS + list(MODEL_ALIASES)
        data = [{"id": m, "object": "model", "created": 0, "owned_by": "proxy"}
                for m in names if allowed is None or m in allowed]
        return {"object": "list", "data": data}

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.post("/v1/chat/completions", dependencies=[Depends(check_key)])
    async def chat(request: Request):
        t0 = time.perf_counter()
        try:
            body = await request.json()
        except Exception as e:
            reqlog_for(request, {})
            _record(request, {}, "", False, 400, f"invalid JSON body: {e}",
                    None, t0, None, endpoint="chat")
            raise HTTPException(400, f"invalid JSON body: {e}")
        if not isinstance(body, dict):
            reqlog_for(request, {"raw": str(body)[:2000]})
            _record(request, {}, "", False, 400, "body must be a JSON object",
                    None, t0, None, endpoint="chat")
            raise HTTPException(400, "request body must be a JSON object")
        reqlog_for(request, body)
        skey = session_key_for(request, body)
        model = resolve_model(body)
        if not model_allowed(request, body.get("model"), model):
            _record(request, body, model, False, 403,
                    "model not permitted for this key", None, t0, None,
                    endpoint="chat")
            raise HTTPException(403, "model not permitted for this key")
        release = acquire_key_slot(request)
        if release is None:
            _record(request, body, model, False, 429,
                    "key concurrency limit reached", None, t0, None,
                    endpoint="chat")
            raise HTTPException(429, "key concurrency limit reached")
        if body.get("stream"):
            return StreamingResponse(
                _release_gen(_sse_stream(request, body, model, skey, t0),
                             release),
                media_type="text/event-stream")
        try:
            resp = await run_in_threadpool(
                _collect, request, body, model, skey, t0)
            return JSONResponse(resp)
        finally:
            release()

    def _collect(request, body, model, skey, t0, force_account=None):
        rl = reqlog_for(request, body)
        texts, think, agg = [], [], ToolAgg()
        finish, usage, rid = "stop", None, f"chatcmpl-{uuid.uuid4().hex[:24]}"
        err, acct_name = None, None
        for acct, ev in iter_chat(body, skey, force_account, stream=False,
                                  log=rl):
            if acct:
                acct_name = acct.display()
            if isinstance(ev, dict):
                err = ev
                break
            if ev.message_id:
                rid = ev.message_id
            if ev.delta_text:
                texts.append(ev.delta_text)
            if ev.delta_thinking:
                think.append(ev.delta_thinking)
            for tc in ev.delta_tool_calls:
                agg.feed(tc)
            if ev.stop_reason:
                finish = upstream.STOP_REASONS.get(ev.stop_reason, "stop")
            if ev.HasField("usage"):
                usage = upstream.extract_usage(ev)
        if err:
            status = err.get("http_error", 502)
            _record(request, body, model, False, status,
                    err.get("message"), usage, t0, None,
                    account=acct_name, endpoint="chat")
            raise HTTPException(status, err.get("message", "upstream error"))

        message = {"role": "assistant", "content": "".join(texts) or None}
        if think:
            message["reasoning_content"] = "".join(think)
        if agg.order:
            message["tool_calls"] = [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"], "arguments": c["args"]}}
                for c in (agg.calls[i] for i in agg.order)]
            if finish == "stop":
                finish = "tool_calls"
        resp = {"id": rid, "object": "chat.completion", "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "message": message,
                             "finish_reason": finish}]}
        if usage:
            resp["usage"] = oai_usage(usage)
        rl.out(resp)
        _record(request, body, model, True, 200, None, usage, t0, None,
                account=acct_name, endpoint="chat")
        return resp

    def _sse_stream(request, body, model, skey, t0, force_account=None):
        rl = reqlog_for(request, body)
        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        rid, created = f"chatcmpl-{uuid.uuid4().hex[:24]}", int(time.time())
        agg, finish, usage, err = ToolAgg(), "stop", None, None
        acct_name, ttft, started = None, None, False

        def chunk(delta, fin=None, **extra):
            obj = {"id": rid, "object": "chat.completion.chunk", "created": created,
                   "model": model,
                   "choices": [{"index": 0, "delta": delta,
                                "finish_reason": fin}]}
            obj.update(extra)
            return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

        try:
            for acct, ev in iter_chat(body, skey, force_account, stream=True,
                                      log=rl):
                if acct:
                    acct_name = acct.display()
                if not started:
                    started = True
                    yield rl.out(chunk({"role": "assistant"}))
                if isinstance(ev, dict):
                    err = ev
                    break
                if ttft is None:
                    ttft = int((time.perf_counter() - t0) * 1000)
                if ev.message_id:
                    rid = ev.message_id
                if ev.delta_text:
                    yield rl.out(chunk({"content": ev.delta_text}))
                if ev.delta_thinking:
                    yield rl.out(chunk({"reasoning_content": ev.delta_thinking}))
                for tc in ev.delta_tool_calls:
                    fed = agg.feed(tc)
                    if fed:
                        idx, call, new_name, delta = fed
                        fn = {"name": "", "arguments": ""}
                        if new_name:
                            fn["name"] = call["name"]
                        if delta:
                            fn["arguments"] = delta
                        yield rl.out(chunk({"tool_calls": [
                            {"index": idx, "id": call["id"],
                             "type": "function", "function": fn}]}))
                if ev.stop_reason:
                    finish = upstream.STOP_REASONS.get(ev.stop_reason, "stop")
                if ev.HasField("usage"):
                    usage = upstream.extract_usage(ev)
        except GeneratorExit:
            rl.flag("client_aborted")
            rl.ev("client_aborted")
            _record(request, body, model, False, 499,
                    "client disconnected mid-stream", usage, t0, ttft,
                    account=acct_name, endpoint="chat")
            raise
        except Exception as e:
            err = {"kind": "proxy_error", "message": str(e)}
            rl.ev("proxy_exception", error=repr(e)[:800])
        if not started:
            yield rl.out(chunk({"role": "assistant"}))

        if err:
            # Surface the failure as an SSE error object (what OpenAI clients
            # expect) — never a fake finish_reason:"stop", which made upstream
            # 断流 look like a normal completion and hid the real error.
            kind = err.get("kind") or "upstream_error"
            yield rl.out("data: " + json.dumps(
                {"error": {"message": err.get("message", "upstream error"),
                           "type": "server_error", "code": kind}},
                ensure_ascii=False) + "\n\n")
            rl.ev("downstream_error", detail=err)
            _record(request, body, model, False, err.get("http_error", 502),
                    err.get("message"), usage, t0, ttft,
                    account=acct_name, endpoint="chat")
        else:
            if agg.order and finish == "stop":
                finish = "tool_calls"
            rl.ev("finish", finish_reason=finish)
            yield rl.out(chunk({}, finish))
            if include_usage and usage:
                yield rl.out(f'data: {{"id": "{rid}", "object": "chat.completion.chunk", "created": {created}, "model": "{model}", "choices": [], "usage": {json.dumps(oai_usage(usage))}}}\n\n')
            _record(request, body, model, True, 200, None, usage, t0, ttft,
                    account=acct_name, endpoint="chat")
        yield rl.out("data: [DONE]\n\n")

    # ---------- Responses API ----------

    from . import responses as responses_mod

    @app.post("/v1/responses", dependencies=[Depends(check_key)])
    async def create_response(request: Request):
        t0 = time.perf_counter()
        try:
            body = await request.json()
        except Exception as e:
            reqlog_for(request, {})
            _record(request, {}, "", False, 400, f"invalid JSON body: {e}",
                    None, t0, None, endpoint="responses")
            raise HTTPException(400, f"invalid JSON body: {e}")
        if not isinstance(body, dict):
            reqlog_for(request, {"raw": str(body)[:2000]})
            _record(request, {}, "", False, 400, "body must be a JSON object",
                    None, t0, None, endpoint="responses")
            raise HTTPException(400, "request body must be a JSON object")
        reqlog_for(request, body)
        release = acquire_key_slot(request)
        if release is None:
            _record(request, body, resolve_model(body), False, 429,
                    "key concurrency limit reached", None, t0, None,
                    endpoint="responses")
            raise HTTPException(429, "key concurrency limit reached")
        return await run_in_threadpool(
            responses_mod.handle, request, body, t0,
            iter_chat=iter_chat, resolve_model=resolve_model,
            record=_record, release=release)

    @app.get("/v1/responses/{rid}", dependencies=[Depends(check_key)])
    def get_response(rid: str):
        row = store.get_response(rid)
        if not row:
            raise HTTPException(404, "Response not found")
        return json.loads(row["response_json"])

    @app.delete("/v1/responses/{rid}", dependencies=[Depends(check_key)])
    def delete_response(rid: str):
        if not store.delete_response(rid):
            raise HTTPException(404, "Response not found")
        return {"id": rid, "object": "response.deleted", "deleted": True}

    @app.get("/v1/responses/{rid}/input_items", dependencies=[Depends(check_key)])
    def response_input_items(rid: str):
        row = store.get_response(rid)
        if not row:
            raise HTTPException(404, "Response not found")
        out_ids = {i.get("id") for i in
                   (json.loads(row["response_json"]).get("output") or [])}
        items = [i for i in json.loads(row["items_json"])
                 if i.get("id") not in out_ids]
        return {"object": "list", "data": items,
                "first_id": items[0].get("id") if items else None,
                "last_id": items[-1].get("id") if items else None,
                "has_more": False}

    app.state.collect = _collect
    app.state.sse_stream = _sse_stream
    app.state.session_key_for = session_key_for
    app.state.iter_chat = iter_chat
    app.state.resolve_model = resolve_model
    app.state.record = _record

    @app.on_event("shutdown")
    def _shutdown():
        app.state.http.close()

    from . import admin
    app.include_router(admin.make_router(app))
    return app
