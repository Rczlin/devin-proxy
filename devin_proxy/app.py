"""FastAPI app: OpenAI /v1/chat/completions + /v1/responses -> Devin Cascade."""
import hashlib
import json
import time
import uuid

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

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


def create_app(api_key=None):
    app = FastAPI(title="devin-proxy")
    pool = accounts_mod.Pool()
    imported = pool.import_detected(creds_mod.detect_all())
    app.state.pool = pool
    app.state.proxy_key = api_key
    app.state.http = httpx.Client(timeout=httpx.Timeout(300, connect=15))
    if imported:
        print(f"accounts: imported {imported} detected credential(s)")
    if not pool.accounts():
        print("accounts: none configured — add one in the admin console "
              "or via DEVIN_SESSION_TOKEN")

    def check_key(request: Request):
        """Bearer auth: master --api-key, or any key created in the admin UI."""
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        request.state.key_name = None
        if app.state.proxy_key and token == app.state.proxy_key:
            request.state.key_name = "master"
            return
        if token:
            info = store.key_info(token)
            if info and not info["disabled"]:
                request.state.key_name = info["name"]
                return
        if app.state.proxy_key or store.has_keys():
            raise HTTPException(401, "unauthorized")

    def _record(request, body, model, ok, status, error, usage, t0, ttft,
                account=None, endpoint=None):
        try:
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

    def iter_chat(body, session_key=None, force_account=None):
        """Failover driver. Yields (acct, event). Terminal upstream error is
        yielded as (acct_or_None, dict). On success returns quietly."""
        model = resolve_model(body)
        tried, last_err = set(), {"message": "no accounts configured",
                                  "http_error": 503}
        force_id = force_account.id if force_account else None
        attempts = 1 if force_account else accounts_mod.MAX_ATTEMPTS
        for _ in range(attempts):
            acct = pool.pick(session_key, exclude=tried, force_id=force_id)
            if acct is None:
                break
            tried.add(acct.id)
            force_id = None
            try:
                req = build_request(body, model, acct)
            except Exception as e:
                last_err = {"message": f"{acct.display()}: {e}",
                            "http_error": getattr(
                                getattr(e, "response", None), "status_code", 502)}
                pool.release(acct)
                pool.mark_fail(acct, last_err)
                continue
            got_content, terminal = False, None
            try:
                for ev in upstream.stream_chat(
                        app.state.http, acct.api_server_url, acct.token, req):
                    if isinstance(ev, dict):
                        if not got_content:
                            terminal = ev
                            break
                        pool.mark_fail(acct, ev)
                        yield acct, ev
                        return
                    got_content = True
                    yield acct, ev
            except Exception as e:
                if got_content:
                    err = {"message": str(e)}
                    pool.mark_fail(acct, err)
                    yield acct, err
                    return
                terminal = {"message": str(e)}
            finally:
                pool.release(acct)
            if terminal is None:
                pool.mark_ok(acct)
                pool.pin(session_key, acct)
                return
            last_err = terminal
            pool.mark_fail(acct, terminal)
        yield None, last_err

    def oai_usage(u):
        return {"prompt_tokens": u.get("prompt_tokens", 0),
                "completion_tokens": u.get("completion_tokens", 0),
                "total_tokens": u.get("prompt_tokens", 0)
                + u.get("completion_tokens", 0)}

    # ---------- endpoints ----------

    @app.get("/v1/models", dependencies=[Depends(check_key)])
    def list_models():
        data = [{"id": m, "object": "model", "created": 0, "owned_by": "devin"}
                for m in KNOWN_UIDS + list(MODEL_ALIASES)]
        return {"object": "list", "data": data}

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "accounts": pool.summary()}

    @app.post("/v1/chat/completions", dependencies=[Depends(check_key)])
    async def chat(request: Request):
        body = await request.json()
        t0 = time.perf_counter()
        skey = session_key_for(request, body)
        model = resolve_model(body)
        if body.get("stream"):
            return StreamingResponse(
                _sse_stream(request, body, model, skey, t0),
                media_type="text/event-stream")
        return JSONResponse(_collect(request, body, model, skey, t0))

    def _collect(request, body, model, skey, t0, force_account=None):
        texts, think, agg = [], [], ToolAgg()
        finish, usage, rid = "stop", None, f"chatcmpl-{uuid.uuid4().hex[:24]}"
        err, acct_name = None, None
        for acct, ev in iter_chat(body, skey, force_account):
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
        _record(request, body, model, True, 200, None, usage, t0, None,
                account=acct_name, endpoint="chat")
        return resp

    def _sse_stream(request, body, model, skey, t0, force_account=None):
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
            for acct, ev in iter_chat(body, skey, force_account):
                if acct:
                    acct_name = acct.display()
                if not started:
                    started = True
                    yield chunk({"role": "assistant"})
                if isinstance(ev, dict):
                    err = ev
                    break
                if ttft is None:
                    ttft = int((time.perf_counter() - t0) * 1000)
                if ev.message_id:
                    rid = ev.message_id
                if ev.delta_text:
                    yield chunk({"content": ev.delta_text})
                if ev.delta_thinking:
                    yield chunk({"reasoning_content": ev.delta_thinking})
                for tc in ev.delta_tool_calls:
                    fed = agg.feed(tc)
                    if fed:
                        idx, call, new_name, delta = fed
                        fn = {"name": "", "arguments": ""}
                        if new_name:
                            fn["name"] = call["name"]
                        if delta:
                            fn["arguments"] = delta
                        yield chunk({"tool_calls": [
                            {"index": idx, "id": call["id"],
                             "type": "function", "function": fn}]})
                if ev.stop_reason:
                    finish = upstream.STOP_REASONS.get(ev.stop_reason, "stop")
                if ev.HasField("usage"):
                    usage = upstream.extract_usage(ev)
        except Exception as e:
            err = {"message": str(e)}
        if not started:
            yield chunk({"role": "assistant"})

        if err:
            yield chunk({}, "stop")
            yield "data: " + json.dumps(
                {"error": {"message": err.get("message", "upstream error"),
                           "type": "server_error"}}) + "\n\n"
            _record(request, body, model, False, err.get("http_error", 502),
                    err.get("message"), usage, t0, ttft,
                    account=acct_name, endpoint="chat")
        else:
            if agg.order and finish == "stop":
                finish = "tool_calls"
            yield chunk({}, finish)
            if include_usage and usage:
                yield f'data: {{"id": "{rid}", "object": "chat.completion.chunk", "created": {created}, "model": "{model}", "choices": [], "usage": {json.dumps(oai_usage(usage))}}}\n\n'
            _record(request, body, model, True, 200, None, usage, t0, ttft,
                    account=acct_name, endpoint="chat")
        yield "data: [DONE]\n\n"

    # ---------- Responses API ----------

    from . import responses as responses_mod

    @app.post("/v1/responses", dependencies=[Depends(check_key)])
    async def create_response(request: Request):
        body = await request.json()
        t0 = time.perf_counter()
        return responses_mod.handle(
            request, body, t0,
            iter_chat=iter_chat, resolve_model=resolve_model,
            record=_record)

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
