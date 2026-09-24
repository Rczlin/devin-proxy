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
# Full-fidelity captures (untruncated wire data) are pruned to the newest
# _CAP_KEEP successful and _CAP_FAIL_KEEP failed ones — a single stream can
# be megabytes and the db should stay portable.
_CAP_KEEP = int(os.environ.get("DEVIN_PROXY_CAP_KEEP", "300"))
_CAP_FAIL_KEEP = int(os.environ.get("DEVIN_PROXY_CAP_FAIL_KEEP", "500"))
_CAP_PRUNE_EVERY = 20      # prune capture retention once per N saves

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

_lock = threading.Lock()    # serializes the writer connection
_rlock = threading.Lock()   # serializes the shared reader connection
_rinit = threading.Lock()
_cinit = threading.Lock()   # first-time _conn() init (migrations)
_con = None
_rcon = None
_db_mem = False             # True when storage fell back to :memory:
_insert_count = 0
_cap_saves = 0
_data_version = 0           # bumped on every request-log mutation
_stats_cache = {}           # hours -> (data_version, overview dict)
_meta_cache = {}            # meta k -> v (invalidated by meta_set)
_last_auto_cleanup = 0.0
_maint_started = False
_mig_started = False


def _conn():
    global _con, _db_mem
    if _con is None:
        with _cinit:
            if _con is None:
                _conn_init()
    return _con


def _conn_init():
    global _con, _db_mem
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
        _db_mem = True
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
        # Big debug payloads live gzipped in these LAST columns. SQLite
        # spills record tails to overflow pages, so with the blobs at the
        # end every small column stays on the leaf page and table scans
        # (stats/log lists) never walk per-row overflow chains — the old
        # inline *_json columns made each row ~150 chained pages.
        "messages_gz": "BLOB",
        "request_gz": "BLOB",
        "events_gz": "BLOB",
        "sse_gz": "BLOB",
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
    _start_blob_migration()


def _reader():
    """Shared read-only connection for admin/reporting queries. In WAL mode
    readers never block the writer, so a heavy dashboard scan can't stall
    request auth/logging on _con. Falls back to _con when storage degraded
    to :memory: (callers must then hold _lock, not _rlock)."""
    global _rcon
    if _rcon is None:
        with _rinit:
            if _rcon is None:
                _conn()
                if _db_mem or _DB_PATH == ":memory:":
                    return _con
                try:
                    c = sqlite3.connect(_DB_PATH, check_same_thread=False)
                    c.row_factory = sqlite3.Row
                    c.execute("PRAGMA busy_timeout=5000")
                    c.execute("PRAGMA query_only=ON")
                    _rcon = c
                except (OSError, sqlite3.Error):
                    return _con
    return _rcon


def _q(sql, args=()):
    """fetchall on the reader connection (its own lock; shares _con + _lock
    in the degraded :memory: case)."""
    con = _reader()
    with (_lock if con is _con else _rlock):
        return con.execute(sql, args).fetchall()


def _q1(sql, args=()):
    rows = _q(sql, args)
    return rows[0] if rows else None


def _gz(s):
    """str -> gzip blob for the tail columns; None passes through."""
    if s is None:
        return None
    if isinstance(s, str):
        s = s.encode("utf-8")
    return gzip.compress(s, compresslevel=6)


_BLOB_COLS = (("messages_json", "messages_gz"), ("request_json", "request_gz"),
              ("events_json", "events_gz"), ("sse_json", "sse_gz"))


