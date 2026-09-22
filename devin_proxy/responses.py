"""OpenAI Responses API (/v1/responses): request mapping + SSE events.

Translates a Responses request into the same chat-completions pipeline used by
/v1/chat/completions (same failover/session logic), then translates the
upstream event stream back into Responses-API objects and SSE events.

Server-side state: every completed response is persisted (store.responses)
with its input+output items, so `previous_response_id` chains rebuild full
context — and inherit the pinned account of the chain.
"""
import json
import time
import uuid

from fastapi.responses import JSONResponse, StreamingResponse

from . import store, upstream
from .app import ToolAgg


def _iid(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def _err(status, message, type_="invalid_request_error", code=None):
    return JSONResponse(
        {"error": {"message": message, "type": type_, "code": code}},
        status_code=status)


# ---------- request mapping ----------

def normalize_input(inp):
    """`input` (str | items) -> list of item dicts."""
    if inp is None:
        return []
    if isinstance(inp, str):
        return [{"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": inp}]}]
    out = []
    for it in inp:
        if isinstance(it, dict):
            out.append(it)
    return out


def _resolve_refs(items, index):
    out = []
    for it in items:
        if it.get("type") == "item_reference":
            ref = index.get(it.get("id"))
            if ref:
                out.append(ref)
            continue
        out.append(it)
    return out


def items_to_messages(items):
    """Responses items -> chat-style messages (reuses app.map_messages)."""
    msgs = []
    for it in items:
        t = it.get("type") or ("message" if it.get("role") else None)
        if t == "message":
            role = it.get("role", "user")
            content = it.get("content")
            if isinstance(content, list):
                parts = []
                for p in content:
                    if not isinstance(p, dict):
                        continue
                    pt = p.get("type")
                    if pt in ("input_text", "output_text", "text"):
                        parts.append({"type": "text",
                                      "text": p.get("text") or ""})
                    elif pt in ("input_image", "image_url"):
                        url = p.get("image_url")
                        if isinstance(url, dict):
                            url = url.get("url", "")
                        if url:
                            parts.append({"type": "image_url",
                                          "image_url": {"url": url}})
                    elif pt == "input_file":
                        name = p.get("filename") or "file"
                        data = p.get("file_data") or ""
                        note = data if data.startswith("data:") is False else ""
                        parts.append({"type": "text",
                                      "text": note or f"[file: {name}]"})
                content = parts
            msgs.append({"role": role, "content": content})
        elif t == "function_call":
            msgs.append({"role": "assistant", "content": None, "tool_calls": [{
                "id": it.get("call_id") or it.get("id") or "",
                "type": "function",
                "function": {"name": it.get("name") or "",
                             "arguments": it.get("arguments") or ""}}]})
        elif t == "function_call_output":
            out = it.get("output")
            if isinstance(out, list):
                out = "\n".join(str(p.get("text") or "") for p in out
                                if isinstance(p, dict))
            msgs.append({"role": "tool",
                         "tool_call_id": it.get("call_id") or it.get("id") or "",
                         "content": out if out is not None else ""})
        elif t == "reasoning":
            text = "\n".join(
                s.get("text") or "" for s in it.get("summary") or []
                if isinstance(s, dict))
            text += "\n" + "\n".join(
                c.get("text") or "" for c in it.get("content") or []
                if isinstance(c, dict))
            msgs.append({"role": "assistant", "content": "",
                         "reasoning_content": text.strip() or None})
    return msgs


def _norm_tools(tools):
    out = []
    for t in tools or []:
        if t.get("type") == "function":
            out.append({"type": "function", "function": {
                "name": t.get("name") or "",
                "description": t.get("description") or "",
                "parameters": t.get("parameters") or {}}})
    return out


def _norm_tool_choice(tc):
    if not tc or isinstance(tc, str):
        return tc
    if tc.get("type") == "function":
        return {"type": "function",
                "function": {"name": tc.get("name") or ""}}
    return "auto"


