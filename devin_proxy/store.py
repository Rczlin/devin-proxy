"""SQLite persistence: request log, usage stats, proxy API keys."""
import hashlib
import os
import secrets
import sqlite3
import threading
import time

_DB_PATH = os.environ.get("DEVIN_PROXY_DB") or os.path.join(
    os.path.expanduser("~"), ".devin-proxy", "devin-proxy.db")
if os.name == "nt" and "DEVIN_PROXY_DB" not in os.environ:
    _DB_PATH = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                            "devin-proxy", "devin-proxy.db")

_MAX_ROWS = int(os.environ.get("DEVIN_PROXY_MAX_ROWS", "50000"))

_lock = threading.Lock()
_con = None
_insert_count = 0


def _conn():
    global _con
    if _con is None:
        os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
        _con = sqlite3.connect(_DB_PATH, check_same_thread=False)
        _con.row_factory = sqlite3.Row
        _con.execute("PRAGMA journal_mode=WAL")
        _con.execute("PRAGMA busy_timeout=5000")
        _con.executescript("""
        CREATE TABLE IF NOT EXISTS requests (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts REAL NOT NULL,
          model TEXT,
          resolved_model TEXT,
          stream INTEGER DEFAULT 0,
          ok INTEGER DEFAULT 1,
          status INTEGER DEFAULT 200,
          error TEXT,
          prompt_tokens INTEGER DEFAULT 0,
          completion_tokens INTEGER DEFAULT 0,
          latency_ms INTEGER DEFAULT 0,
          ttft_ms INTEGER,
          client TEXT,
          key_name TEXT,
          messages_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_req_ts ON requests(ts);
        CREATE INDEX IF NOT EXISTS idx_req_model ON requests(model);

        CREATE TABLE IF NOT EXISTS api_keys (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          key_hash TEXT NOT NULL UNIQUE,
          prefix TEXT,
          tail TEXT,
          created REAL NOT NULL,
          disabled INTEGER DEFAULT 0,
          last_used REAL
        );
        """)
        _migrate_keys()
        _con.commit()
    return _con


def _migrate_keys():
    """Move plaintext `keys` rows (old schema) into hashed `api_keys`."""
    tables = {r[0] for r in _con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "keys" not in tables:
        return
    for r in _con.execute("SELECT name,key,created,disabled FROM keys"):
        h = _hash(r["key"])
        _con.execute(
            "INSERT OR IGNORE INTO api_keys (name,key_hash,prefix,tail,created,disabled)"
            " VALUES (?,?,?,?,?,?)",
            (r["name"], h, r["key"][:10], r["key"][-4:], r["created"], r["disabled"]))
    _con.execute("DROP TABLE keys")


def _hash(key):
    return hashlib.sha256(key.encode()).hexdigest()


def log_request(model, resolved_model, stream, ok, status, error,
                prompt_tokens, completion_tokens, latency_ms, ttft_ms,
                client, key_name, messages_json):
    global _insert_count
    with _lock:
        _conn().execute(
            "INSERT INTO requests (ts,model,resolved_model,stream,ok,status,error,"
            "prompt_tokens,completion_tokens,latency_ms,ttft_ms,client,key_name,messages_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), model, resolved_model, int(stream), int(ok), status, error,
             prompt_tokens, completion_tokens, latency_ms, ttft_ms, client, key_name,
             messages_json))
        _insert_count += 1
        if _insert_count % 100 == 0:
            _conn().execute(
                "DELETE FROM requests WHERE id < "
                "(SELECT MIN(id) FROM (SELECT id FROM requests ORDER BY id DESC LIMIT ?))",
                (_MAX_ROWS,))
        _conn().commit()


def _where(model=None, ok=None, q=None):
    sql, args = " WHERE 1=1", []
    if model:
        sql += " AND (model=? OR resolved_model=?)"
        args += [model, model]
    if ok is not None:
        sql += " AND ok=?"
        args.append(int(ok))
    if q:
        sql += " AND (error LIKE ? OR client LIKE ? OR key_name LIKE ?)"
        args += [f"%{q}%"] * 3
    return sql, args