def _inflate(d):
    """Row dict -> decode the gzipped tail columns back into their *_json
    fields (legacy uncompressed values pass through untouched)."""
    for tcol, gcol in _BLOB_COLS:
        b = d.pop(gcol, None)
        if b is not None:
            try:
                d[tcol] = gzip.decompress(b).decode("utf-8")
            except Exception:
                pass
    return d


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
            con.execute(
                "DELETE FROM captures WHERE request_id < "
                "(SELECT MIN(id) FROM (SELECT id FROM requests"
                " ORDER BY id DESC LIMIT ?))", (keep,))
            if aggressive:
                con.execute(
                    "DELETE FROM captures WHERE ok=1 AND request_id < "
                    "(SELECT MIN(request_id) FROM (SELECT request_id"
                    " FROM captures WHERE ok=1 ORDER BY request_id DESC"
                    " LIMIT 20))")
                con.execute(
                    "DELETE FROM captures WHERE ok=0 AND request_id < "
                    "(SELECT MIN(request_id) FROM (SELECT request_id"
                    " FROM captures WHERE ok=0 ORDER BY request_id DESC"
                    " LIMIT ?))", (_CAP_FAIL_KEEP,))
            cutoff = time.time() - (3600 if aggressive else 86400)
            con.execute("DELETE FROM responses WHERE created<?", (cutoff,))
            con.execute("DELETE FROM sessions WHERE updated<?", (cutoff,))
            con.commit()
            global _data_version
            _data_version += 1
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
                # keep the WAL bounded so reads never merge a huge -wal
                with _lock:
                    if _con is not None and not _db_mem:
                        _con.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except Exception:
                pass
    threading.Thread(target=loop, daemon=True, name="devin-proxy-maint").start()


def _start_blob_migration():
    """One-time background pass: move the legacy inline *_json blob columns
    into the gzipped tail columns, freeing their overflow chains. Chunked +
    yielding so it never hogs the reader or writer lock; once done the file
    is VACUUMed when there's room (the freed pages would otherwise sit on
    the freelist and the ~1GB file would never shrink)."""
    global _mig_started
    if _mig_started or _db_mem or _DB_PATH == ":memory:":
        return
    _mig_started = True

    def run():
        migrated = 0
        try:
            while True:
                ids = [r["id"] for r in _q(
                    "SELECT id FROM requests WHERE messages_json IS NOT NULL"
                    " OR request_json IS NOT NULL OR events_json IS NOT NULL"
                    " OR sse_json IS NOT NULL ORDER BY id LIMIT 40")]
                if not ids:
                    break
                migrated += len(ids)
                for rid in ids:
                    row = _q1(
                        "SELECT messages_json,request_json,events_json,"
                        "sse_json FROM requests WHERE id=?", (rid,))
                    if row is None:
                        continue
                    vals = [_gz(row["messages_json"]), _gz(row["request_json"]),
                            _gz(row["events_json"]), _gz(row["sse_json"])]
                    with _lock:
                        _con.execute(
                            "UPDATE requests SET messages_gz=?,request_gz=?,"
                            "events_gz=?,sse_gz=?,messages_json=NULL,"
                            "request_json=NULL,events_json=NULL,sse_json=NULL"
                            " WHERE id=?", (*vals, rid))
                        _con.commit()
                    time.sleep(0.01)
            with _lock:
                _con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            return
        if not migrated:
            return
        # VACUUM rebuilds into a temp copy — only when the disk clearly has
        # room for it; otherwise the freelist is just reused going forward.
        try:
            free = shutil.disk_usage(
                os.path.dirname(_DB_PATH) or ".").free
            if free > max(512 * 1024 * 1024, _safe_db_size() * 2):
                with _lock:
                    _con.execute("VACUUM")
        except (OSError, sqlite3.Error):
            pass

    threading.Thread(target=run, daemon=True, name="devin-proxy-blobmig").start()


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
    global _insert_count, _data_version
    # compress off-lock: ~400KB of debug JSON -> ~40KB, and the CPU work
    # doesn't hold the writer up.
    blobs = [_gz(messages_json), _gz(request_json),
             _gz(events_json), _gz(sse_json)]
    with _lock:
        cur = _conn().execute(
            "INSERT INTO requests (ts,model,resolved_model,stream,ok,status,error,"
            "prompt_tokens,completion_tokens,latency_ms,ttft_ms,client,key_name,"
            "account,endpoint,flags,cached_tokens,cache_creation_tokens,gen_ms,tps,"
            "upstream_model,upstream_msg_id,upstream_req_id,"
            "messages_gz,request_gz,events_gz,sse_gz)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), model, resolved_model, int(stream), int(ok), status, error,
             prompt_tokens, completion_tokens, latency_ms, ttft_ms, client, key_name,
             account, endpoint, flags, cached_tokens, cache_creation_tokens,
             gen_ms, tps, upstream_model, upstream_msg_id, upstream_req_id,
             *blobs))
        _insert_count += 1
        if _insert_count % 100 == 0:
            keep = _conn().execute(
                "SELECT MIN(id) m FROM (SELECT id FROM requests"
                " ORDER BY id DESC LIMIT ?)", (_MAX_ROWS,)).fetchone()["m"]
            if keep is not None:
                _conn().execute("DELETE FROM requests WHERE id<?", (keep,))
                _conn().execute(
                    "DELETE FROM captures WHERE request_id<?", (keep,))
        _conn().commit()
        _data_version += 1
        return cur.lastrowid


