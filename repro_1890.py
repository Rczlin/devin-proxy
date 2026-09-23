"""Replay req-1890's captured upstream protobufs against server.codeium.com.

Usage: python repro_1890.py <control|orig|san|both> [account_id]

  control — minimal 1-message request, no tools (account/model sanity check)
  orig    — captured attempt-0 pb verbatim (original tool descriptions)
  san     — captured attempt-1 pb verbatim (homoglyph-sanitized descriptions)
  both    — orig then san

Credentials are patched into metadata (api_key + user_jwt) from the local
proxy DB; everything else on the wire is byte-identical to the capture.
"""
import base64
import json
import sqlite3
import sys
import time

sys.path.insert(0, r"L:\C\devin-proxy")
import httpx  # noqa: E402
from devin_proxy import proto, upstream  # noqa: E402

CAP_PATH = r"L:\Download\req-1890-capture.json"
DB = r"C:\Users\22974\AppData\Roaming\devin-proxy\devin-proxy.db"
SERVER = "https://server.codeium.com"


def get_token(account_id=None):
    con = sqlite3.connect(DB)
    try:
        if account_id:
            row = con.execute(
                "select token from accounts where id=?", (account_id,)
            ).fetchone()
        else:
            row = con.execute(
                "select token from accounts where disabled=0 order by id "
                "limit 1").fetchone()
    finally:
        con.close()
    if not row:
        raise SystemExit("no account in db")
    return row[0]


def run(name, req, client, token, max_events=40):
    print(f"\n===== {name} =====", flush=True)
    t0 = time.time()
    n = 0
    try:
        for ev in upstream.stream_chat(client, SERVER, token, req):
            n += 1
            if isinstance(ev, dict):
                print(f"[{time.time()-t0:6.1f}s] ERROR {json.dumps(ev)[:1200]}",
                      flush=True)
                return ev
            bits = []
            if ev.delta_text:
                bits.append(f"text={ev.delta_text[:80]!r}")
            if ev.delta_thinking:
                bits.append(f"think={ev.delta_thinking[:60]!r}")
            if ev.delta_tool_calls:
                bits.append("tools=" + ",".join(
                    f"{t.name}({len(t.arguments)}b)"
                    for t in ev.delta_tool_calls))
            if ev.HasField("info"):
                bits.append(f"info(model={ev.info.model_uid})")
            if ev.stop_reason:
                bits.append(f"stop={ev.stop_reason}")
            if ev.usage:
                bits.append("usage")
            print(f"[{time.time()-t0:6.1f}s] frame seq={ev.seq} "
                  + (" ".join(bits) or "(empty)"), flush=True)
            if n >= max_events:
                print("... still streaming, aborting (outcome already clear)")
                return {"kind": "streaming_ok"}
    except Exception as e:
        print(f"[{time.time()-t0:6.1f}s] EXC {e!r}", flush=True)
        return {"kind": "exception", "message": str(e)}
    print(f"[{time.time()-t0:6.1f}s] stream ended cleanly, {n} events")
    return {"kind": "ok"}


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "both"
    acct_id = int(sys.argv[2]) if len(sys.argv) > 2 else None
    token = get_token(acct_id)
    cap = json.load(open(CAP_PATH, encoding="utf-8"))
    client = httpx.Client(
        timeout=httpx.Timeout(connect=15, read=600, write=120, pool=15))
    jwt = upstream.get_user_jwt(client, SERVER, token)
    print(f"account token tail ...{token[-8:]}  jwt {len(jwt)} chars")

    def load(which):
        req = proto.GetChatMessageRequest()
        req.ParseFromString(
            base64.b64decode(cap["attempts"][which]["request_pb_b64"]))
        req.metadata.api_key = token
        req.metadata.user_jwt = jwt
        return req

    if mode in ("control", "both", "all"):
        req = upstream.build_request(
            token, jwt, "swe-2-max",
            None,
            [proto.ChatMessagePrompt(
                source=upstream.SOURCE["user"],
                prompt="Reply with the word OK.")],
            [], max_tokens=64)
        run("control: minimal no-tools request", req, client, token)
    if mode in ("orig", "both", "all"):
        run("orig: captured attempt-0 (original descriptions)",
            load(0), client, token)
    if mode in ("san", "both", "all"):
        run("san: captured attempt-1 (sanitized descriptions)",
            load(1), client, token)

    # ---- ablations (all based on the sanitized attempt-1 pb) ----
    if mode in ("san-tiny", "all"):
        req = load(1)
        del req.chat_message_prompts[:]
        req.chat_message_prompts.add(
            source=upstream.SOURCE["user"], prompt="Reply with the word OK.")
        run("san-tiny: sanitized tools + trivial convo", req, client, token)
    if mode in ("notools", "all"):
        req = load(1)
        del req.tools[:]
        run("notools: full convo, no tools", req, client, token)
    if mode in ("san-tail", "all"):
        req = load(1)
        keep = 20
        del req.chat_message_prompts[:len(req.chat_message_prompts) - keep]
        run(f"san-tail: sanitized tools + last {keep} msgs",
            req, client, token)
    if mode in ("orig-tiny", "all"):
        req = load(0)
        del req.chat_message_prompts[:]
        req.chat_message_prompts.add(
            source=upstream.SOURCE["user"], prompt="Reply with the word OK.")
        run("orig-tiny: original tools + trivial convo", req, client, token)


if __name__ == "__main__":
    main()
