import os, tempfile, json
os.environ["DEVIN_PROXY_DB"] = tempfile.mktemp(suffix=".db")
from devin_proxy import proto
from devin_proxy.app import ReqLog, _msg_summary

m = proto.GetChatMessageResponse()
tc = m.delta_tool_calls.add()
tc.id = "c1"; tc.name = "exec"; tc.arguments = '{"cmd":"ls"}'
m.stop_reason = 10
s = _msg_summary(m)
assert s["tool_calls"][0]["name"] == "exec" and s["stop"] == 10, s
print("tool_call summary:", json.dumps(s, ensure_ascii=False))

u = proto.GetChatMessageResponse()
rep = u.usage.add(); rep.title = "Token Usage"
mt = rep.metrics.add(); mt.metric = "input_tokens"; mt.value.v = 1234.0
mt = rep.metrics.add(); mt.metric = "cached_input_tokens"; mt.value.v = 512.0
rep2 = u.usage.add(); rep2.title = "Response Statistics"
mt = rep2.metrics.add(); mt.metric = "model"; mt.text.s = "Claude Sonnet 5 Medium"
u.info.model_uid = "claude-sonnet-5-medium"; u.info.msg_id = "msg_x"
h = u.info.headers.add(); h.name = "Request-Id"; h.value = "req_x"
s = _msg_summary(u)
assert s["usage"]["prompt_tokens"] == 1234, s
assert s["usage"]["cached_tokens"] == 512, s
assert s["usage"]["model"] == "Claude Sonnet 5 Medium", s
assert s["usage"]["upstream_req_id"] == "req_x", s
assert s["upstream"]["msg"] == "msg_x", s
print("usage summary:", json.dumps(s))

rl = ReqLog({"model": "x", "messages": [{"role": "user", "content": "hi"}]})
rl.ev("msg", text="A" * 20000)
rl.out("data: " + "B" * 20000 + "\n\n")
e = rl.events[-1]
assert e.get("data_truncated") and len(e["text"]) < 5000
assert len(rl.sse[0]) <= 13000
rl.flag("truncated"); rl.flag("retried")
assert rl.flags_str() == "retried,truncated"
assert json.loads(rl.events_dump()) and json.loads(rl.sse_dump())
print("ReqLog caps + flags OK")

# cap reached
import devin_proxy.app as A
A._LOG_CAP = 1000
rl2 = ReqLog()
for i in range(50):
    rl2.ev("msg", text="x" * 300)
assert rl2._ev_full and rl2.events[-1]["t"] == "log_cap"
print("cap reached -> log_cap marker OK")
print("ALL OK")