@_resilient(default=None)
def save_capture(request_id, ok, payload):
    """Store a full-fidelity capture (dict -> gzip blob) for one request row.
    Success captures prune to the newest _CAP_KEEP, failures to the newest
    _CAP_FAIL_KEEP. Never raises — capture must not break the request."""
    global _cap_saves
    if request_id is None:
        return
    try:
        blob = gzip.compress(json.dumps(payload, ensure_ascii=False,
                                        default=str).encode(),
                             compresslevel=6)
    except Exception:
        return
    with _lock:
        _conn().execute(
            "INSERT OR REPLACE INTO captures (request_id,ts,ok,data)"
            " VALUES (?,?,?,?)",
            (request_id, time.time(), int(ok), blob))
        _cap_saves += 1
        if _cap_saves % _CAP_PRUNE_EVERY == 0:
            # range deletes on the request_id PK — cheap, unlike the old
            # per-save NOT IN scans over the whole (multi-MB) captures table
            _conn().execute(
                "DELETE FROM captures WHERE ok=1 AND request_id < "
                "(SELECT MIN(request_id) FROM (SELECT request_id"
                " FROM captures WHERE ok=1 ORDER BY request_id DESC"
                " LIMIT ?))", (_CAP_KEEP,))
            _conn().execute(
                "DELETE FROM captures WHERE ok=0 AND request_id < "
                "(SELECT MIN(request_id) FROM (SELECT request_id"
                " FROM captures WHERE ok=0 ORDER BY request_id DESC"
                " LIMIT ?))", (_CAP_FAIL_KEEP,))
        _conn().commit()


@_resilient(default=None)
def get_capture(request_id):
    r = _q1("SELECT data FROM captures WHERE request_id=?", (request_id,))
    if not r:
        return None
    try:
        return json.loads(gzip.decompress(r["data"]).decode("utf-8"))
    except Exception:
        return None


@_resilient(default=list)
def export_captures():
    """[{request_id, ok, data(gzip blob)}] for the diagnostic bundle."""
    rows = _q("SELECT request_id,ok,data FROM captures ORDER BY request_id")
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
        rows = _q(f"SELECT {_REQ_LIST_COLS} FROM requests{where}"
                  " ORDER BY id DESC LIMIT ?", args + [limit])
    else:
        rows = _q(f"SELECT {_REQ_LIST_COLS} FROM requests{where}"
                  " ORDER BY id DESC LIMIT ? OFFSET ?",
                  args + [limit, offset])
    total = _q1(f"SELECT COUNT(*) c FROM requests{where}", args)["c"]
    return [dict(r) for r in rows], total


@_resilient(default=None)
def get_request(rid):
    r = _q1("SELECT * FROM requests WHERE id=?", (rid,))
    return _inflate(dict(r)) if r else None


@_resilient(default=None)
def clear_requests():
    global _data_version
    with _lock:
        _conn().execute("DELETE FROM requests")
        _conn().execute("DELETE FROM captures")
        _conn().commit()
        _data_version += 1


