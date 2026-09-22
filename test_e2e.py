"""E2E smoke checks against a running devin-proxy.

Usage: DEVIN_PROXY_BASE=http://127.0.0.1:8317 DEVIN_PROXY_KEY=sk-... python test_e2e.py
"""
import json
import os
import urllib.request

BASE = os.environ.get("DEVIN_PROXY_BASE", "http://127.0.0.1:8317")
KEY = os.environ["DEVIN_PROXY_KEY"]
H = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}


def post(path, body):
    req = urllib.request.Request(BASE + path, json.dumps(body).encode(), H)
    return json.load(urllib.request.urlopen(req, timeout=120))


def get(path):
    req = urllib.request.Request(BASE + path, headers=H)
    return json.load(urllib.request.urlopen(req, timeout=30))


def sse(path, body):
    req = urllib.request.Request(BASE + path, json.dumps(body).encode(), H)
    events = []
    for line in urllib.request.urlopen(req, timeout=120):
        line = line.decode().strip()
        if line.startswith("data: ") and line != "data: [DONE]":
            events.append(json.loads(line[6:]))
    return events


# 1. tool call -> function_call_output round trip via previous_response_id
r1 = post("/v1/responses", {
    "model": "claude",
    "input": "weather in Tokyo? use the tool",
    "tools": [{"type": "function", "name": "get_weather",
               "description": "Get weather",
               "parameters": {"type": "object",
                              "properties": {"city": {"type": "string"}},
                              "required": ["city"]}}]})
fc = next(i for i in r1["output"] if i["type"] == "function_call")
print("R1:", r1["id"], "fc:", fc["name"], fc["arguments"])

r2 = post("/v1/responses", {
    "model": "claude",
    "previous_response_id": r1["id"],
    "input": [{"type": "function_call_output", "call_id": fc["call_id"],
               "output": '{"temp":22,"cond":"sunny"}'}]})
print("R2:", r2["id"], "->",
      [i["content"][0]["text"][:80] for i in r2["output"]
       if i["type"] == "message"])

# 2. streaming with tools -> function_call SSE events
ev = sse("/v1/responses", {
    "model": "claude", "stream": True,
    "input": "weather in Rome? use the tool",
    "tools": [{"type": "function", "name": "get_weather",
               "description": "Get weather",
               "parameters": {"type": "object",
                              "properties": {"city": {"type": "string"}},
                              "required": ["city"]}}]})
types = [e["type"] for e in ev]
print("stream events:", [t for t in types if "function" in t or t.endswith("done")])
final = ev[-1]["response"]
print("final output:", [i["type"] for i in final["output"]])

# 3. input_items + delete
items = get(f"/v1/responses/{r1['id']}/input_items")
print("input_items:", [i.get("type") for i in items["data"]])
d = json.load(urllib.request.urlopen(urllib.request.Request(
    f"{BASE}/v1/responses/{r1['id']}", headers=H, method="DELETE")))
print("delete:", d)

# 4. error paths
try:
    post("/v1/responses", {"model": "claude", "input": "x",
                           "previous_response_id": "resp_missing"})
except urllib.error.HTTPError as e:
    print("missing prev ->", e.code, e.read()[:120])