def to_chat_body(body):
    """Responses request -> chat-completions-shaped body + chain context."""
    prev_items, skey = [], None
    prev_id = body.get("previous_response_id")
    if prev_id:
        row = store.get_response(prev_id)
        if not row:
            raise ValueError(f"Previous response '{prev_id}' not found")
        prev_items = json.loads(row["items_json"])
        skey = row["session_key"]
    new_items = normalize_input(body.get("input"))
    index = {i.get("id"): i for i in prev_items + new_items if i.get("id")}
    items = _resolve_refs(prev_items + new_items, index)
    messages = items_to_messages(items)
    if body.get("instructions"):
        messages.insert(0, {"role": "developer",
                            "content": body["instructions"]})
    chat = {
        "model": body.get("model"),
        "messages": messages,
        "tools": _norm_tools(body.get("tools")),
        "tool_choice": _norm_tool_choice(body.get("tool_choice")),
        "stream": body.get("stream"),
        "temperature": body.get("temperature"),
        "top_p": body.get("top_p"),
        "max_tokens": body.get("max_output_tokens"),
        "reasoning": body.get("reasoning"),
        "user": body.get("user"),
        "session_id": body.get("session_id"),
        "metadata": body.get("metadata"),
    }
    conv = body.get("conversation")
    if isinstance(conv, dict):
        conv = conv.get("id")
    if conv:
        skey = skey or f"conv:{conv}"
    return chat, items, skey


# ---------- response object ----------

def _usage_obj(u):
    u = u or {}
    return {
        "input_tokens": u.get("prompt_tokens", 0),
        "output_tokens": u.get("completion_tokens", 0),
        "total_tokens": u.get("prompt_tokens", 0)
        + u.get("completion_tokens", 0),
        "input_tokens_details": {"cached_tokens": u.get("cached_tokens", 0)},
        "output_tokens_details": {"reasoning_tokens": 0},
    }


def output_items(text, think, agg):
    items = []
    if think:
        items.append({"id": _iid("rs"), "type": "reasoning",
                      "summary": [{"type": "summary_text", "text": think}],
                      "content": [{"type": "reasoning_text", "text": think}]})
    for cid in agg.order:
        c = agg.calls[cid]
        items.append({"id": _iid("fc"), "type": "function_call",
                      "call_id": c["id"], "name": c["name"],
                      "arguments": c["args"], "status": "completed"})
    if text or not agg.order:
        items.append({"id": _iid("msg"), "type": "message", "role": "assistant",
                      "status": "completed",
                      "content": [{"type": "output_text", "text": text,
                                   "annotations": []}]})
    return items


def response_object(rid, model, body, items_out, usage, status="completed",
                    err=None):
    return {
        "id": rid, "object": "response", "created_at": int(time.time()),
        "status": status, "error": err, "incomplete_details": None,
        "instructions": body.get("instructions"), "model": model,
        "output": items_out, "tools": body.get("tools") or [],
        "tool_choice": body.get("tool_choice") or "auto",
        "parallel_tool_calls": body.get("parallel_tool_calls", True),
        "previous_response_id": body.get("previous_response_id"),
        "reasoning": body.get("reasoning") or {"effort": None},
        "store": body.get("store") is not False,
        "temperature": body.get("temperature"),
        "top_p": body.get("top_p"),
        "max_output_tokens": body.get("max_output_tokens"),
        "text": body.get("text") or {"format": {"type": "text"}},
        "truncation": body.get("truncation") or "disabled",
        "usage": _usage_obj(usage),
        "metadata": body.get("metadata") or {},
        "user": body.get("user"),
    }


# ---------- handler ----------

def handle(request, body, t0, *, iter_chat, resolve_model, record):
    try:
        chat_body, items, chain_skey = to_chat_body(body)
    except ValueError as e:
        return _err(400, str(e), code="previous_response_not_found")
    model = resolve_model(chat_body)
    conv = body.get("conversation")
    if isinstance(conv, dict):
        conv = conv.get("id")
    skey = (chain_skey
            or (f"conv:{conv}" if conv else None)
            or (f"resp:{body['previous_response_id']}"
                if body.get("previous_response_id") else None)
            or _fingerprint(messages_of(chat_body))
            or (f"u:{body['user']}" if body.get("user") else None))
    rid = _iid("resp")
    if body.get("stream"):
        return StreamingResponse(
            _sse(request, body, chat_body, model, skey, rid, items,
                 iter_chat, record, t0),
            media_type="text/event-stream")
    return _collect(request, body, chat_body, model, skey, rid, items,
                    iter_chat, record, t0)


def messages_of(chat_body):
    return chat_body.get("messages") or []