def list_requests(limit=50, offset=0, model=None, ok=None, q=None):
    where, args = _where(model, ok, q)
    with _lock:
        rows = _conn().execute(
            f"SELECT * FROM requests{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            args + [limit, offset]).fetchall()
        total = _conn().execute(
            f"SELECT COUNT(*) c FROM requests{where}", args).fetchone()["c"]
    return [dict(r) for r in rows], total


def get_request(rid):
    with _lock:
        r = _conn().execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
    return dict(r) if r else None


def clear_requests():
    with _lock:
        _conn().execute("DELETE FROM requests")
        _conn().commit()


def stats_overview():
    with _lock:
        c = _conn()
        now = time.time()
        today = now - (now + _tz_offset()) % 86400
        row = c.execute("""
          SELECT COUNT(*) total,
                 SUM(ok) ok_count,
                 SUM(prompt_tokens) in_tok,
                 SUM(completion_tokens) out_tok,
                 AVG(latency_ms) avg_lat,
                 AVG(ttft_ms) avg_ttft
          FROM requests""").fetchone()
        today_row = c.execute("""
          SELECT COUNT(*) total, SUM(prompt_tokens) in_tok, SUM(completion_tokens) out_tok
          FROM requests WHERE ts>=?""", (today,)).fetchone()
        by_model = c.execute("""
          SELECT COALESCE(resolved_model,model) m, COUNT(*) n,
                 SUM(prompt_tokens) in_tok, SUM(completion_tokens) out_tok,
                 AVG(latency_ms) avg_lat,
                 SUM(1-ok) errs
          FROM requests GROUP BY m ORDER BY n DESC LIMIT 20""").fetchall()
        since = time.time() - 24 * 3600
        hourly = c.execute("""
          SELECT CAST((ts - ?)/3600 AS INT) h, COUNT(*) n,
                 SUM(prompt_tokens+completion_tokens) tok, SUM(1-ok) errs
          FROM requests WHERE ts>=? GROUP BY h ORDER BY h""",
          (since, since)).fetchall()
        db_size = os.path.getsize(_DB_PATH) if os.path.exists(_DB_PATH) else 0
    return {
        "total": row["total"] or 0,
        "errors": (row["total"] or 0) - (row["ok_count"] or 0),
        "input_tokens": row["in_tok"] or 0,
        "output_tokens": row["out_tok"] or 0,
        "avg_latency_ms": round(row["avg_lat"] or 0),
        "avg_ttft_ms": round(row["avg_ttft"] or 0),
        "today": {"requests": today_row["total"] or 0,
                  "input_tokens": today_row["in_tok"] or 0,
                  "output_tokens": today_row["out_tok"] or 0},
        "by_model": [dict(r) for r in by_model],
        "hourly": [{"hour_ago": 23 - r["h"], "requests": r["n"],
                    "tokens": r["tok"] or 0, "errors": r["errs"] or 0}
                   for r in hourly],
        "db_size": db_size,
        "max_rows": _MAX_ROWS,
    }


def _tz_offset():
    return -time.timezone if not time.daylight else -time.altzone


def list_models():
    with _lock:
        rows = _conn().execute("""
          SELECT COALESCE(resolved_model,model) m, COUNT(*) n,
                 SUM(prompt_tokens) in_tok, SUM(completion_tokens) out_tok,
                 AVG(latency_ms) avg_lat, MAX(ts) last_used, SUM(1-ok) errs
          FROM requests GROUP BY m ORDER BY n DESC""").fetchall()
    return [dict(r) for r in rows]


# ---------- api keys ----------

def create_key(name):
    key = "sk-dp-" + secrets.token_urlsafe(24)
    with _lock:
        _conn().execute(
            "INSERT INTO api_keys (name,key_hash,prefix,tail,created) VALUES (?,?,?,?,?)",
            (name, _hash(key), key[:10], key[-4:], time.time()))
        _conn().commit()
    return key


def list_keys():
    with _lock:
        rows = _conn().execute(
            "SELECT id,name,prefix,tail,created,disabled,last_used "
            "FROM api_keys ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def delete_key(kid):
    with _lock:
        _conn().execute("DELETE FROM api_keys WHERE id=?", (kid,))
        _conn().commit()


def set_key_disabled(kid, disabled):
    with _lock:
        _conn().execute("UPDATE api_keys SET disabled=? WHERE id=?",
                        (int(disabled), kid))
        _conn().commit()


def key_info(key):
    """-> (name, disabled) for a presented bearer key, else None."""
    with _lock:
        r = _conn().execute(
            "SELECT name,disabled FROM api_keys WHERE key_hash=?",
            (_hash(key),)).fetchone()
        if r is not None and not r["disabled"]:
            _conn().execute("UPDATE api_keys SET last_used=? WHERE key_hash=?",
                            (time.time(), _hash(key)))
            _conn().commit()
    return dict(r) if r else None


def has_keys():
    with _lock:
        return _conn().execute(
            "SELECT COUNT(*) c FROM api_keys").fetchone()["c"] > 0


def counts():
    with _lock:
        reqs = _conn().execute("SELECT COUNT(*) c FROM requests").fetchone()["c"]
        keys = _conn().execute("SELECT COUNT(*) c FROM api_keys").fetchone()["c"]
    return {"requests": reqs, "keys": keys}
