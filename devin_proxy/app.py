"""FastAPI app: OpenAI /v1/chat/completions + /v1/responses -> Devin Cascade."""
import base64
import hashlib
import json
import os
import secrets
import threading
import time
import traceback
import uuid

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from . import accounts as accounts_mod
from . import creds as creds_mod
from . import models as models_mod
from . import proto
from . import store
from . import upstream

# Upstream read timeout: Devin's agent stream can pause for a long time while
# the server runs tools. 300s was too short and looked like a silent 断流.
_READ_TIMEOUT = float(os.environ.get("DEVIN_PROXY_READ_TIMEOUT", "1800"))
# Per-blob char caps for the per-request debug record (request body /
# upstream event timeline / outbound SSE frames).
_LOG_CAP = int(os.environ.get("DEVIN_PROXY_LOG_CAP", "200000"))
_EV_CAP = 12000          # one event / one SSE payload
_REQ_CAP = 60000         # raw request body
_RAW_CAP = 8000          # unparsed-body forensics prefix
# Full-fidelity wire capture (inbound body, upstream pb request, every
# upstream frame, outbound SSE — untruncated). 0 disables; _CAP_MAX bounds
# one request's total captured bytes so a runaway stream can't eat memory.
_CAPTURE = os.environ.get("DEVIN_PROXY_CAPTURE", "1") not in ("0", "false")
_CAP_MAX = int(os.environ.get("DEVIN_PROXY_CAP_MAX", str(32 * 1024 * 1024)))

try:
    import h2  # noqa: F401
    _HTTP2 = True
except Exception:
    _HTTP2 = False


class ReqLog:
    """Per-request debug recorder: request body, upstream event timeline and
    every SSE payload sent downstream. Attached to request.state and persisted
    into the requests row by _record. Never raises."""

    def __init__(self, body=None, raw=None):
        self.t0 = time.perf_counter()
        self.events, self.sse, self.flags = [], [], set()
        self.request_json = None
        self._ev_sz = self._sse_sz = 0
        self._ev_full = self._sse_full = False
        self.raw_len = self.raw_sha = None
        # untruncated wire capture — claude-tap style. events/sse above are
        # the bounded summaries; this is the full packet-level record.
        self.cap = {"inbound": None, "attempts": [], "downstream": []}
        self._cap_sz = 0
        self._cap_full = not _CAPTURE
        if raw is not None:
            self.raw_len = len(raw)
            self.raw_sha = hashlib.sha256(raw).hexdigest()[:16]
        if body is not None:
            try:
                s = json.dumps(body, ensure_ascii=False, default=str)
                if len(s) > _REQ_CAP:
                    self.flags.add("request_capped")
                self.request_json = s[:_REQ_CAP]
            except Exception:
                pass
        elif raw is not None:
            # unparsable body — keep a bounded raw prefix for forensics
            try:
                self.request_json = json.dumps(
                    {"_unparsed": raw[:_RAW_CAP].decode("utf-8", "replace"),
                     "_body_len": len(raw)})
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

    def _cap_room(self, n):
        """Size guard for the untruncated capture. False once _CAP_MAX hit —
        the flag marks the bundle so analysis knows data was dropped."""
        if self._cap_full:
            return False
        if self._cap_sz + n > _CAP_MAX:
            self.flags.add("capture_capped")
            self._cap_full = True
            return False
        self._cap_sz += n
        return True

    def cap_inbound(self, request, raw):
        try:
            body = base64.b64encode(raw or b"").decode()
            if self._cap_room(len(body)):
                self.cap["inbound"] = {
                    "method": request.method,
                    "url": str(request.url),
                    "headers": _safe_headers(request),
                    "body_b64": body,
                }
        except Exception:
            pass

    def cap_attempt(self, att):
        """att: dict the caller keeps filling (frames appended via cap_frame)."""
        try:
            if self._cap_room(len(att.get("request_pb_b64") or "")):
                self.cap["attempts"].append(att)
                return att
        except Exception:
            pass
        return None

    def cap_frame(self, att, kind, data):
        """One raw upstream payload (kind: frame|trailer|http_body|
        resp_headers). `data` is bytes (already gunzipped for frames)."""
        try:
            b64 = base64.b64encode(data).decode()
            if self._cap_room(len(b64)):
                att.setdefault("frames", []).append({"k": kind, "b64": b64})
        except Exception:
            pass

    def has_capture(self):
        try:
            return bool(self.cap["inbound"] or self.cap["attempts"]
                        or self.cap["downstream"])
        except Exception:
            return False

    def out(self, payload):
        """Record one outbound SSE frame (or the final response body).
        Returns the payload so `yield rl.out(s)` stays transparent."""
        try:
            s_full = payload if isinstance(payload, str) else json.dumps(
                payload, ensure_ascii=False, default=str)
            if self._cap_room(len(s_full)):
                self.cap["downstream"].append(s_full)
            if self._sse_full:
                return payload
            s = s_full.strip()
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


