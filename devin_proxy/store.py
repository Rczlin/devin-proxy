"""SQLite persistence: request log, usage stats, proxy API keys, accounts."""
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
        _con.execute("PRAGMA synchronous=NORMAL")
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
          account TEXT,
          endpoint TEXT,
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

        CREATE TABLE IF NOT EXISTS accounts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT,
          email TEXT,
          token TEXT NOT NULL UNIQUE,
          api_server_url TEXT DEFAULT 'https://server.codeium.com',
          devin_webapp_host TEXT,
          devin_api_url TEXT,
          source TEXT,
          plan TEXT,
          created REAL NOT NULL,
          disabled INTEGER DEFAULT 0,
          fail_count INTEGER DEFAULT 0,
          consecutive_fails INTEGER DEFAULT 0,
          cooldown_until REAL DEFAULT 0,
          last_error TEXT,
          last_used REAL,
          last_ok REAL,
          req_count INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS sessions (
          session_key TEXT PRIMARY KEY,
          account_id INTEGER,
          updated REAL
        );

        CREATE TABLE IF NOT EXISTS responses (
          id TEXT PRIMARY KEY,
          created REAL,
          model TEXT,
          status TEXT,
          account TEXT,
          session_key TEXT,
          items_json TEXT,
          response_json TEXT
        );

        CREATE TABLE IF NOT EXISTS meta (
          k TEXT PRIMARY KEY,
          v TEXT
        );
        """)
        _migrate_keys()
        _migrate_requests()
        _add_columns("api_keys", {
            "models": "TEXT",
            "max_concurrent": "INTEGER DEFAULT 0",
        })
        _add_columns("accounts", {
            "max_concurrent": "INTEGER DEFAULT 0",
            "models": "TEXT",
        })
        _con.commit()
    return _con


def _add_columns(table, cols):
    existing = {r[1] for r in _con.execute(f"PRAGMA table_info({table})")}
    for col, ddl in cols.items():
        if col not in existing:
            _con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


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


def _migrate_requests():
    """Add account/endpoint columns to pre-existing requests tables."""
    cols = {r[1] for r in _con.execute("PRAGMA table_info(requests)")}
    for col in ("account", "endpoint"):
        if col not in cols:
            _con.execute(f"ALTER TABLE requests ADD COLUMN {col} TEXT")


def _hash(key):
    return hashlib.sha256(key.encode()).hexdigest()


def log_request(model, resolved_model, stream, ok, status, error,
                prompt_tokens, completion_tokens, latency_ms, ttft_ms,
                client, key_name, messages_json, account=None, endpoint=None):
    global _insert_count
    with _lock:
        _conn().execute(
            "INSERT INTO requests (ts,model,resolved_model,stream,ok,status,error,"
            "prompt_tokens,completion_tokens,latency_ms,ttft_ms,client,key_name,"
            "account,endpoint,messages_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), model, resolved_model, int(stream), int(ok), status, error,
             prompt_tokens, completion_tokens, latency_ms, ttft_ms, client, key_name,
             account, endpoint, messages_json))
        _insert_count += 1
        if _insert_count % 100 == 0:
            _conn().execute(
                "DELETE FROM requests WHERE id < "
                "(SELECT MIN(id) FROM (SELECT id FROM requests ORDER BY id DESC LIMIT ?))",
                (_MAX_ROWS,))
        _conn().commit()


def _where(model=None, ok=None, q=None, account=None):
    sql, args = " WHERE 1=1", []
    if model:
        sql += " AND (model=? OR resolved_model=?)"
        args += [model, model]
    if account:
        sql += " AND account=?"
        args.append(account)
    if ok is not None:
        sql += " AND ok=?"
        args.append(int(ok))
    if q:
        sql += " AND (error LIKE ? OR client LIKE ? OR key_name LIKE ? OR account LIKE ?)"
        args += [f"%{q}%"] * 4
    return sql, args


def list_requests(limit=50, offset=0, model=None, ok=None, q=None, account=None):
    where, args = _where(model, ok, q, account)
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

def _models_text(models):
    """Normalize a model allowlist (iterable or comma string) -> TEXT or None."""
    if models is None:
        return None
    if isinstance(models, str):
        models = [m.strip() for m in models.split(",")]
    models = [m for m in models if m]
    return ",".join(models) if models else None


def create_key(name, models=None, max_concurrent=0):
    key = "sk-dp-" + secrets.token_urlsafe(24)
    with _lock:
        _conn().execute(
            "INSERT INTO api_keys (name,key_hash,prefix,tail,created,models,"
            "max_concurrent) VALUES (?,?,?,?,?,?,?)",
            (name, _hash(key), key[:10], key[-4:], time.time(),
             _models_text(models), int(max_concurrent or 0)))
        _conn().commit()
    return key


def list_keys():
    with _lock:
        rows = _conn().execute(
            "SELECT id,name,prefix,tail,created,disabled,last_used,models,"
            "max_concurrent FROM api_keys ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def delete_key(kid):
    with _lock:
        _conn().execute("DELETE FROM api_keys WHERE id=?", (kid,))
        _conn().commit()


def update_key(kid, **fields):
    cols = {"name", "disabled", "models", "max_concurrent"}
    sets, args = [], []
    for k, v in fields.items():
        if k in cols:
            if k == "models":
                v = _models_text(v)
            elif k == "max_concurrent":
                v = int(v or 0)
            elif k == "disabled":
                v = int(v)
            sets.append(f"{k}=?")
            args.append(v)
    if not sets:
        return
    with _lock:
        _conn().execute(f"UPDATE api_keys SET {','.join(sets)} WHERE id=?",
                        args + [kid])
        _conn().commit()


def set_key_disabled(kid, disabled):
    update_key(kid, disabled=disabled)


def key_info(key):
    """-> full key row for a presented bearer key, else None."""
    h = _hash(key)
    with _lock:
        r = _conn().execute(
            "SELECT id,name,disabled,models,max_concurrent,last_used "
            "FROM api_keys WHERE key_hash=?", (h,)).fetchone()
        now = time.time()
        if (r is not None and not r["disabled"]
                and now - (r["last_used"] or 0) > 60):
            _conn().execute("UPDATE api_keys SET last_used=? WHERE key_hash=?",
                            (now, h))
            _conn().commit()
    if r is None:
        return None
    d = dict(r)
    d["models"] = [m for m in (d.get("models") or "").split(",") if m]
    return d


def has_keys():
    with _lock:
        return _conn().execute(
            "SELECT COUNT(*) c FROM api_keys").fetchone()["c"] > 0


def counts():
    with _lock:
        reqs = _conn().execute("SELECT COUNT(*) c FROM requests").fetchone()["c"]
        keys = _conn().execute("SELECT COUNT(*) c FROM api_keys").fetchone()["c"]
        accs = _conn().execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"]
    return {"requests": reqs, "keys": keys, "accounts": accs}


# ---------- upstream accounts ----------

def add_account(token, name=None, email=None, api_server_url=None,
                devin_webapp_host=None, devin_api_url=None, source=None,
                plan=None):
    """Insert an upstream account; dedup by token. -> row dict or None if dup."""
    with _lock:
        try:
            cur = _conn().execute(
                "INSERT INTO accounts (name,email,token,api_server_url,"
                "devin_webapp_host,devin_api_url,source,plan,created)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (name, email, token, api_server_url or "https://server.codeium.com",
                 devin_webapp_host, devin_api_url, source, plan, time.time()))
            _conn().commit()
            r = _conn().execute("SELECT * FROM accounts WHERE id=?",
                                (cur.lastrowid,)).fetchone()
            return dict(r) if r else None
        except sqlite3.IntegrityError:
            return None


def update_account(aid, **fields):
    cols = {"name", "email", "api_server_url", "devin_webapp_host",
            "devin_api_url", "plan", "disabled", "fail_count",
            "consecutive_fails", "cooldown_until", "last_error",
            "last_used", "last_ok", "req_count", "max_concurrent", "models"}
    sets, args = [], []
    for k, v in fields.items():
        if k in cols:
            if k == "models":
                v = _models_text(v)
            elif k == "max_concurrent":
                v = int(v or 0)
            sets.append(f"{k}=?")
            args.append(v)
    if not sets:
        return
    with _lock:
        _conn().execute(f"UPDATE accounts SET {','.join(sets)} WHERE id=?",
                        args + [aid])
        _conn().commit()


def get_account(aid):
    with _lock:
        r = _conn().execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
    return dict(r) if r else None


def list_accounts():
    with _lock:
        rows = _conn().execute("SELECT * FROM accounts ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def delete_account(aid):
    with _lock:
        _conn().execute("DELETE FROM accounts WHERE id=?", (aid,))
        _conn().execute("DELETE FROM sessions WHERE account_id=?", (aid,))
        _conn().commit()


def account_stats():
    with _lock:
        rows = _conn().execute("""
          SELECT account a, COUNT(*) n, SUM(ok) ok_n,
                 SUM(prompt_tokens) in_tok, SUM(completion_tokens) out_tok,
                 AVG(latency_ms) avg_lat, MAX(ts) last_used
          FROM requests WHERE account IS NOT NULL GROUP BY a""").fetchall()
    return {r["a"]: dict(r) for r in rows}


# ---------- session pinning ----------

def set_pin(session_key, account_id):
    with _lock:
        _conn().execute(
            "INSERT INTO sessions (session_key,account_id,updated) VALUES (?,?,?) "
            "ON CONFLICT(session_key) DO UPDATE SET account_id=excluded.account_id,"
            "updated=excluded.updated",
            (session_key, account_id, time.time()))
        _conn().commit()


def touch_pins(pairs):
    """Batch-refresh `updated` on existing pins. pairs: [(updated, key), ...]"""
    if not pairs:
        return
    with _lock:
        _conn().executemany(
            "UPDATE sessions SET updated=? WHERE session_key=?", pairs)
        _conn().commit()


def list_pins():
    with _lock:
        rows = _conn().execute("""
          SELECT s.session_key, s.account_id, s.updated, a.name aname, a.email
          FROM sessions s LEFT JOIN accounts a ON a.id=s.account_id
          ORDER BY s.updated DESC""").fetchall()
    return [dict(r) for r in rows]


def unpin(session_key):
    with _lock:
        _conn().execute("DELETE FROM sessions WHERE session_key=?", (session_key,))
        _conn().commit()


def prune_pins(ttl_s):
    with _lock:
        _conn().execute("DELETE FROM sessions WHERE updated<?",
                        (time.time() - ttl_s,))
        _conn().commit()


# ---------- stored responses (previous_response_id chain) ----------

def save_response(rid, model, status, account, session_key, items_json, response_json):
    with _lock:
        _conn().execute(
            "INSERT OR REPLACE INTO responses"
            " (id,created,model,status,account,session_key,items_json,response_json)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (rid, time.time(), model, status, account, session_key,
             items_json, response_json))
        _conn().commit()


def get_response(rid):
    with _lock:
        r = _conn().execute("SELECT * FROM responses WHERE id=?", (rid,)).fetchone()
    return dict(r) if r else None


def delete_response(rid):
    with _lock:
        cur = _conn().execute("DELETE FROM responses WHERE id=?", (rid,))
        _conn().commit()
    return cur.rowcount > 0


def prune_responses(ttl_s=86400):
    with _lock:
        _conn().execute("DELETE FROM responses WHERE created<?",
                        (time.time() - ttl_s,))
        _conn().commit()


# ---------- meta kv ----------

def meta_get(k):
    with _lock:
        r = _conn().execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else None


def meta_set(k, v):
    with _lock:
        _conn().execute(
            "INSERT INTO meta (k,v) VALUES (?,?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
        _conn().commit()
