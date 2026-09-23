"""Diagnostic-bundle helpers: the capture decoder shipped inside the
zip, plus the streaming zip writer used by /admin/api/export."""
import gzip
import io


_DECODER = '''\
"""Decode a request capture from a devin-proxy diagnostic bundle.
Usage: python decode_capture.py captures/req-<id>.json
(proto_schema.py must sit next to this script.)"""
import base64, json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from proto_schema import GetChatMessageRequest, GetChatMessageResponse

cap = json.load(open(sys.argv[1], encoding="utf-8"))
inb = cap.get("inbound") or {}
print("inbound:", inb.get("method"), inb.get("url"))
body = base64.b64decode(inb.get("body_b64") or "")
print("  body:", body[:500])
for att in cap.get("attempts") or []:
    req = GetChatMessageRequest()
    req.ParseFromString(base64.b64decode(att["request_pb_b64"]))
    print(f"--- attempt {att.get('attempt')} acct={att.get('account')} "
          f"server={att.get('server')} model={req.chat_model_uid} "
          f"tools={[t.name for t in req.tools]} "
          f"prompts={len(req.chat_message_prompts)}")
    for f in att.get("frames") or []:
        raw = base64.b64decode(f["b64"])
        if f["k"] == "frame":
            m = GetChatMessageResponse(); m.ParseFromString(raw)
            chunk = m.delta_text or m.delta_thinking or ""
            print("  frame:", repr(chunk[:120]),
                  "stop=%s" % m.stop_reason if m.stop_reason else "")
        else:
            print(f"  {f['k']}:", raw[:400])
for i, s in enumerate(cap.get("downstream") or []):
    print(f"down[{i}]:", s[:200])
'''


class _ZipStreamer(io.RawIOBase):
    """File-like object that yields zip bytes as the archive is written.

    zipfile needs a seekable target for the central directory, so we stream
    into a temp file in chunks and yield as we go — peak memory stays bounded
    to one chunk instead of holding the whole archive in RAM."""

    def __init__(self, writer):
        self._writer = writer          # callable(fileobj) — writes the zip

    def readable(self):
        return True

    def __iter__(self):
        import tempfile
        with tempfile.TemporaryFile() as f:
            self._writer(f)
            f.seek(0)
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    return
                yield chunk