def _fingerprint(msgs):
    import hashlib
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
    return "h:" + hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


def _persist(body, rid, model, status, acct_name, skey, items_in, items_out,
             resp):
    if body.get("store") is False:
        return
    try:
        store.save_response(
            rid, model, status, acct_name, skey,
            json.dumps(items_in + items_out, ensure_ascii=False),
            json.dumps(resp, ensure_ascii=False))
    except Exception:
        pass


def _collect(request, body, chat_body, model, skey, rid, items_in,
             iter_chat, record, t0):
    texts, think, agg = [], [], ToolAgg()
    usage, err, acct_name = None, None, None
    for acct, ev in iter_chat(chat_body, skey):
        if acct:
            acct_name = acct.display()
        if isinstance(ev, dict):
            err = ev
            break
        if ev.delta_text:
            texts.append(ev.delta_text)
        if ev.delta_thinking:
            think.append(ev.delta_thinking)
        for tc in ev.delta_tool_calls:
            agg.feed(tc)
        if ev.HasField("usage"):
            usage = upstream.extract_usage(ev)
    if err:
        status = err.get("http_error", 502)
        record(request, body, model, False, status, err.get("message"),
               usage, t0, None, account=acct_name, endpoint="responses")
        return _err(status if status < 600 else 502,
                    err.get("message", "upstream error"), type_="server_error")
    items_out = output_items("".join(texts), "".join(think), agg)
    resp = response_object(rid, model, body, items_out, usage)
    _persist(body, rid, model, "completed", acct_name, skey,
             items_in, items_out, resp)
    record(request, body, model, True, 200, None, usage, t0, None,
           account=acct_name, endpoint="responses")
    return JSONResponse(resp)


class _Emit:
    """Responses-API SSE event emitter."""

    def __init__(self):
        self.seq = 0

    def ev(self, type_, **kw):
        self.seq += 1
        return "data: " + json.dumps(
            {"type": type_, "sequence_number": self.seq, **kw},
            ensure_ascii=False) + "\n\n"


