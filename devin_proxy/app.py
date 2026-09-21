"""FastAPI app: OpenAI /v1/chat/completions -> Devin Cascade GetChatMessage."""
import json
import time
import uuid

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

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


def create_app(api_key=None):
    app = FastAPI(title="devin-proxy")
    cred = creds_mod.load()
    if not cred:
        raise RuntimeError(
            "no Devin credentials: set DEVIN_SESSION_TOKEN, sign in with "
            "`devin auth login`, or sign in Devin Desktop")
    app.state.creds = cred
    app.state.proxy_key = api_key
    app.state.http = httpx.Client(timeout=httpx.Timeout(300, connect=15))

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

    def _record(request, body, model, ok, status, error, usage, t0, ttft):
        try:
            msgs = body.get("messages") or []
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
                messages_json=json.dumps(msgs, ensure_ascii=False)[:20000],
            )
        except Exception:
            pass

    # ---------- OpenAI -> upstream mapping ----------

    def msg_parts(content):
        """content -> (text, [images])"""
        if content is None:
            return "", []
        if isinstance(content, str):
            return content, []
        texts, images = [], []
        for p in content:
            if p.get("type") == "text":
                texts.append(p.get("text") or "")
            elif p.get("type") == "image_url":
                url = (p.get("image_url") or {}).get("url", "")
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
                prompts.append(upstream.make_prompt("assistant", text, tool_calls=tcs))
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

    def build_request(body):
        model = MODEL_ALIASES.get(body.get("model"), body.get("model") or "claude-sonnet-5-medium")
        system, prompts = map_messages(body.get("messages"))
        tools = map_tools(body.get("tools"))
        if body.get("tool_choice") == "none":
            tools = []
        jwt = upstream.get_user_jwt(app.state.http, cred.api_server_url, cred.api_key)
        req = upstream.build_request(
            cred.api_key, jwt, model, system, prompts, tools,
            max_tokens=body.get("max_tokens") or body.get("max_completion_tokens"),
            temperature=body.get("temperature"), top_p=body.get("top_p"))
        return model, req

    class ToolAgg:
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

    def oai_usage(u):
        return {"prompt_tokens": u.get("prompt_tokens", 0),
                "completion_tokens": u.get("completion_tokens", 0),
                "total_tokens": u.get("prompt_tokens", 0) + u.get("completion_tokens", 0)}

    # ---------- endpoints ----------

    @app.get("/v1/models", dependencies=[Depends(check_key)])
    def list_models():
        data = [{"id": m, "object": "model", "created": 0, "owned_by": "devin"}
                for m in KNOWN_UIDS + list(MODEL_ALIASES)]
        return {"object": "list", "data": data}

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "creds": cred.source}

    @app.post("/v1/chat/completions", dependencies=[Depends(check_key)])
    async def chat(request: Request):
        body = await request.json()
        t0 = time.perf_counter()
        try:
            model, req = build_request(body)
        except Exception as e:
            _record(request, body, body.get("model"), False, 400, str(e), None, t0, None)
            raise HTTPException(400, f"bad request: {e}")

        if body.get("stream"):
            return StreamingResponse(_sse_stream(request, body, model, req, t0),
                                     media_type="text/event-stream")
        return JSONResponse(_collect(request, body, model, req, t0))

    def _collect(request, body, model, req, t0):
        texts, think, agg = [], [], ToolAgg()
        finish, usage, rid, err = "stop", None, f"chatcmpl-{uuid.uuid4().hex[:24]}", None
        for ev in upstream.stream_chat(app.state.http, cred.api_server_url,
                                       cred.api_key, req):
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
                    err.get("message"), usage, t0, None)
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
                "choices": [{"index": 0, "message": message, "finish_reason": finish}]}
        if usage:
            resp["usage"] = oai_usage(usage)
        _record(request, body, model, True, 200, None, usage, t0, None)
        return resp

    def _sse_stream(request, body, model, req, t0):
        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        rid, created = f"chatcmpl-{uuid.uuid4().hex[:24]}", int(time.time())
        agg, finish, usage, err = ToolAgg(), "stop", None, None

        def chunk(delta, fin=None, **extra):
            obj = {"id": rid, "object": "chat.completion.chunk", "created": created,
                   "model": model,
                   "choices": [{"index": 0, "delta": delta, "finish_reason": fin}]}
            obj.update(extra)
            return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

        yield chunk({"role": "assistant"})
        ttft = None
        try:
            for ev in upstream.stream_chat(app.state.http, cred.api_server_url,
                                           cred.api_key, req):
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

        if err:
            yield chunk({}, "stop")
            yield "data: " + json.dumps(
                {"error": {"message": err.get("message", "upstream error"),
                           "type": "server_error"}}) + "\n\n"
            _record(request, body, model, False, err.get("http_error", 502),
                    err.get("message"), usage, t0, ttft)
        else:
            if agg.order and finish == "stop":
                finish = "tool_calls"
            yield chunk({}, finish)
            if include_usage and usage:
                yield f'data: {{"id": "{rid}", "object": "chat.completion.chunk", "created": {created}, "model": "{model}", "choices": [], "usage": {json.dumps(oai_usage(usage))}}}\n\n'
            _record(request, body, model, True, 200, None, usage, t0, ttft)
        yield "data: [DONE]\n\n"

    app.state.build_request = build_request
    app.state.collect = _collect
    app.state.sse_stream = _sse_stream

    @app.on_event("shutdown")
    def _shutdown():
        app.state.http.close()

    from . import admin
    app.include_router(admin.make_router(app))
    return app
