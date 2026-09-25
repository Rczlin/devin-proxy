import os, tempfile, sqlite3

# Legacy shape: a requests table in the *log* db missing the newer columns.
# The split layout keeps requests in <db>-logs.db, so point DEVIN_PROXY_DB
# at the core path and pre-create the log db with the old schema.
db = tempfile.mktemp(suffix=".db")
logdb = db[:-3] + "-logs.db"
c = sqlite3.connect(logdb)
c.execute("""CREATE TABLE requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, model TEXT,
  resolved_model TEXT, stream INTEGER DEFAULT 0, ok INTEGER DEFAULT 1,
  status INTEGER DEFAULT 200, error TEXT, prompt_tokens INTEGER DEFAULT 0,
  completion_tokens INTEGER DEFAULT 0, latency_ms INTEGER DEFAULT 0,
  ttft_ms INTEGER, client TEXT, key_name TEXT, account TEXT, endpoint TEXT,
  messages_json TEXT)""")
c.execute("INSERT INTO requests (ts,model) VALUES (1.0,'old')")
c.commit(); c.close()

os.environ["DEVIN_PROXY_DB"] = db
from devin_proxy import store

store.log_request(model="m", resolved_model="m", stream=1, ok=0, status=502,
                  error="e", prompt_tokens=1, completion_tokens=2,
                  latency_ms=3, ttft_ms=4, client="c", key_name="k",
                  messages_json="[]", request_json="{}", events_json="[1]",
                  sse_json="[2]", flags="truncated")
row = store.get_request(2)
assert row["flags"] == "truncated" and row["events_json"] == "[1]", row
items, total = store.list_requests(10, 0)
assert total == 2 and items[0]["flags"] == "truncated", items
assert store.get_request(1)["flags"] is None        # old row: NULL, fine
assert "flags" in items[0] and "sse_json" not in items[0]  # list stays light
print("MIGRATION OK — old schema upgraded, blobs in diag db")