def _sse(request, body, chat_body, model, skey, rid, items_in,
         iter_chat, record, t0):
    em = _Emit()
    agg, usage, err = ToolAgg(), None, None
    acct_name, ttft = None, None
    base = response_object(rid, model, body, [], None, status="in_progress")
    text_parts, think_parts = [], []

    # open-item state
    out_idx = -1
    open_kind = None          # 'reasoning' | 'message' | 'fc'
    msg_id = _iid("msg")
    rs_id = _iid("rs")
    fc_open = None            # {"id","call_id","name"}
    fc_items = []

    def close_open():
        nonlocal open_kind
        if open_kind == "reasoning":
            t = "".join(think_parts)
            yield em.ev("response.reasoning_summary_text.done",
                        item_id=rs_id, output_index=out_idx,
                        summary_index=0, text=t)
            yield em.ev("response.reasoning_summary_part.done",
                        item_id=rs_id, output_index=out_idx,
                        summary_index=0,
                        part={"type": "summary_text", "text": t})
            yield em.ev("response.output_item.done", output_index=out_idx,
                        item={"id": rs_id, "type": "reasoning",
                              "summary": [{"type": "summary_text", "text": t}]})
        elif open_kind == "message":
            t = "".join(text_parts)
            yield em.ev("response.output_text.done", item_id=msg_id,
                        output_index=out_idx, content_index=0, text=t)
            yield em.ev("response.content_part.done", item_id=msg_id,
                        output_index=out_idx, content_index=0,
                        part={"type": "output_text", "text": t,
                              "annotations": []})
            yield em.ev("response.output_item.done", output_index=out_idx,
                        item={"id": msg_id, "type": "message",
                              "role": "assistant", "status": "completed",
                              "content": [{"type": "output_text", "text": t,
                                           "annotations": []}]})
        elif open_kind == "fc" and fc_open:
            c = agg.calls.get(fc_open["call_id"],
                              {"name": fc_open["name"], "args": ""})
            yield em.ev("response.function_call_arguments.done",
                        item_id=fc_open["id"], output_index=out_idx,
                        arguments=c["args"])
            item = {"id": fc_open["id"], "type": "function_call",
                    "call_id": fc_open["call_id"], "name": c["name"],
                    "arguments": c["args"], "status": "completed"}
            fc_items.append(item)
            yield em.ev("response.output_item.done", output_index=out_idx,
                        item=item)
        open_kind = None

    def open_reasoning():
        nonlocal out_idx, open_kind
        out_idx += 1
        open_kind = "reasoning"
        yield em.ev("response.output_item.added", output_index=out_idx,
                    item={"id": rs_id, "type": "reasoning", "summary": []})
        yield em.ev("response.reasoning_summary_part.added", item_id=rs_id,
                    output_index=out_idx, summary_index=0,
                    part={"type": "summary_text", "text": ""})

    def open_message():
        nonlocal out_idx, open_kind
        out_idx += 1
        open_kind = "message"
        yield em.ev("response.output_item.added", output_index=out_idx,
                    item={"id": msg_id, "type": "message", "role": "assistant",
                          "status": "in_progress", "content": []})
        yield em.ev("response.content_part.added", item_id=msg_id,
                    output_index=out_idx, content_index=0,
                    part={"type": "output_text", "text": "", "annotations": []})

    def open_fc(call_id, name):
        nonlocal out_idx, open_kind, fc_open
        out_idx += 1
        open_kind = "fc"
        fc_open = {"id": _iid("fc"), "call_id": call_id, "name": name}
        yield em.ev("response.output_item.added", output_index=out_idx,
                    item={"id": fc_open["id"], "type": "function_call",
                          "call_id": call_id, "name": name,
                          "arguments": "", "status": "in_progress"})

    yield em.ev("response.created", response=base)
    yield em.ev("response.in_progress", response=base)

    for acct, ev in iter_chat(chat_body, skey):
        if acct:
            acct_name = acct.display()
        if isinstance(ev, dict):
            err = ev
            break
        if ttft is None:
            ttft = int((time.perf_counter() - t0) * 1000)
        if ev.delta_thinking:
            if open_kind != "reasoning":
                for x in close_open():
                    yield x
                for x in open_reasoning():
                    yield x
            think_parts.append(ev.delta_thinking)
            yield em.ev("response.reasoning_summary_text.delta",
                        item_id=rs_id, output_index=out_idx, summary_index=0,
                        delta=ev.delta_thinking)
        if ev.delta_text:
            if open_kind != "message":
                for x in close_open():
                    yield x
                for x in open_message():
                    yield x
            text_parts.append(ev.delta_text)
            yield em.ev("response.output_text.delta", item_id=msg_id,
                        output_index=out_idx, content_index=0,
                        delta=ev.delta_text)
        for tc in ev.delta_tool_calls:
            fed = agg.feed(tc)
            if not fed:
                continue
            idx, call, new_name, delta = fed
            if new_name or (fc_open is None and call["id"]):
                if not (fc_open and fc_open["call_id"] == call["id"]):
                    for x in close_open():
                        yield x
                    for x in open_fc(call["id"], call["name"]):
                        yield x
            if delta and fc_open:
                yield em.ev("response.function_call_arguments.delta",
                            item_id=fc_open["id"], output_index=out_idx,
                            delta=delta)
        if ev.HasField("usage"):
            usage = upstream.extract_usage(ev)

    for x in close_open():
        yield x

    if err:
        status = err.get("http_error", 502)
        msg = err.get("message", "upstream error")
        resp = response_object(rid, model, body, [], usage, status="failed",
                               err={"code": "server_error", "message": msg})
        yield em.ev("response.failed", response=resp)
        yield em.ev("error", code="server_error", message=msg)
        record(request, body, model, False, status, msg, usage, t0, ttft,
               account=acct_name, endpoint="responses")
    else:
        items_out = output_items("".join(text_parts), "".join(think_parts), agg)
        # keep generated ids stable with what we streamed
        k = 0
        for it in items_out:
            if it["type"] == "reasoning":
                it["id"] = rs_id
            elif it["type"] == "message":
                it["id"] = msg_id
            elif it["type"] == "function_call" and k < len(fc_items):
                it.update(fc_items[k])
                k += 1
        resp = response_object(rid, model, body, items_out, usage)
        _persist(body, rid, model, "completed", acct_name, skey,
                 items_in, items_out, resp)
        record(request, body, model, True, 200, None, usage, t0, ttft,
               account=acct_name, endpoint="responses")
        yield em.ev("response.completed", response=resp)
    yield "data: [DONE]\n\n"