_HDR_MASK = ("authorization", "cookie", "token", "secret", "key")


def _safe_headers(request):
    """Inbound headers for forensics — secret-bearing names are masked,
    not dropped, so the client fingerprint stays visible."""
    out = {}
    for i, (k, v) in enumerate(request.headers.items()):
        if i >= 64:
            out["…"] = f"{len(request.headers) - 64} more"
            break
        kl = k.lower()
        out[k] = "***" if any(s in kl for s in _HDR_MASK) else v[:300]
    return out


def reqlog_for(request, body=None, raw=None):
    """Get (or lazily create) the ReqLog for this request."""
    rl = getattr(request.state, "reqlog", None)
    if rl is None:
        rl = ReqLog(body, raw)
        request.state.reqlog = rl
        try:
            if raw is not None:
                rl.cap_inbound(request, raw)
            rl.ev("request",
                  ua=(request.headers.get("user-agent") or "")[:160],
                  path=str(request.url.path),
                  model=body.get("model") if isinstance(body, dict) else None,
                  body_len=rl.raw_len, body_sha=rl.raw_sha,
                  headers=_safe_headers(request))
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
    if len(ev.usage):
        d["usage"] = upstream.extract_usage(ev)
    if ev.HasField("info") and (ev.info.msg_id or ev.info.model_uid):
        d["upstream"] = {"msg": ev.info.msg_id or None,
                         "model": ev.info.model_uid or None,
                         "in": ev.info.input_tokens or None,
                         "out": ev.info.output_tokens or None}
    if ev.elapsed:
        d["elapsed_s"] = round(ev.elapsed, 2)
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
    models_mod.maybe_refresh(app.state.http, pool)

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
            u = usage or {}
            latency = int((time.perf_counter() - t0) * 1000)
            # generation time excludes queueing when we have TTFT; the
            # non-stream path has no TTFT so total latency is the basis
            gen_ms = latency - ttft if ttft is not None else None
            basis = gen_ms if gen_ms is not None else latency
            out_tok = u.get("completion_tokens") or 0
            tps = round(out_tok * 1000 / max(basis, 1), 1) \
                if out_tok else None
            rid = store.log_request(
                model=body.get("model"), resolved_model=model,
                stream=bool(body.get("stream")), ok=ok, status=status,
                error=(error or "")[:500],
                prompt_tokens=u.get("prompt_tokens", 0),
                completion_tokens=out_tok,
                cached_tokens=u.get("cached_tokens", 0),
                cache_creation_tokens=u.get("cache_creation_tokens", 0),
                latency_ms=latency,
                ttft_ms=ttft,
                gen_ms=gen_ms,
                tps=tps,
                upstream_model=u.get("upstream_model_uid") or u.get("model"),
                upstream_msg_id=u.get("upstream_msg_id"),
                upstream_req_id=u.get("upstream_req_id"),
                client=request.client.host if request.client else None,
                key_name=getattr(request.state, "key_name", None),
                account=account, endpoint=endpoint,
                messages_json=json.dumps(msgs, ensure_ascii=False)[:20000],
                request_json=rl.request_json,
                events_json=rl.events_dump(),
                sse_json=rl.sse_dump(),
                flags=rl.flags_str(),
            )
            if rid is not None and rl.has_capture():
                rl.cap["request_id"] = rid
                rl.cap["ok"] = ok
                store.save_capture(rid, ok, rl.cap)
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
        meta = body.get("metadata")
        user = (body.get("user") or body.get("prompt_cache_key")
                or (meta.get("user_id") if isinstance(meta, dict) else None))
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

    # The real CLI keeps one cascade_id / trajectory_id / session_id per
    # conversation and replays them every turn; upstream joins requests by
    # these ids. Derive stable uuids from the session key so proxied
    # conversations look the same (a fresh id per request is the anomaly).
    _ID_NS = uuid.uuid5(uuid.NAMESPACE_DNS, "devin-proxy")

    def conv_ids(skey):
        if not skey:
            return {}
        u = lambda k: str(uuid.uuid5(_ID_NS, f"{k}:{skey}"))
        return {"cascade_id": u("cascade"), "trajectory_id": u("traj"),
                "session_id": u("sess")}

    def prompt_id_of(p):
        """Content-derived per-message uuid — identical replayed history
        gets identical prompt ids, like the real CLI's stable uuids."""
        h = hashlib.sha256()
        h.update(str(p.source).encode())
        h.update(b"\x00" + (p.tool_call_id or "").encode())
        for tc in p.tool_calls:
            h.update(b"\x00" + tc.id.encode() + tc.name.encode()
                     + tc.arguments.encode())
        h.update(b"\x00" + (p.prompt or "").encode())
        h.update(b"\x00" + (p.thinking or "").encode())
        return str(uuid.uuid5(_ID_NS, "m:" + h.hexdigest()))

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
                # reasoning_content is not forwarded: without the thinking
                # signature upstream providers reject replayed thinking.
                prompts.append(upstream.make_prompt(
                    "assistant", text, tool_calls=tcs))
            elif role == "tool":
                text, _ = msg_parts(m.get("content"))
                prompts.append(upstream.make_prompt(
                    "tool", text, tool_call_id=m.get("tool_call_id", "")))
        # Codex-style inputs arrive as runs of same-role items (e.g. several
        # consecutive user messages); model providers reject non-alternating
        # roles, so coalesce adjacent user/assistant prompts. Tool results
        # keep their own prompt — each carries a distinct tool_call_id.
        merged = []
        for p in prompts:
            last = merged[-1] if merged else None
            if (last is not None and p.source == last.source
                    and p.source in (upstream.SOURCE["user"],
                                     upstream.SOURCE["assistant"])):
                if p.prompt:
                    last.prompt = (last.prompt + "\n\n" + p.prompt
                                   if last.prompt else p.prompt)
                last.images.extend(p.images)
                last.tool_calls.extend(p.tool_calls)
                if p.thinking:
                    last.thinking = (last.thinking + "\n" + p.thinking
                                     if last.thinking else p.thinking)
            else:
                merged.append(p)
        prompts = [p for p in merged
                   if p.prompt or len(p.images) or len(p.tool_calls)
                   or p.thinking or p.tool_call_id]
        # drop tool results whose call was dropped/unmapped — a tool_result
        # without a matching tool_use is a provider-side 400.
        seen, out = set(), []
        for p in prompts:
            if p.source == upstream.SOURCE["assistant"]:
                seen.update(tc.id for tc in p.tool_calls)
            elif p.source == upstream.SOURCE["tool"] and p.tool_call_id \
                    and p.tool_call_id not in seen:
                continue
            out.append(p)
        for p in out:
            p.prompt_id = prompt_id_of(p)
        return "\n\n".join(system) or None, out

    def map_tools(tools):
        return [{"name": (t.get("function") or {}).get("name", ""),
                 "description": (t.get("function") or {}).get("description", ""),
                 "parameters": (t.get("function") or {}).get("parameters") or {}}
                for t in tools or [] if t.get("type") == "function"]

    def resolve_model(body):
        name = body.get("model")
        uid = (models_mod.resolve(name)
               or models_mod.default_uid())
        r = body.get("reasoning")
        effort = ((r.get("effort") if isinstance(r, dict)
                   else r if isinstance(r, str) else None)
                  or body.get("reasoning_effort")
                  or models_mod.auto_effort(name))
        return models_mod.apply_effort(uid, effort)

    def build_request(body, model, acct, skey=None):
        system, prompts = map_messages(body.get("messages"))
        tools = map_tools(body.get("tools"))
        if body.get("tool_choice") == "none":
            tools = []
        jwt = upstream.get_user_jwt(app.state.http, acct.api_server_url,
                                    acct.token)
        max_tokens = (body.get("max_tokens") or body.get("max_output_tokens")
                      or body.get("max_completion_tokens"))
        cap = models_mod.max_output_for(model)
        if cap:
            max_tokens = min(max_tokens or cap, cap)
        return upstream.build_request(
            acct.token, jwt, model, system, prompts, tools,
            max_tokens=max_tokens,
            temperature=body.get("temperature"), top_p=body.get("top_p"),
            **conv_ids(skey))

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
                             models=want,
                             remote=models_mod.served_by(want))
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
                req = build_request(body, model, acct, skey=session_key)
            except Exception as e:
                st = getattr(getattr(e, "response", None), "status_code",
                             None)
                last_err = {"message": f"{acct.display()}: {e}",
                            "http_error": st or 502,
                            "kind": "build_error",
                            # a real 5xx from the auth endpoint is provider-
                            # side trouble like a chat-path 5xx; transport
                            # failures (no response) stay hard
                            "transient": bool(st and st >= 500)}
                if log:
                    log.ev("build_error", account=acct.display(),
                           error=str(e)[:500], status=st,
                           tb=traceback.format_exc()[-3000:])
                pool.release(acct)
                pool.mark_fail(acct, last_err)
                continue
            cap_att = None
            if log:
                req_pb = req.SerializeToString()
                sent = {t.name for t in req.tools}
                dropped = [{"name": (t.get("function") or {}).get("name")
                                   or t.get("name"),
                            "type": t.get("type"),
                            "n_sub": len(t.get("tools") or []) or None}
                           for t in (body.get("tools") or [])
                           if isinstance(t, dict)
                           and ((t.get("function") or {}).get("name")
                                or t.get("name")) not in sent]
                src = {v: k for k, v in upstream.SOURCE.items()}
                # SOURCE aliases system->user(1), but system prompts never
                # land in chat_message_prompts (they go to req.prompt)
                src[upstream.SOURCE["user"]] = "user"
                log.ev("built", model=model, server=acct.api_server_url,
                       msgs=[{"r": src.get(p.source, p.source),
                              "c": len(p.prompt or ""),
                              "tc": len(p.tool_calls),
                              "img": len(p.images)}
                             for p in req.chat_message_prompts[:200]],
                       sys_chars=len(req.prompt or ""),
                       n_tools=len(req.tools),
                       tools=[t.name for t in req.tools][:100],
                       dropped_tools=dropped[:100] or None,
                       max_tokens=req.completion_config.max_tokens,
                       req_bytes=len(req_pb),
                       cascade_id=req.cascade_id,
                       session_id=req.metadata.session_id or None)
                # full wire capture: the exact protobuf we POST upstream —
                # but metadata.api_key/user_jwt are credentials, so capture
                # a redacted copy (fields present, values "***")
                try:
                    cap_req = proto.GetChatMessageRequest()
                    cap_req.CopyFrom(req)
                    cap_req.metadata.api_key = "***"
                    if cap_req.metadata.user_jwt:
                        cap_req.metadata.user_jwt = "***"
                    cap_att = log.cap_attempt({
                        "attempt": attempt, "account": acct.display(),
                        "server": acct.api_server_url, "model": model,
                        "request_pb_b64": base64.b64encode(
                            cap_req.SerializeToString()).decode(),
                        "redacted": "metadata.api_key/user_jwt"})
                except Exception:
                    cap_att = None
                try:
                    from google.protobuf.json_format import MessageToDict
                    if cap_att is not None:
                        cap_att["request"] = MessageToDict(cap_req)
                except Exception:
                    pass
            got_content, saw_stop, terminal, buf = False, False, None, []
            desc_sanitized = False
            while True:
                try:
                    for ev in upstream.stream_chat(
                            app.state.http, acct.api_server_url, acct.token,
                            req,
                            capture=(lambda k, d: log.cap_frame(cap_att, k, d))
                            if cap_att is not None else None):
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
                            terminal = ev       # buffered: retry next account
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
                               error=repr(e)[:800],
                               tb=traceback.format_exc()[-3000:])
                    if got_content and stream:
                        pool.mark_fail(acct, e2)
                        yield acct, e2
                        return
                    terminal = e2
                finally:
                    pool.release(acct)
                # upstream rejects requests whose tool descriptions carry
                # verbatim text from its internal tool/prompt corpus, reported
                # as "MCP configuration issue". Retry once on the same account
                # with homoglyph-sanitized descriptions — request-scoped, so
                # failover to another account would not help anyway.
                if (terminal is not None
                        and terminal.get("kind") == "upstream_error"
                        and "MCP configuration issue" in
                        str(terminal.get("message", ""))
                        and len(req.tools) and not desc_sanitized):
                    desc_sanitized = True
                    got_content, saw_stop, terminal, buf = (
                        False, False, None, [])
                    for t in req.tools:
                        t.description = upstream.sanitize_tool_desc(
                            t.description)
                    if log:
                        log.flag("desc_sanitized")
                        log.ev("mcp_retry", account=acct.display(),
                               n_tools=len(req.tools))
                        try:
                            cap_req2 = proto.GetChatMessageRequest()
                            cap_req2.CopyFrom(req)
                            cap_req2.metadata.api_key = "***"
                            if cap_req2.metadata.user_jwt:
                                cap_req2.metadata.user_jwt = "***"
                            cap_att = log.cap_attempt({
                                "attempt": attempt, "account": acct.display(),
                                "server": acct.api_server_url, "model": model,
                                "sanitized_desc": True,
                                "request_pb_b64": base64.b64encode(
                                    cap_req2.SerializeToString()).decode(),
                                "redacted": "metadata.api_key/user_jwt"})
                            from google.protobuf.json_format import (
                                MessageToDict)
                            cap_att["request"] = MessageToDict(cap_req2)
                        except Exception:
                            cap_att = None
                    continue
                break
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
        # upstream input_tokens is the UNCACHED count; OpenAI semantics put
        # cache reads inside prompt_tokens with a details breakdown
        inp = u.get("prompt_tokens", 0) + u.get("cached_tokens", 0)
        out = {"prompt_tokens": inp,
               "completion_tokens": u.get("completion_tokens", 0),
               "total_tokens": inp + u.get("completion_tokens", 0)}
        if u.get("cached_tokens"):
            out["prompt_tokens_details"] = {
                "cached_tokens": u["cached_tokens"]}
        return out

    # ---------- endpoints ----------

    def _variant_obj(m, created):
        return {
            "id": m["uid"], "object": "model", "created": created,
            "owned_by": m["vendor"], "display_name": m["label"],
            "family": m["family"], "effort": m["effort"],
            "context_window": m["context"],
            "max_output_tokens": m["max_output"],
            "credit_cost": m["credit"],
            "cost_summary": m.get("cost_summary"),
            "alias": m.get("alias"),
            "capabilities": {"vision": bool(m["images"]),
                             "thinking": bool(m["thinking"]),
                             "tools": True},
            "source": ("remote" if m["remote_accounts"]
                       else "url" if m["url"] else "builtin")}

    @app.get("/v1/models", dependencies=[Depends(check_key)])
    def list_models(request: Request):
        """One entry per model family (id = family name; the effort is
        picked via reasoning.effort). ?variants=1 lists every variant
        uid instead."""
        allowed = _key_models(request)
        models_mod.maybe_refresh(app.state.http, pool)
        created = int(models_mod.sync_info()["ts"] or 0)
        full = request.query_params.get("variants") in ("1", "true", "all")
        data = []
        for f in models_mod.grouped():
            if allowed is not None and not (
                    {f["prefix"]} | {m["uid"] for m in f["models"]}
            ) & allowed:
                continue
            if full:
                data.extend(_variant_obj(m, created) for m in f["models"])
                continue
            want = f.get("default") or "medium"
            main = (next((m for m in f["models"]
                          if m["effort"] == want), None)
                    or next((m for m in f["models"]
                             if m["effort"] == "medium"), None)
                    or f["models"][0])
            o = _variant_obj(main, created)
            o["id"] = f["prefix"]
            o["display_name"] = f["label"]
            o["effort"] = None
            o["default_effort"] = f.get("default")
            o["efforts"] = [m["effort"] for m in f["models"]
                            if m["effort"]]
            data.append(o)
        for a, t in models_mod.aliases().items():
            if allowed is None or a in allowed:
                data.append({"id": a, "object": "model", "created": 0,
                             "owned_by": "proxy", "alias_of": t})
        return {"object": "list", "data": data}

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.post("/v1/chat/completions", dependencies=[Depends(check_key)])
    async def chat(request: Request):
        t0 = time.perf_counter()
        raw = await request.body()
        try:
            body = json.loads(raw)
        except Exception as e:
            rl = reqlog_for(request, raw=raw)
            rl.ev("parse_error", error=str(e)[:300])
            _record(request, {}, "", False, 400, f"invalid JSON body: {e}",
                    None, t0, None, endpoint="chat")
            raise HTTPException(400, f"invalid JSON body: {e}")
        if not isinstance(body, dict):
            reqlog_for(request, {"raw": str(body)[:2000]}, raw)
            _record(request, {}, "", False, 400, "body must be a JSON object",
                    None, t0, None, endpoint="chat")
            raise HTTPException(400, "request body must be a JSON object")
        reqlog_for(request, body, raw)
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
            if len(ev.usage):
                usage = {**(usage or {}), **upstream.extract_usage(ev)}
            ui = upstream.upstream_info(ev)
            if ui:
                usage = {**(usage or {}), **ui}
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
                if len(ev.usage):
                    usage = {**(usage or {}), **upstream.extract_usage(ev)}
                ui = upstream.upstream_info(ev)
                if ui:
                    usage = {**(usage or {}), **ui}
        except GeneratorExit:
            rl.flag("client_aborted")
            rl.ev("client_aborted")
            _record(request, body, model, False, 499,
                    "client disconnected mid-stream", usage, t0, ttft,
                    account=acct_name, endpoint="chat")
            raise
        except Exception as e:
            err = {"kind": "proxy_error", "message": str(e)}
            rl.ev("proxy_exception", error=repr(e)[:800],
                  tb=traceback.format_exc()[-3000:])
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
        raw = await request.body()
        try:
            body = json.loads(raw)
        except Exception as e:
            rl = reqlog_for(request, raw=raw)
            rl.ev("parse_error", error=str(e)[:300])
            _record(request, {}, "", False, 400, f"invalid JSON body: {e}",
                    None, t0, None, endpoint="responses")
            raise HTTPException(400, f"invalid JSON body: {e}")
        if not isinstance(body, dict):
            reqlog_for(request, {"raw": str(body)[:2000]}, raw)
            _record(request, {}, "", False, 400, "body must be a JSON object",
                    None, t0, None, endpoint="responses")
            raise HTTPException(400, "request body must be a JSON object")
        reqlog_for(request, body, raw)
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
