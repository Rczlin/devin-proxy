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
mt = u.usage.metrics.add(); mt.metric = "input_tokens"; mt.value.v = 1234.0
s = _msg_summary(u)
assert s["usage"]["prompt_tokens"] == 1234, s
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
