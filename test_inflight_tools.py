"""InflightTracker tool_call/thinking accounting — run: python test_inflight_tools.py
Reproduces: TPS under/over-count + missing preview when the stream is
dominated by tool-call deltas instead of plain text."""
import time
from types import SimpleNamespace

from devin_proxy.app import InflightTracker


def ev(text="", think="", tool_calls=None, stop=0, usage=None):
    """Fake upstream frame matching the protobuf surface event() reads."""
    return SimpleNamespace(
        delta_text=text,
        delta_thinking=think,
        delta_tool_calls=tool_calls or [],
        stop_reason=stop,
        usage=usage or [],
    )


def tc(cid, name="", arguments=""):
    return SimpleNamespace(id=cid, name=name, arguments=arguments)


# --- 1. tool-call args count toward out_chars (CUMULATIVE frames) ----------
# Upstream sends arguments as a growing cumulative snapshot per call id
# (see ToolAgg.feed's startswith branch). Feeding three cumulative frames
# for one call must count the FINAL length once, not sum all three.
t = InflightTracker()
rid = t.register({"model": "m", "endpoint": "responses", "stream": True})
t.update(rid, first_ev_at=time.time())
for snap in ['{"a"', '{"a": 1,', '{"a": 1, "b": 2}']:
    t.event(rid, ev(tool_calls=[tc("c1", name="exec", arguments=snap)]))
row = t.snapshot()[0]
# final args = len('{"a": 1, "b": 2}') == 17 chars; name "exec" counted once
expected = len('{"a": 1, "b": 2}') + len("exec")
assert row["tool_chars"] == expected, (row["tool_chars"], expected)
assert row["out_tokens"] == round(expected / row["_ratio"]) \
    if "_ratio" in row else row["out_tokens"] > 0
print("1. tool-call cumulative chars OK ->", row["tool_chars"], "chars")

# --- 2. tool-call args appear in the output tail preview -------------------
assert '{"a": 1, "b": 2}' in row["tail"], row["tail"]
print("2. tool-call args in tail OK")

# --- 3. thinking delta goes into tail too ---------------------------------
t2 = InflightTracker()
rid2 = t2.register({"model": "m", "endpoint": "chat", "stream": True})
t2.update(rid2, first_ev_at=time.time())
t2.event(rid2, ev(think="让我想一想这个问题"))
row2 = t2.snapshot()[0]
assert "让我想一想这个问题" in row2["tail"], row2["tail"]
print("3. thinking in tail OK")

# --- 4. a pure-tool-call request reports non-zero tps ----------------------
# Before the fix, a request emitting only tool_calls had out_chars==0 ->
# out_tokens==0 -> tps=None even though real output was produced.
t3 = InflightTracker()
rid3 = t3.register({"model": "m", "endpoint": "responses", "stream": True})
e = t3._reqs[rid3]
e["first_ev_at"] = time.time()
for i in range(5):
    t3.event(rid3, ev(tool_calls=[
        tc(f"c{i}", name="exec", arguments="x" * 100)]))
e["done_at"] = e["first_ev_at"] + 5   # pretend 5s of generation
e["phase"] = "done"
row3 = t3.snapshot()[0]
assert row3["tool_chars"] == 5 * (100 + len("exec")), row3["tool_chars"]
assert row3["tps"] is not None and row3["tps"] > 0, row3["tps"]
print("4. tool-only request has tps OK ->", row3["tps"])

# --- 5. interleaved text+tool+think all land in tail -----------------------
t4 = InflightTracker()
rid4 = t4.register({"model": "m", "endpoint": "chat", "stream": True})
t4.update(rid4, first_ev_at=time.time())
t4.event(rid4, ev(text="前半句"))
t4.event(rid4, ev(think="中间想了想"))
t4.event(rid4, ev(tool_calls=[tc("c1", name="exec", arguments='{"cmd":"ls"}')]))
row4 = t4.snapshot()[0]
assert "前半句" in row4["tail"]
assert "中间想了想" in row4["tail"]
assert '{"cmd":"ls"}' in row4["tail"]
assert row4["tool_chars"] == len('{"cmd":"ls"}') + len("exec")
print("5. mixed stream tail OK")

print("ALL TOOL-CALL ACCOUNTING TESTS PASSED")