@_resilient(default=0)
def prune_requests(model=None, ok=None, q=None, account=None, flag=None,
                   before_ts=None):
    """Delete logged requests matching the same filters as list_requests,
    plus an optional age cutoff. Returns the number of rows removed;
    captures are deleted for exactly those rows."""
    global _data_version
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
        _data_version += 1
    return len(ids)


def stats_overview(hours=24):
    """Windowed stats for the dashboard — never raises. Falls back to an
    all-zero snapshot if the db can't be read (e.g. mid disk-full).
    Results are cached per window against _data_version, so the UI's
    polling is free between writes."""
    try:
        ent = _stats_cache.get(hours or 0)
        if ent and ent[0] == _data_version:
            return dict(ent[1])
        ver = _data_version            # read BEFORE computing: a mid-scan
        d = _stats_overview_impl(hours)  # write just expires this entry
        if len(_stats_cache) > 64:     # hours is caller-chosen — keep bounded
            _stats_cache.clear()
        _stats_cache[hours or 0] = (ver, d)
        return dict(d)
    except sqlite3.Error as e:
        if _is_space_error(e):
            _maybe_auto_cleanup()
        return _empty_overview(hours)


def _empty_overview(hours):
    now = time.time()
    return {
        "hours": hours or 0, "bucket_s": 0, "since": now, "now": now,
        "total": 0, "errors": 0, "streams": 0, "input_tokens": 0,
        "uncached_tokens": 0,
        "output_tokens": 0, "cached_tokens": 0, "cache_creation_tokens": 0,
        "cache_hit_pct": 0, "avg_tps": 0, "avg_latency_ms": 0,
        "avg_ttft_ms": 0, "p50_ms": 0, "p95_ms": 0, "rpm": 0, "tpm": 0,
        "truncated": 0, "retried": 0,
        "today": {"requests": 0, "input_tokens": 0, "output_tokens": 0},
        "alltime": {"requests": 0}, "series": [],
        "by_model": [], "by_account": [], "by_key": [], "by_endpoint": [],
        "recent_errors": [], "db_size": _safe_db_size(), "max_rows": _MAX_ROWS,
    }


# every column the overview needs — all sit ahead of the *_gz tail blobs,
# so this single scan reads only leaf pages even on a fat old table
_STATS_COLS = ("id,ts,model,resolved_model,stream,ok,status,error,"
               "prompt_tokens,completion_tokens,latency_ms,ttft_ms,"
               "key_name,account,endpoint,flags,cached_tokens,"
               "cache_creation_tokens,tps")


