"""SQLite persistence: request log, usage stats, proxy API keys, accounts."""
import functools
import gzip
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import threading
import time

_DB_PATH = os.environ.get("DEVIN_PROXY_DB") or os.path.join(
    os.path.expanduser("~"), ".devin-proxy", "devin-proxy.db")
if os.name == "nt" and "DEVIN_PROXY_DB" not in os.environ:
    _DB_PATH = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                            "devin-proxy", "devin-proxy.db")

_MAX_ROWS = int(os.environ.get("DEVIN_PROXY_MAX_ROWS", "50000"))
# Full-fidelity captures (untruncated wire data) are kept for ALL failed
# requests but only the newest _CAP_KEEP successful ones — a successful
# stream can be megabytes and the db should stay portable.
_CAP_KEEP = int(os.environ.get("DEVIN_PROXY_CAP_KEEP", "300"))

# ---------- disk-space resilience ----------
# The proxy must keep serving requests even when the volume backing the db
# is completely full. Two mechanisms cooperate:
#  - a small sacrificial "reserve" file, deleted the instant we see a
#    disk-full error, so the emergency cleanup below always has a little
#    room to write into (freeing space itself needs *some* space: WAL mode
#    appends before it can delete/checkpoint).
#  - every state-mutating function is wrapped so a disk-full sqlite error
#    is swallowed (and triggers that cleanup) instead of bubbling up and
#    breaking the request that was otherwise served successfully.
_MIN_FREE_MB = int(os.environ.get("DEVIN_PROXY_MIN_FREE_MB", "128"))
_RESERVE_MB = int(os.environ.get("DEVIN_PROXY_RESERVE_MB", "8"))
_EMERGENCY_ROWS = max(200, min(2000, _MAX_ROWS // 10))
_MAINT_INTERVAL_S = 45
_AUTO_CLEANUP_COOLDOWN_S = 20

_lock = threading.Lock()
_con = None
_insert_count = 0
_last_auto_cleanup = 0.0
_maint_started = False


def _conn():
    global _con
    if _con is None:
        try:
            os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
            _con = sqlite3.connect(_DB_PATH, check_same_thread=False)
        except (OSError, sqlite3.Error) as e:
            # Disk full (or otherwise unwritable) even before we hold a
            # connection — fall back to an in-memory db so the proxy can
            # still start and serve traffic; persistence resumes once the
            # file becomes writable again and the process is restarted.
            print(f"devin-proxy: cannot open {_DB_PATH} ({e}); "
                  "falling back to an in-memory db", flush=True)
            _con = sqlite3.connect(":memory:", check_same_thread=False)
        _con.row_factory = sqlite3.Row
        _con.execute("PRAGMA journal_mode=WAL")
        _con.execute("PRAGMA busy_timeout=5000")
        _con.execute("PRAGMA synchronous=NORMAL")
        _con.execute("PRAGMA temp_store=MEMORY")
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
        CREATE INDEX IF NOT EXISTS idx_req_ok ON requests(ok);
        CREATE INDEX IF NOT EXISTS idx_req_account ON requests(account);
        CREATE TABLE IF NOT EXISTS captures (
          request_id INTEGER PRIMARY KEY,
          ts REAL NOT NULL,
          ok INTEGER DEFAULT 1,
          data BLOB            -- gzip(json) full-fidelity capture
        );

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
        _add_columns("requests", {
            "request_json": "TEXT",
            "events_json": "TEXT",
            "sse_json": "TEXT",
            "flags": "TEXT",
            "cached_tokens": "INTEGER DEFAULT 0",
            "cache_creation_tokens": "INTEGER DEFAULT 0",
            "gen_ms": "INTEGER",
            "tps": "REAL",
            "upstream_model": "TEXT",
            "upstream_msg_id": "TEXT",
            "upstream_req_id": "TEXT",
        })
        _add_columns("api_keys", {
            "models": "TEXT",
            "max_concurrent": "INTEGER DEFAULT 0",
        })
        _add_columns("accounts", {
            "max_concurrent": "INTEGER DEFAULT 0",
            "models": "TEXT",
        })
        _con.commit()
        _ensure_reserve()
        _start_maintenance_thread()
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


# ---------- disk-space resilience ----------

def _reserve_path():
    d = os.path.dirname(_DB_PATH) or "."
    return os.path.join(d, ".devin-proxy-reserve")


def _ensure_reserve():
    """Best-effort: keep a small sacrificial file on disk. Deleting a file
    needs no free space of its own, so this guarantees we can always claw
    back a little room the instant we hit ENOSPC — enough for the DELETE +
    WAL-checkpoint below to actually run. Never raises."""
    if _DB_PATH == ":memory:":
        return
    path = _reserve_path()
    need = _RESERVE_MB * 1024 * 1024
    try:
        if os.path.exists(path) and os.path.getsize(path) >= need:
            return
        with open(path, "wb") as f:
            f.truncate(need)
    except OSError:
        pass  # disk is already full — nothing to spare yet


def _release_reserve():
    """Delete the sacrificial file, instantly freeing _RESERVE_MB."""
    try:
        os.remove(_reserve_path())
        return True
    except OSError:
        return False


def disk_status():
    """Free/total bytes on the volume backing the db, plus current size."""
    d = os.path.dirname(_DB_PATH) or "."
    try:
        u = shutil.disk_usage(d)
        free, total = u.free, u.total
    except OSError:
        free = total = None
    return {
        "free": free, "total": total,
        "low": free is not None and free < _MIN_FREE_MB * 1024 * 1024,
        "reserve_mb": _RESERVE_MB,
        "reserve_active": os.path.exists(_reserve_path()),
        "db_size": _safe_db_size(),
    }


def _safe_db_size():
    try:
        return os.path.getsize(_DB_PATH) if os.path.exists(_DB_PATH) else 0
    except OSError:
        return 0


def _is_space_error(exc):
    if not isinstance(exc, sqlite3.Error):
        return False
    msg = str(exc).lower()
    return any(s in msg for s in ("disk", "full", "no space", "i/o error"))


def _maybe_auto_cleanup():
    """Rate-limited trigger, called from the write-error path so a burst of
    failing writes doesn't each pay for a full cleanup pass."""
    global _last_auto_cleanup
    now = time.time()
    if now - _last_auto_cleanup < _AUTO_CLEANUP_COOLDOWN_S:
        return
    _last_auto_cleanup = now
    free_space(aggressive=True, vacuum=False)


def _resilient(default=None):
    """Decorator for state-mutating store functions: a disk-full (or other)
    sqlite error is logged and swallowed instead of propagating — callers
    on the request path must never fail just because logging/accounting
    couldn't be persisted."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            try:
                return fn(*a, **kw)
            except sqlite3.Error as e:
                if _is_space_error(e):
                    _maybe_auto_cleanup()
                else:
                    print(f"devin-proxy: store.{fn.__name__} failed: {e}",
                          flush=True)
                return default() if callable(default) else default
        return wrapper
    return deco


def free_space(aggressive=True, vacuum=True):
    """Reclaim disk space: drop the sacrificial reserve, prune old request/
    capture/response/session history, checkpoint the WAL and (optionally)
    VACUUM. Safe to call anytime — holds the same lock as every other
    write, never raises, and is exposed to the admin UI as a manual
    "free up space" action as well as being used for emergency recovery."""
    freed_reserve = _release_reserve()
    before = _safe_db_size()
    keep = _EMERGENCY_ROWS if aggressive else max(_EMERGENCY_ROWS, _MAX_ROWS // 2)
    try:
        with _lock:
            con = _con
            if con is None:
                return {"freed_reserve": freed_reserve, "freed_bytes": 0,
                        "db_size_before": before, "db_size_after": before}
            con.execute(
                "DELETE FROM requests WHERE id < "
                "(SELECT MIN(id) FROM (SELECT id FROM requests"
                " ORDER BY id DESC LIMIT ?))", (keep,))
            con.execute("DELETE FROM captures WHERE request_id NOT IN "
                       "(SELECT id FROM requests)")
            if aggressive:
                con.execute(
                    "DELETE FROM captures WHERE ok=1 AND request_id NOT IN "
                    "(SELECT request_id FROM captures WHERE ok=1"
                    " ORDER BY request_id DESC LIMIT 20)")
            cutoff = time.time() - (3600 if aggressive else 86400)
            con.execute("DELETE FROM responses WHERE created<?", (cutoff,))
            con.execute("DELETE FROM sessions WHERE updated<?", (cutoff,))
            con.commit()
            try:
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            if vacuum:
                try:
                    con.execute("VACUUM")
                except sqlite3.Error:
                    pass
    except sqlite3.Error:
        pass
    after = _safe_db_size()
    _ensure_reserve()
    return {"freed_reserve": freed_reserve,
            "freed_bytes": max(0, before - after),
            "db_size_before": before, "db_size_after": after}


def _start_maintenance_thread():
    global _maint_started
    if _maint_started:
        return
    _maint_started = True

    def loop():
        while True:
            time.sleep(_MAINT_INTERVAL_S)
            try:
                st = disk_status()
                if st["low"]:
                    free_space(aggressive=True, vacuum=False)
                elif not st["reserve_active"]:
                    _ensure_reserve()
            except Exception:
                pass
    threading.Thread(target=loop, daemon=True, name="devin-proxy-maint").start()


@_resilient(default=None)
def log_request(model, resolved_model, stream, ok, status, error,
                prompt_tokens, completion_tokens, latency_ms, ttft_ms,
                client, key_name, messages_json, account=None, endpoint=None,
                request_json=None, events_json=None, sse_json=None, flags=None,
                cached_tokens=0, cache_creation_tokens=0, gen_ms=None,
                tps=None, upstream_model=None, upstream_msg_id=None,
                upstream_req_id=None):
    """-> new row id, or None if the write couldn't be persisted (e.g. the
    disk is full) — the request itself must still succeed either way."""
    global _insert_count
    with _lock:
        cur = _conn().execute(
            "INSERT INTO requests (ts,model,resolved_model,stream,ok,status,error,"
            "prompt_tokens,completion_tokens,latency_ms,ttft_ms,client,key_name,"
            "account,endpoint,messages_json,request_json,events_json,sse_json,"
            "flags,cached_tokens,cache_creation_tokens,gen_ms,tps,"
            "upstream_model,upstream_msg_id,upstream_req_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), model, resolved_model, int(stream), int(ok), status, error,
             prompt_tokens, completion_tokens, latency_ms, ttft_ms, client, key_name,
             account, endpoint, messages_json, request_json, events_json, sse_json,
             flags, cached_tokens, cache_creation_tokens, gen_ms, tps,
             upstream_model, upstream_msg_id, upstream_req_id))
        _insert_count += 1
        if _insert_count % 100 == 0:
            _conn().execute(
                "DELETE FROM requests WHERE id < "
                "(SELECT MIN(id) FROM (SELECT id FROM requests ORDER BY id DESC LIMIT ?))",
                (_MAX_ROWS,))
            _conn().execute(
                "DELETE FROM captures WHERE request_id NOT IN "
                "(SELECT id FROM requests)")
        _conn().commit()
        return cur.lastrowid


@_resilient(default=None)
def save_capture(request_id, ok, payload):
    """Store a full-fidelity capture (dict -> gzip blob) for one request row.
    Failure captures are kept forever; success captures are pruned to the
    newest _CAP_KEEP. Never raises — capture must not break the request."""
    if request_id is None:
        return
    try:
        blob = gzip.compress(json.dumps(payload, ensure_ascii=False,
                                        default=str).encode())
    except Exception:
        return
    with _lock:
        _conn().execute(
            "INSERT OR REPLACE INTO captures (request_id,ts,ok,data)"
            " VALUES (?,?,?,?)",
            (request_id, time.time(), int(ok), blob))
        _conn().execute(
            "DELETE FROM captures WHERE ok=1 AND request_id NOT IN "
            "(SELECT request_id FROM captures WHERE ok=1"
            " ORDER BY request_id DESC LIMIT ?)", (_CAP_KEEP,))
        _conn().commit()


@_resilient(default=None)
def get_capture(request_id):
    with _lock:
        r = _conn().execute("SELECT data FROM captures WHERE request_id=?",
                            (request_id,)).fetchone()
    if not r:
        return None
    try:
        return json.loads(gzip.decompress(r["data"]).decode("utf-8"))
    except Exception:
        return None


@_resilient(default=list)
def export_captures():
    """[{request_id, ok, data(gzip blob)}] for the diagnostic bundle."""
    with _lock:
        rows = _conn().execute(
            "SELECT request_id,ok,data FROM captures"
            " ORDER BY request_id").fetchall()
    return [dict(r) for r in rows]


def _where(model=None, ok=None, q=None, account=None, flag=None):
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
    if flag:
        sql += " AND flags LIKE ?"
        args.append(f"%{flag}%")
    if q:
        sql += (" AND (error LIKE ? OR client LIKE ? OR key_name LIKE ?"
                " OR account LIKE ? OR flags LIKE ?)")
        args += [f"%{q}%"] * 5
    return sql, args


_REQ_LIST_COLS = ("id,ts,model,resolved_model,stream,ok,status,error,"
                  "prompt_tokens,completion_tokens,cached_tokens,"
                  "cache_creation_tokens,latency_ms,ttft_ms,gen_ms,tps,"
                  "client,key_name,account,endpoint,flags,"
                  "(SELECT COUNT(*) FROM captures c"
                  " WHERE c.request_id=requests.id) has_cap")


@_resilient(default=lambda: ([], 0))
def list_requests(limit=50, offset=0, model=None, ok=None, q=None, account=None,
                  flag=None, before_id=None):
    where, args = _where(model, ok, q, account, flag)
    if before_id:
        # keyset pagination: stable under concurrent inserts and indexed,
        # unlike OFFSET which rescans and drifts when new rows land mid-page
        where += " AND id<?"
        args.append(before_id)
    with _lock:
        if before_id:
            rows = _conn().execute(
                f"SELECT {_REQ_LIST_COLS} FROM requests{where}"
                " ORDER BY id DESC LIMIT ?", args + [limit]).fetchall()
        else:
            rows = _conn().execute(
                f"SELECT {_REQ_LIST_COLS} FROM requests{where}"
                " ORDER BY id DESC LIMIT ? OFFSET ?",
                args + [limit, offset]).fetchall()
        total = _conn().execute(
            f"SELECT COUNT(*) c FROM requests{where}", args).fetchone()["c"]
    return [dict(r) for r in rows], total


@_resilient(default=None)
def get_request(rid):
    with _lock:
        r = _conn().execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
    return dict(r) if r else None


@_resilient(default=None)
def clear_requests():
    with _lock:
        _conn().execute("DELETE FROM requests")
        _conn().execute("DELETE FROM captures")
        _conn().commit()


@_resilient(default=0)
def prune_requests(model=None, ok=None, q=None, account=None, flag=None,
                   before_ts=None):
    """Delete logged requests matching the same filters as list_requests,
    plus an optional age cutoff. Returns the number of rows removed;
    captures are deleted for exactly those rows."""
    where, args = _where(model, ok, q, account, flag)
    if before_ts:
        where += " AND ts<?"
        args.append(before_ts)
    with _lock:
        ids = [r["id"] for r in _conn().execute(
            f"SELECT id FROM requests{where}", args).fetchall()]
        # chunked deletes: SQLite caps bound variables (~999 default, 32766
        # on new builds) — a big filtered prune would blow past it.
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            ph = ",".join("?" * len(chunk))
            _conn().execute(
                f"DELETE FROM captures WHERE request_id IN ({ph})", chunk)
            _conn().execute(
                f"DELETE FROM requests WHERE id IN ({ph})", chunk)
        _conn().commit()
    return len(ids)


def stats_overview(hours=24):
    """Windowed stats for the dashboard — never raises. Falls back to an
    all-zero snapshot if the db can't be read (e.g. mid disk-full)."""
    try:
        return _stats_overview_impl(hours)
    except sqlite3.Error as e:
        if _is_space_error(e):
            _maybe_auto_cleanup()
        return _empty_overview(hours)


def _empty_overview(hours):
    now = time.time()
    return {
        "hours": hours or 0, "bucket_s": 0, "since": now, "now": now,
        "total": 0, "errors": 0, "streams": 0, "input_tokens": 0,
        "output_tokens": 0, "cached_tokens": 0, "cache_creation_tokens": 0,
        "cache_hit_pct": 0, "avg_tps": 0, "avg_latency_ms": 0,
        "avg_ttft_ms": 0, "p50_ms": 0, "p95_ms": 0, "rpm": 0, "tpm": 0,
        "truncated": 0, "retried": 0,
        "today": {"requests": 0, "input_tokens": 0, "output_tokens": 0},
        "alltime": {"requests": 0}, "series": [],
        "by_model": [], "by_account": [], "by_key": [], "by_endpoint": [],
        "recent_errors": [], "db_size": _safe_db_size(), "max_rows": _MAX_ROWS,
    }


def _stats_overview_impl(hours=24):
    """Windowed stats for the dashboard. hours<=0/None = all time.

    The time series is bucketed adaptively (5m/30m/1h/4h/1d) and returned
    gap-free — every bucket in [since, now] is present, zeros included."""
    with _lock:
        c = _conn()
        now = time.time()
        today = now - (now + _tz_offset()) % 86400
        if hours:
            since = now - hours * 3600
            if hours <= 3:
                bucket = 300
            elif hours <= 24:
                bucket = 1800
            elif hours <= 96:
                bucket = 3600
            elif hours <= 336:
                bucket = 14400
            else:
                bucket = 86400
        else:
            lo = c.execute("SELECT MIN(ts) t FROM requests").fetchone()["t"]
            since = lo or (now - 86400)
            days = max(1, int((now - since) / 86400) + 1)
            bucket = 86400 * max(1, -(-days // 60))
        # bucket start alignment: day+ buckets land on local midnight,
        # sub-day buckets on whole UTC multiples (hour marks in CST too)
        tz = _tz_offset() if bucket >= 86400 else 0
        base = int(since + tz) - int(since + tz) % bucket - tz
        where, wargs = " WHERE ts>=?", [since]

        tot = c.execute(f"""
          SELECT COUNT(*) n, SUM(ok) ok_n, SUM(stream) st,
                 SUM(prompt_tokens) in_tok, SUM(completion_tokens) out_tok,
                 SUM(cached_tokens) cached_tok,
                 SUM(cache_creation_tokens) cache_wr,
                 AVG(latency_ms) avg_lat, AVG(ttft_ms) avg_ttft,
                 AVG(tps) avg_tps
          FROM requests{where}""", wargs).fetchone()

        def _pct(p):
            n = tot["ok_n"] or 0
            if not n:
                return 0
            r = c.execute(
                f"SELECT latency_ms FROM requests{where} AND ok=1"
                " ORDER BY latency_ms LIMIT 1 OFFSET ?",
                wargs + [min(n - 1, int(n * p))]).fetchone()
            return r[0] if r else 0

        rows = c.execute(f"""
          SELECT CAST((ts - ?)/{bucket} AS INT) b, COUNT(*) n,
                 SUM(1-ok) errs, SUM(prompt_tokens) in_tok,
                 SUM(completion_tokens) out_tok, AVG(latency_ms) avg_lat
          FROM requests{where} GROUP BY b""", [base] + wargs).fetchall()
        byb = {r["b"]: r for r in rows}
        nb = max(0, int((now - base) // bucket))
        series = [{"t": base + b * bucket,
                   "n": r["n"] if (r := byb.get(b)) else 0,
                   "errs": (r["errs"] or 0) if r else 0,
                   "in_tok": (r["in_tok"] or 0) if r else 0,
                   "out_tok": (r["out_tok"] or 0) if r else 0,
                   "avg_lat": round(r["avg_lat"] or 0) if r else 0}
                  for b in range(nb + 1)]

        by_model = c.execute(f"""
          SELECT COALESCE(resolved_model,model) m, COUNT(*) n,
                 SUM(1-ok) errs, SUM(prompt_tokens) in_tok,
                 SUM(completion_tokens) out_tok, AVG(latency_ms) avg_lat,
                 SUM(cached_tokens) cached, AVG(tps) avg_tps,
                 AVG(ttft_ms) avg_ttft, MAX(ts) last_used
          FROM requests{where} GROUP BY m ORDER BY n DESC LIMIT 24""",
            wargs).fetchall()
        by_account = c.execute(f"""
          SELECT account a, COUNT(*) n, SUM(1-ok) errs,
                 SUM(prompt_tokens) in_tok, SUM(completion_tokens) out_tok,
                 AVG(latency_ms) avg_lat, MAX(ts) last_used
          FROM requests{where} AND account IS NOT NULL
          GROUP BY a ORDER BY n DESC""", wargs).fetchall()
        by_key = c.execute(f"""
          SELECT key_name k, COUNT(*) n, SUM(1-ok) errs,
                 SUM(prompt_tokens+completion_tokens) tok, MAX(ts) last_used
          FROM requests{where} GROUP BY key_name ORDER BY n DESC""",
            wargs).fetchall()
        by_endpoint = c.execute(f"""
          SELECT COALESCE(endpoint,'chat') ep, COUNT(*) n, SUM(1-ok) errs
          FROM requests{where} GROUP BY ep ORDER BY n DESC""",
            wargs).fetchall()
        recent_errors = c.execute(f"""
          SELECT id,ts,model,resolved_model,status,error,account,key_name,
                 latency_ms,flags
          FROM requests{where} AND ok=0 ORDER BY id DESC LIMIT 12""",
            wargs).fetchall()
        rpm = c.execute("SELECT COUNT(*) c FROM requests WHERE ts>=?",
                        (now - 60,)).fetchone()["c"]
        tok5 = c.execute(
            "SELECT SUM(prompt_tokens+completion_tokens) t FROM requests"
            " WHERE ts>=?", (now - 300,)).fetchone()["t"] or 0
        truncated = c.execute(
            f"SELECT COUNT(*) c FROM requests{where}"
            " AND flags LIKE '%truncated%'", wargs).fetchone()["c"]
        retried = c.execute(
            f"SELECT COUNT(*) c FROM requests{where}"
            " AND flags LIKE '%retried%'", wargs).fetchone()["c"]
        all_total = c.execute("SELECT COUNT(*) c FROM requests").fetchone()["c"]
        today_row = c.execute("""
          SELECT COUNT(*) total, SUM(prompt_tokens) in_tok,
                 SUM(completion_tokens) out_tok
          FROM requests WHERE ts>=?""", (today,)).fetchone()
        db_size = _safe_db_size()
    return {
        "hours": hours or 0,
        "bucket_s": bucket,
        "since": since,
        "now": now,
        "total": tot["n"] or 0,
        "errors": (tot["n"] or 0) - (tot["ok_n"] or 0),
        "streams": tot["st"] or 0,
        "input_tokens": tot["in_tok"] or 0,
        "output_tokens": tot["out_tok"] or 0,
        "cached_tokens": tot["cached_tok"] or 0,
        "cache_creation_tokens": tot["cache_wr"] or 0,
        "cache_hit_pct": round(100 * (tot["cached_tok"] or 0)
                               / max(1, (tot["cached_tok"] or 0)
                                     + (tot["in_tok"] or 0)), 1),
        "avg_tps": round(tot["avg_tps"] or 0, 1),
        "avg_latency_ms": round(tot["avg_lat"] or 0),
        "avg_ttft_ms": round(tot["avg_ttft"] or 0),
        "p50_ms": _pct(0.5),
        "p95_ms": _pct(0.95),
        "rpm": rpm,
        "tpm": round(tok5 / 5),
        "truncated": truncated,
        "retried": retried,
        "today": {"requests": today_row["total"] or 0,
                  "input_tokens": today_row["in_tok"] or 0,
                  "output_tokens": today_row["out_tok"] or 0},
        "alltime": {"requests": all_total},
        "series": series,
        "by_model": [dict(r) for r in by_model],
        "by_account": [dict(r) for r in by_account],
        "by_key": [dict(r) for r in by_key],
        "by_endpoint": [dict(r) for r in by_endpoint],
        "recent_errors": [dict(r) for r in recent_errors],
        "db_size": db_size,
        "max_rows": _MAX_ROWS,
    }


def _tz_offset():
    return -time.timezone if not time.daylight else -time.altzone


@_resilient(default=list)
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


@_resilient(default=None)
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


@_resilient(default=list)
def list_keys():
    with _lock:
        rows = _conn().execute(
            "SELECT id,name,prefix,tail,created,disabled,last_used,models,"
            "max_concurrent FROM api_keys ORDER BY id").fetchall()
    return [dict(r) for r in rows]


@_resilient(default=None)
def delete_key(kid):
    with _lock:
        cur = _conn().execute("DELETE FROM api_keys WHERE id=?", (kid,))
        _conn().commit()
        return cur.rowcount


@_resilient(default=None)
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
        return 0
    with _lock:
        cur = _conn().execute(
            f"UPDATE api_keys SET {','.join(sets)} WHERE id=?",
            args + [kid])
        _conn().commit()
        return cur.rowcount


def set_key_disabled(kid, disabled):
    update_key(kid, disabled=disabled)


def key_info(key):
    """-> full key row for a presented bearer key, else None. Authentication
    must keep working even if the disk is full: the row lookup always runs,
    and only the best-effort `last_used` timestamp write is guarded so a
    failed write can never turn a valid key into a rejected request."""
    h = _hash(key)
    space_error = False
    with _lock:
        r = _conn().execute(
            "SELECT id,name,disabled,models,max_concurrent,last_used "
            "FROM api_keys WHERE key_hash=?", (h,)).fetchone()
        now = time.time()
        if (r is not None and not r["disabled"]
                and now - (r["last_used"] or 0) > 60):
            try:
                _conn().execute(
                    "UPDATE api_keys SET last_used=? WHERE key_hash=?",
                    (now, h))
                _conn().commit()
            except sqlite3.Error as e:
                space_error = _is_space_error(e)
    if space_error:
        _maybe_auto_cleanup()  # must run outside _lock — it re-acquires it
    if r is None:
        return None
    d = dict(r)
    d["models"] = [m for m in (d.get("models") or "").split(",") if m]
    return d


@_resilient(default=lambda: True)  # fail closed: assume auth required
def has_keys():
    with _lock:
        return _conn().execute(
            "SELECT COUNT(*) c FROM api_keys").fetchone()["c"] > 0


@_resilient(default=lambda: {"requests": 0, "keys": 0, "accounts": 0,
                             "captures": 0})
def counts():
    with _lock:
        reqs = _conn().execute("SELECT COUNT(*) c FROM requests").fetchone()["c"]
        keys = _conn().execute("SELECT COUNT(*) c FROM api_keys").fetchone()["c"]
        accs = _conn().execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"]
        caps = _conn().execute("SELECT COUNT(*) c FROM captures").fetchone()["c"]
    return {"requests": reqs, "keys": keys, "accounts": accs,
            "captures": caps}


# ---------- upstream accounts ----------

@_resilient(default=None)
def add_account(token, name=None, email=None, api_server_url=None,
                devin_webapp_host=None, devin_api_url=None, source=None,
                plan=None):
    """Insert an upstream account; dedup by token. -> row dict or None if dup
    (or if the write couldn't be persisted, e.g. disk full)."""
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


@_resilient(default=None)
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


@_resilient(default=None)
def get_account(aid):
    with _lock:
        r = _conn().execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
    return dict(r) if r else None


@_resilient(default=list)
def list_accounts():
    with _lock:
        rows = _conn().execute("SELECT * FROM accounts ORDER BY id").fetchall()
    return [dict(r) for r in rows]


@_resilient(default=None)
def delete_account(aid):
    with _lock:
        _conn().execute("DELETE FROM accounts WHERE id=?", (aid,))
        _conn().execute("DELETE FROM sessions WHERE account_id=?", (aid,))
        _conn().commit()


@_resilient(default=dict)
def account_stats():
    with _lock:
        rows = _conn().execute("""
          SELECT account a, COUNT(*) n, SUM(ok) ok_n,
                 SUM(prompt_tokens) in_tok, SUM(completion_tokens) out_tok,
                 AVG(latency_ms) avg_lat, MAX(ts) last_used
          FROM requests WHERE account IS NOT NULL GROUP BY a""").fetchall()
    return {r["a"]: dict(r) for r in rows}


@_resilient(default=list)
def error_stats(hours=24, limit=12):
    """Failure rollup for the log page: group errors by a short signature
    (first 80 chars, digits stripped so 'timeout after 31.4s' and '…29.9s'
    collapse into one row) -> count + latest ts + an example id."""
    since = time.time() - hours * 3600 if hours else 0
    with _lock:
        rows = _conn().execute(
            """
          SELECT substr(error,1,80) sig, COUNT(*) n, MAX(ts) last,
                 MAX(id) example_id
          FROM requests
          WHERE ok=0 AND error IS NOT NULL AND ts>=?
          GROUP BY sig ORDER BY n DESC LIMIT ?""",
            (since, max(1, min(limit, 100)))).fetchall()
    import re
    out = {}
    for r in rows:
        sig = re.sub(r"\d+", "#", r["sig"]).strip() or "(empty)"
        d = out.setdefault(sig, {"sig": sig, "n": 0,
                                 "last": 0, "example_id": 0})
        d["n"] += r["n"]
        if r["last"] > d["last"]:
            d["last"], d["example_id"] = r["last"], r["example_id"]
    return sorted(out.values(), key=lambda x: -x["n"])[:limit]


# ---------- session pinning ----------

@_resilient(default=None)
def set_pin(session_key, account_id):
    with _lock:
        _conn().execute(
            "INSERT INTO sessions (session_key,account_id,updated) VALUES (?,?,?) "
            "ON CONFLICT(session_key) DO UPDATE SET account_id=excluded.account_id,"
            "updated=excluded.updated",
            (session_key, account_id, time.time()))
        _conn().commit()


@_resilient(default=None)
def touch_pins(pairs):
    """Batch-refresh `updated` on existing pins. pairs: [(updated, key), ...]"""
    if not pairs:
        return
    with _lock:
        _conn().executemany(
            "UPDATE sessions SET updated=? WHERE session_key=?", pairs)
        _conn().commit()


@_resilient(default=list)
def list_pins():
    with _lock:
        rows = _conn().execute("""
          SELECT s.session_key, s.account_id, s.updated, a.name aname, a.email
          FROM sessions s LEFT JOIN accounts a ON a.id=s.account_id
          ORDER BY s.updated DESC""").fetchall()
    return [dict(r) for r in rows]


@_resilient(default=None)
def unpin(session_key):
    with _lock:
        _conn().execute("DELETE FROM sessions WHERE session_key=?", (session_key,))
        _conn().commit()


@_resilient(default=None)
def prune_pins(ttl_s):
    with _lock:
        _conn().execute("DELETE FROM sessions WHERE updated<?",
                        (time.time() - ttl_s,))
        _conn().commit()


# ---------- stored responses (previous_response_id chain) ----------

@_resilient(default=None)
def save_response(rid, model, status, account, session_key, items_json, response_json):
    with _lock:
        _conn().execute(
            "INSERT OR REPLACE INTO responses"
            " (id,created,model,status,account,session_key,items_json,response_json)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (rid, time.time(), model, status, account, session_key,
             items_json, response_json))
        _conn().commit()


@_resilient(default=None)
def get_response(rid):
    with _lock:
        r = _conn().execute("SELECT * FROM responses WHERE id=?", (rid,)).fetchone()
    return dict(r) if r else None


@_resilient(default=lambda: False)
def delete_response(rid):
    with _lock:
        cur = _conn().execute("DELETE FROM responses WHERE id=?", (rid,))
        _conn().commit()
    return cur.rowcount > 0


@_resilient(default=None)
def prune_responses(ttl_s=86400):
    with _lock:
        _conn().execute("DELETE FROM responses WHERE created<?",
                        (time.time() - ttl_s,))
        _conn().commit()


# ---------- meta kv ----------

@_resilient(default=None)
def meta_get(k):
    with _lock:
        r = _conn().execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else None


@_resilient(default=None)
def meta_set(k, v):
    with _lock:
        _conn().execute(
            "INSERT INTO meta (k,v) VALUES (?,?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
        _conn().commit()


# ---------- diagnostic bundle ----------

@_resilient(default=list)
def export_requests(limit=500, hours=0):
    """Full request rows for offline analysis — every column, newest first."""
    sql, args = "SELECT * FROM requests", []
    if hours and hours > 0:
        sql += " WHERE ts>=?"
        args.append(time.time() - hours * 3600)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(limit, _MAX_ROWS)))
    with _lock:
        return [dict(r) for r in _conn().execute(sql, args).fetchall()]


@_resilient(default=list)
def export_accounts():
    """accounts minus `token` — credentials never enter a bundle."""
    cols = ("id,name,email,api_server_url,devin_webapp_host,devin_api_url,"
            "source,plan,created,disabled,fail_count,consecutive_fails,"
            "cooldown_until,last_error,last_used,last_ok,req_count,"
            "max_concurrent,models")
    with _lock:
        return [dict(r) for r in _conn().execute(
            f"SELECT {cols} FROM accounts ORDER BY id").fetchall()]


@_resilient(default=list)
def export_keys():
    """api_keys metadata only — hash/prefix identify a key, none are usable."""
    cols = ("id,name,prefix,tail,created,disabled,last_used,models,"
            "max_concurrent")
    with _lock:
        return [dict(r) for r in _conn().execute(
            f"SELECT {cols} FROM api_keys ORDER BY id").fetchall()]


@_resilient(default=dict)
def export_meta():
    """meta kv minus secrets (master_key; tomb:* are deleted-key hashes)."""
    with _lock:
        rows = _conn().execute("SELECT k,v FROM meta").fetchall()
    return {r["k"]: r["v"] for r in rows
            if r["k"] != "master_key" and not r["k"].startswith("tomb:")}


@_resilient(default=list)
def export_responses(limit=500):
    with _lock:
        return [dict(r) for r in _conn().execute(
            "SELECT * FROM responses ORDER BY created DESC LIMIT ?",
            (max(1, min(limit, 5000)),)).fetchall()]
