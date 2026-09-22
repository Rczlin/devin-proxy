"""Upstream client for the Cognition chat backend (Connect-RPC over httpx)."""
import gzip
import json
import sys
import threading
import time
import uuid

import httpx
from google.protobuf import timestamp_pb2

from . import proto

CHAT_PATH = "/exa.api_server_pb.ApiServerService/GetChatMessage"
JWT_PATH = "/exa.auth_pb.AuthService/GetUserJwt"

COMPRESSED = 0x01
END_STREAM = 0x02

STOP_REASONS = {10: "tool_calls", 11: "content_filter", 1: "length", 3: "length"}
SOURCE = {"user": 1, "assistant": 2, "tool": 4, "system": 1}

CLIENT_IDE = "devin-cli"
CLIENT_VERSION = "3000.10.31"


def build_metadata(api_key, user_jwt=""):
    ts = timestamp_pb2.Timestamp()
    ts.GetCurrentTime()
    platform = {"win32": "windows", "darwin": "darwin"}.get(sys.platform, "linux")
    return proto.Metadata(
        ide_name=CLIENT_IDE,
        ide_version=CLIENT_VERSION,
        api_key=api_key,
        locale="en",
        os=platform,
        extension_version=CLIENT_VERSION,
        request_id=int(time.time() * 1000),
        session_id=str(uuid.uuid4()),
        ide_name_2=CLIENT_IDE,
        timestamp=ts,
        user_jwt=user_jwt,
        trigger_id=str(uuid.uuid4()),
        ide_name_3="Unset",
        ide_name_4=CLIENT_IDE,
    )


_jwt_cache = {}          # (base_url, api_key) -> {"jwt": str, "exp": float}
_jwt_lock = threading.Lock()


def get_user_jwt(client, base_url, api_key, force_refresh=False):
    key = (base_url, api_key)
    with _jwt_lock:
        cached = _jwt_cache.get(key)
        if not force_refresh and cached and cached["exp"] > time.time() + 60:
            return cached["jwt"]
        req = proto.GetUserJwtRequest(metadata=build_metadata(api_key))
        resp = client.post(
            base_url + JWT_PATH,
            content=req.SerializeToString(),
            headers={"Content-Type": "application/proto",
                     "Connect-Protocol-Version": "1"},
        )
        resp.raise_for_status()
        out = proto.GetUserJwtResponse()
        out.ParseFromString(resp.content)
        if not out.jwt:
            raise RuntimeError("GetUserJwt returned no JWT")
        exp = time.time() + 600
        try:
            import base64
            payload = out.jwt.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp", exp)
        except Exception:
            pass
        _jwt_cache[key] = {"jwt": out.jwt, "exp": exp}
        return out.jwt


def clear_jwt(base_url, api_key):
    with _jwt_lock:
        _jwt_cache.pop((base_url, api_key), None)


def make_prompt(role, text="", tool_call_id=None, tool_calls=None, images=None,
                thinking=None):
    msg = proto.ChatMessagePrompt(
        source=SOURCE[role],
        prompt=text or "",
        num_tokens=max(1, len(text or "") // 4),
        is_user_input=1,
    )
    if thinking:
        msg.thinking = thinking
    if tool_call_id:
        msg.tool_call_id = tool_call_id
    for tc in tool_calls or []:
        msg.tool_calls.add(id=tc["id"], name=tc["name"],
                           arguments=tc.get("arguments", ""))
    for img in images or []:
        msg.images.add(base64_data=img["base64"], mime_type=img.get("mime", "image/png"))
    return msg


def make_tool(t):
    desc = t.get("description") or ""
    if len(desc) > 6998:
        desc = desc[:6995] + "..."
    return proto.ChatToolDefinition(
        name=t["name"], description=desc,
        parameters_json=json.dumps(t.get("parameters") or {}))


def build_request(api_key, user_jwt, model_uid, system_prompt, prompts,
                  tools=None, max_tokens=None, temperature=None, top_p=None):
    req = proto.GetChatMessageRequest(
        metadata=build_metadata(api_key, user_jwt),
        request_type=5,                     # CASCADE
        cascade_id=str(uuid.uuid4()),
        planner_mode=1,                     # DEFAULT
        chat_model_uid=model_uid,
    )
    if system_prompt:
        req.prompt = system_prompt
    req.chat_message_prompts.extend(prompts)
    req.completion_config.CopyFrom(proto.CompletionConfiguration(
        num_completions=1,
        max_tokens=max_tokens or 128000,
        max_newlines=400,
        temperature=1.0 if temperature is None else temperature,
        top_k=40,
        top_p=0.95 if top_p is None else top_p,
    ))
    for t in tools or []:
        req.tools.append(make_tool(t))
    req.trajectory_ref.CopyFrom(proto.TrajectoryReference(
        trajectory_id=str(uuid.uuid4()), f3=4, f4=14))
    return req


def _connect_frame(body):
    payload = gzip.compress(body)
    return bytes([COMPRESSED]) + len(payload).to_bytes(4, "big") + payload


def _trailer_error(payload):
    try:
        data = json.loads(payload.decode("utf-8", "replace"))
        err = data.get("error")
        if isinstance(err, dict):
            return err.get("message") or json.dumps(err)
        return str(err) if err else None
    except Exception:
        return None


def stream_chat(client, base_url, api_key, request, timeout=300):
    """POST GetChatMessage; yields GetChatMessageResponse messages, then
    dicts {'error': ...} on trailer errors."""
    headers = {
        "Content-Type": "application/connect+proto",
        "Connect-Protocol-Version": "1",
        "Connect-Content-Encoding": "gzip",
        "Connect-Accept-Encoding": "gzip",
        "Accept-Encoding": "identity",
        "User-Agent": "connect-go/1.18.1 (go1.26.3)",
        "Authorization": f"Basic {api_key}-{api_key}",
    }
    pending = b""
    with client.stream("POST", base_url + CHAT_PATH,
                       content=_connect_frame(request.SerializeToString()),
                       headers=headers, timeout=timeout) as resp:
        if resp.status_code != 200:
            body = resp.read()
            yield {"http_error": resp.status_code,
                   "message": body.decode("utf-8", "replace")[:2000]}
            return
        for chunk in resp.iter_bytes(65536):
            pending += chunk
            while len(pending) >= 5:
                flags = pending[0]
                ln = int.from_bytes(pending[1:5], "big")
                if len(pending) < 5 + ln:
                    break
                payload = pending[5:5 + ln]
                pending = pending[5 + ln:]
                raw = gzip.decompress(payload) if flags & COMPRESSED else payload
                if flags & END_STREAM:
                    err = _trailer_error(raw)
                    if err:
                        yield {"error": err}
                    continue
                msg = proto.GetChatMessageResponse()
                msg.ParseFromString(raw)
                yield msg


def extract_usage(msg):
    """GetChatMessageResponse.usage -> {prompt_tokens, completion_tokens, ...}"""
    out = {}
    for m in msg.usage.metrics:
        n = round(m.value.v)
        if m.metric == "input_tokens":
            out["prompt_tokens"] = n
        elif m.metric == "output_tokens":
            out["completion_tokens"] = n
        elif "cached" in m.metric or "cache_read" in m.metric:
            out["cached_tokens"] = n
        elif "cache_creation" in m.metric or "cache_write" in m.metric:
            out["cache_creation_tokens"] = n
    return out or None
