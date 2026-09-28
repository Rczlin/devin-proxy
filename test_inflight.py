"""InflightTracker unit checks — run with: python test_inflight.py"""
import time
from devin_proxy.app import InflightTracker


def mk_tracker():
    return InflightTracker()


# 1. finish() keeps entry as done
t = mk_tracker()
rid = t.register({"model": "m", "endpoint": "chat", "stream": True})
t.finish(rid)
snap = t.snapshot()
assert len(snap) == 1 and snap[0]["phase"] == "done", snap
print("1. done phase kept OK")

# 2. elapsed frozen at done_at
e = t._reqs[rid]
e["done_at"] = time.time() - 5  # pretend finished 5s ago
s1 = t.snapshot()[0]["elapsed_s"]
time.sleep(0.05)
s2 = t.snapshot()[0]["elapsed_s"]
assert abs(s1 - s2) < 0.01, (s1, s2)
print("2. elapsed frozen OK")

# 3. TPS frozen after done (uses done_at - first_ev_at, not now)
e["first_ev_at"] = e["done_at"] - 2  # 2s of generation
e["text_chars"] = 200
e["out_tokens"] = 50
tps1 = t.snapshot()[0]["tps"]
time.sleep(0.1)
tps2 = t.snapshot()[0]["tps"]
assert tps1 == tps2, (tps1, tps2)
assert tps1 == 25.0, tps1  # 50 tok / 2s
print("3. tps frozen on done OK ->", tps1)

# 4. CJK-aware estimate: 300 CJK chars -> ~200 tok (ratio ~1.5), not 75
t2 = mk_tracker()
rid2 = t2.register({"model": "m", "endpoint": "chat", "stream": True})
t2.update(rid2, first_ev_at=time.time())
t2._reqs[rid2]["text_chars"] = 300
t2._reqs[rid2]["tail"] = "你好世界" * 40
row = t2.snapshot()[0]
assert row["out_tokens_est"] is True
assert row["out_tokens"] > 150, row["out_tokens"]
print("4. CJK estimate OK ->", row["out_tokens"])

# 5. pure-English tail -> ratio 4.0
t3 = mk_tracker()
rid3 = t3.register({"model": "m", "endpoint": "chat", "stream": True})
t3.update(rid3, first_ev_at=time.time())
t3._reqs[rid3]["text_chars"] = 400
t3._reqs[rid3]["tail"] = "hello world " * 30
row3 = t3.snapshot()[0]
assert row3["out_tokens"] == 100, row3["out_tokens"]
print("5. EN estimate OK ->", row3["out_tokens"])

# 6. done rows expire after TTL
e["done_at"] = time.time() - 31
assert t.snapshot() == []
print("6. done TTL expiry OK")

# 7. stall cleared on done
t4 = mk_tracker()
rid4 = t4.register({"model": "m", "endpoint": "chat", "stream": True})
t4.update(rid4, first_ev_at=time.time())
t4._reqs[rid4]["last_ev_at"] = time.time() - 20  # would be stalled
t4.finish(rid4)
row4 = t4.snapshot()[0]
assert row4["phase"] == "done" and row4["stall_s"] is None, row4
print("7. stall cleared on done OK")

print("ALL INFLIGHT TESTS PASSED")