def _stats_overview_impl(hours=24):
    """Windowed stats for the dashboard. hours<=0/None = all time.

    One scan of the window's rows, then all rollups (totals, adaptive
    5m/30m/1h/4h/1d gap-free series, per-model/account/key/endpoint
    breakdowns, p50/p95, recent errors) are computed in Python — that
    replaced ~14 separate SQL passes which each re-walked the table."""
    con = _reader()
    with (_lock if con is _con else _rlock):
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
            lo = con.execute(
                "SELECT MIN(ts) t FROM requests").fetchone()["t"]
            since = lo or (now - 86400)
            days = max(1, int((now - since) / 86400) + 1)
            bucket = 86400 * max(1, -(-days // 60))
        # bucket start alignment: day+ buckets land on local midnight,
        # sub-day buckets on whole UTC multiples (hour marks in CST too)
        tz = _tz_offset() if bucket >= 86400 else 0
        base = int(since + tz) - int(since + tz) % bucket - tz

        rows = con.execute(
            f"SELECT {_STATS_COLS} FROM requests WHERE ts>=?",
            (since,)).fetchall()
        all_total = con.execute(
            "SELECT COUNT(*) c FROM requests").fetchone()["c"]
        today_row = None
        if since > today:    # window doesn't reach midnight — query it
            today_row = con.execute(
                "SELECT COUNT(*) n,"
                " SUM(prompt_tokens+cached_tokens) i,"
                " SUM(completion_tokens) o FROM requests WHERE ts>=?",
                (today,)).fetchone()
        db_size = _safe_db_size()

    def v(r, c):
        x = r[c]
        return x if x is not None else 0

    n = len(rows)
    ok_rows = [r for r in rows if r["ok"]]
    ok_n = len(ok_rows)
    lats = sorted(r["latency_ms"] for r in ok_rows
                  if r["latency_ms"] is not None)
    nl = len(lats)

    def pct(p):
        return lats[min(nl - 1, int(nl * p))] if nl else 0

    def avg(col):
        s = c = 0
        for r in rows:
            x = r[col]
            if x is not None:
                s += x
                c += 1
        return s / c if c else 0

    # bucketed series
    byb = {}
    for r in rows:
        e = byb.setdefault(int((r["ts"] - base) / bucket),
                           [0, 0, 0, 0, 0.0, 0, 0])
        e[0] += 1
        e[1] += 1 - (r["ok"] or 0)
        e[2] += v(r, "prompt_tokens")
        e[3] += v(r, "completion_tokens")
        if r["latency_ms"] is not None:
            e[4] += r["latency_ms"]
            e[5] += 1
        e[6] += v(r, "cached_tokens")
    nb = max(0, int((now - base) // bucket))
    series = [{"t": base + b * bucket,
               "n": e[0] if e else 0,
               "errs": e[1] if e else 0,
               "in_tok": e[2] if e else 0,
               "out_tok": e[3] if e else 0,
               "avg_lat": round(e[4] / e[5]) if e and e[5] else 0,
               "cache_rd": e[6] if e else 0}
              for b in range(nb + 1) for e in [byb.get(b)]]

    # group rollups — one dict per key, all measures accumulated together
    def _group(keyfn, skip_none=False):
        out = {}
        for r in rows:
            k = keyfn(r)
            if k is None and skip_none:
                continue
            e = out.get(k)
            if e is None:
                e = out[k] = {"n": 0, "errs": 0, "in_tok": 0, "out_tok": 0,
                              "lat_s": 0.0, "lat_n": 0, "cached": 0,
                              "cache_wr": 0,
                              "tps_s": 0.0, "tps_n": 0,
                              "ttft_s": 0.0, "ttft_n": 0, "last": 0}
            e["n"] += 1
            e["errs"] += 1 - (r["ok"] or 0)
            e["in_tok"] += v(r, "prompt_tokens")
            e["out_tok"] += v(r, "completion_tokens")
            e["cached"] += v(r, "cached_tokens")
            e["cache_wr"] += v(r, "cache_creation_tokens")
            if r["latency_ms"] is not None:
                e["lat_s"] += r["latency_ms"]
                e["lat_n"] += 1
            if r["tps"] is not None:
                e["tps_s"] += r["tps"]
                e["tps_n"] += 1
            if r["ttft_ms"] is not None:
                e["ttft_s"] += r["ttft_ms"]
                e["ttft_n"] += 1
            if r["ts"] and r["ts"] > e["last"]:
                e["last"] = r["ts"]
        return out

    def _avg(e, s, n):
        return e[s] / e[n] if e[n] else 0

    gm = _group(lambda r: r["resolved_model"] or r["model"])
    by_model = [{"m": k, "n": e["n"], "errs": e["errs"],
                 "in_tok": e["in_tok"], "out_tok": e["out_tok"],
                 "avg_lat": _avg(e, "lat_s", "lat_n"),
                 "cached": e["cached"],
                 "avg_tps": _avg(e, "tps_s", "tps_n"),
                 "avg_ttft": _avg(e, "ttft_s", "ttft_n"),
                 "last_used": e["last"]}
                for k, e in sorted(gm.items(), key=lambda kv: -kv[1]["n"])
                [:24]]
    ga = _group(lambda r: r["account"], skip_none=True)
    by_account = [{"a": k, "n": e["n"], "errs": e["errs"],
                   "in_tok": e["in_tok"], "out_tok": e["out_tok"],
                   "cached": e["cached"],
                   "avg_lat": _avg(e, "lat_s", "lat_n"),
                   "last_used": e["last"]}
                  for k, e in sorted(ga.items(), key=lambda kv: -kv[1]["n"])]
    gk = _group(lambda r: r["key_name"])
    by_key = [{"k": k, "n": e["n"], "errs": e["errs"],
               "tok": e["in_tok"] + e["out_tok"] + e["cached"],
               "cached": e["cached"],
               "last_used": e["last"]}
              for k, e in sorted(gk.items(), key=lambda kv: -kv[1]["n"])]
    ge = _group(lambda r: r["endpoint"] or "chat")
    by_endpoint = [{"ep": k, "n": e["n"], "errs": e["errs"]}
                   for k, e in sorted(ge.items(), key=lambda kv: -kv[1]["n"])]

    recent_errors = [
        {c: r[c] for c in
         ("id", "ts", "model", "resolved_model", "status", "error",
          "account", "key_name", "latency_ms", "flags")}
        for r in sorted((r for r in rows if not r["ok"]),
                        key=lambda r: r["id"], reverse=True)[:12]]

    rpm = sum(1 for r in rows if r["ts"] >= now - 60)
    tok5 = sum(v(r, "prompt_tokens") + v(r, "completion_tokens")
               for r in rows if r["ts"] >= now - 300)
    truncated = sum(1 for r in rows if "truncated" in (r["flags"] or ""))
    retried = sum(1 for r in rows if "retried" in (r["flags"] or ""))
    if today_row is None:    # window covers today — reuse the same rows
        tw = [r for r in rows if r["ts"] >= today]
        today_row = {"n": len(tw),
                     "i": sum(v(r, "prompt_tokens") + v(r, "cached_tokens")
                              for r in tw),
                     "o": sum(v(r, "completion_tokens") for r in tw)}

    in_tok = sum(v(r, "prompt_tokens") for r in rows)
    out_tok = sum(v(r, "completion_tokens") for r in rows)
    cached_tok = sum(v(r, "cached_tokens") for r in rows)
    cache_wr = sum(v(r, "cache_creation_tokens") for r in rows)
    return {
        "hours": hours or 0,
        "bucket_s": bucket,
        "since": since,
        "now": now,
        "total": n,
        "errors": n - ok_n,
        "streams": sum(v(r, "stream") for r in rows),
        "input_tokens": in_tok + cached_tok,
        "uncached_tokens": in_tok,
        "output_tokens": out_tok,
        "cached_tokens": cached_tok,
        "cache_creation_tokens": cache_wr,
        "cache_hit_pct": round(100 * cached_tok
                               / max(1, cached_tok + in_tok), 1),
        "avg_tps": round(avg("tps"), 1),
        "avg_latency_ms": round(avg("latency_ms")),
        "avg_ttft_ms": round(avg("ttft_ms")),
        "p50_ms": pct(0.5),
        "p95_ms": pct(0.95),
        "rpm": rpm,
        "tpm": round(tok5 / 5),
        "truncated": truncated,
        "retried": retried,
        "today": {"requests": today_row["n"] or 0,
                  "input_tokens": today_row["i"] or 0,
                  "output_tokens": today_row["o"] or 0},
        "alltime": {"requests": all_total},
        "series": series,
        "by_model": by_model,
        "by_account": by_account,
        "by_key": by_key,
        "by_endpoint": by_endpoint,
        "recent_errors": recent_errors,
        "db_size": db_size,
        "max_rows": _MAX_ROWS,
    }


def _tz_offset():
    return -time.timezone if not time.daylight else -time.altzone


@_resilient(default=list)
def list_models():
    rows = _q("""
      SELECT COALESCE(resolved_model,model) m, COUNT(*) n,
             SUM(prompt_tokens+cached_tokens) in_tok,
             SUM(completion_tokens) out_tok,
             SUM(cached_tokens) cached,
             AVG(latency_ms) avg_lat, MAX(ts) last_used, SUM(1-ok) errs
      FROM requests GROUP BY m ORDER BY n DESC""")
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
    rows = _q("SELECT id,name,prefix,tail,created,disabled,last_used,models,"
              "max_concurrent FROM api_keys ORDER BY id")
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
    r = _q1("SELECT (SELECT COUNT(*) FROM requests) reqs,"
            " (SELECT COUNT(*) FROM api_keys) keys,"
            " (SELECT COUNT(*) FROM accounts) accs,"
            " (SELECT COUNT(*) FROM captures) caps")
    return {"requests": r["reqs"], "keys": r["keys"],
            "accounts": r["accs"], "captures": r["caps"]}


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
    rows = _q("SELECT * FROM accounts ORDER BY id")
    return [dict(r) for r in rows]


@_resilient(default=None)
def delete_account(aid):
    with _lock:
        _conn().execute("DELETE FROM accounts WHERE id=?", (aid,))
        _conn().execute("DELETE FROM sessions WHERE account_id=?", (aid,))
        _conn().commit()


@_resilient(default=dict)
def account_stats():
    rows = _q("""
      SELECT account a, COUNT(*) n, SUM(ok) ok_n,
             SUM(prompt_tokens+cached_tokens) in_tok,
             SUM(completion_tokens) out_tok,
             SUM(cached_tokens) cached,
             AVG(latency_ms) avg_lat, MAX(ts) last_used
      FROM requests WHERE account IS NOT NULL GROUP BY a""")
    return {r["a"]: dict(r) for r in rows}


@_resilient(default=list)
def error_stats(hours=24, limit=12):
    """Failure rollup for the log page: group errors by a short signature
    (first 80 chars, digits stripped so 'timeout after 31.4s' and '…29.9s'
    collapse into one row) -> count + latest ts + an example id."""
    since = time.time() - hours * 3600 if hours else 0
    rows = _q(
        """
      SELECT substr(error,1,80) sig, COUNT(*) n, MAX(ts) last,
             MAX(id) example_id
      FROM requests
      WHERE ok=0 AND error IS NOT NULL AND ts>=?
      GROUP BY sig ORDER BY n DESC LIMIT ?""",
        (since, max(1, min(limit, 100))))
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
    rows = _q("""
      SELECT s.session_key, s.account_id, s.updated, a.name aname, a.email
      FROM sessions s LEFT JOIN accounts a ON a.id=s.account_id
      ORDER BY s.updated DESC""")
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
    """meta values are tiny and read on every /v1 call (model resolve,
    aliases, efforts) — cache them; meta_set writes through."""
    try:
        return _meta_cache[k]
    except KeyError:
        pass
    with _lock:
        r = _conn().execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    v = r["v"] if r else None
    _meta_cache[k] = v
    return v


@_resilient(default=None)
def meta_set(k, v):
    with _lock:
        _conn().execute(
            "INSERT INTO meta (k,v) VALUES (?,?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
        _conn().commit()
    _meta_cache[k] = v


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
    return [_inflate(dict(r)) for r in _q(sql, args)]


@_resilient(default=list)
def export_accounts():
    """accounts minus `token` — credentials never enter a bundle."""
    cols = ("id,name,email,api_server_url,devin_webapp_host,devin_api_url,"
            "source,plan,created,disabled,fail_count,consecutive_fails,"
            "cooldown_until,last_error,last_used,last_ok,req_count,"
            "max_concurrent,models")
    return [dict(r) for r in
            _q(f"SELECT {cols} FROM accounts ORDER BY id")]


@_resilient(default=list)
def export_keys():
    """api_keys metadata only — hash/prefix identify a key, none are usable."""
    cols = ("id,name,prefix,tail,created,disabled,last_used,models,"
            "max_concurrent")
    return [dict(r) for r in _q(f"SELECT {cols} FROM api_keys ORDER BY id")]


@_resilient(default=dict)
def export_meta():
    """meta kv minus secrets (master_key; tomb:* are deleted-key hashes)."""
    rows = _q("SELECT k,v FROM meta")
    return {r["k"]: r["v"] for r in rows
            if r["k"] != "master_key" and not r["k"].startswith("tomb:")}


@_resilient(default=list)
def export_responses(limit=500):
    return [dict(r) for r in _q(
        "SELECT * FROM responses ORDER BY created DESC LIMIT ?",
        (max(1, min(limit, 5000)),))]
