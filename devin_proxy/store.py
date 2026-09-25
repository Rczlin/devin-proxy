"""SQLite persistence: request log, usage stats, proxy API keys, accounts.

Storage is split across three database files so each data class has its
own lifecycle — the big win is that "reclaim disk space" can mean "delete
one file" (which needs no free space of its own) instead of "DELETE rows
then VACUUM" (which needs free space it may not have):

  _DB_PATH    (devin-proxy.db)      core: api_keys / accounts / sessions / meta
                                    the proxy cannot run without these.
  _LOG_DB     (devin-proxy-logs.db) request log — one light row per request.
                                    Source for every statistic. Gone -> stats
                                    reset to zero but the proxy still serves.
  _DIAG_DB    (devin-proxy-diag.db) diagnostics — captures, stored responses,
                                    and the per-request *_json payloads
                                    (messages/request/events/sse). Gone ->
                                    detail pages lose bodies but lists/stats
                                    keep working. Largest file by far; safe
                                    to delete whole at ENOSPC.
"""
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

_MEM = ":memory:"


def _sibling(base, suffix):
    """Companion db path next to the core file: foo.db -> foo-<suffix>.db."""
    root, ext = os.path.splitext(base)
    return f"{root}-{suffix}{ext or '.db'}"


def _env_path(var, fallback):
    """Explicit env override wins; sibling path derives; :memory: is global."""
    v = os.environ.get(var)
    if v:
        return v
    if _DB_PATH == _MEM:
        return _MEM
    return fallback


_LOG_DB = _env_path("DEVIN_PROXY_LOG_DB", _sibling(_DB_PATH, "logs"))
_DIAG_DB = _env_path("DEVIN_PROXY_DIAG_DB", _sibling(_DB_PATH, "diag"))

_MAX_ROWS = int(os.environ.get("DEVIN_PROXY_MAX_ROWS", "50000"))
# Full-fidelity captures (untruncated wire data) are pruned to the newest
# _CAP_KEEP successful and _CAP_FAIL_KEEP failed ones — a single stream can
# be megabytes and the db should stay portable.
_CAP_KEEP = int(os.environ.get("DEVIN_PROXY_CAP_KEEP", "300"))
_CAP_FAIL_KEEP = int(os.environ.get("DEVIN_PROXY_CAP_FAIL_KEEP", "500"))
_CAP_PRUNE_EVERY = 20      # prune capture retention once per N saves

# ---------- disk-space resilience ----------
# The proxy must keep serving requests even when the volume backing the db
# is completely full. Three cooperating mechanisms:
#  - a small sacrificial "reserve" file, deleted the instant we see a
#    disk-full error, so cleanup always has a little room to write into.
#  - proactive watermarks: below _SOFT_FREE_MB the maintenance loop prunes
#    old data every pass; below _HARD_FREE_MB it prunes harder and also
#    drops the whole diagnostic db if needed. Deleting a file needs no
#    free space, so this still works at literally 0 bytes free.
#  - every state-mutating function is wrapped so a disk-full sqlite error
#    is swallowed (and triggers that cleanup) instead of bubbling up and
#    breaking the request that was otherwise served successfully.
_MIN_FREE_MB = int(os.environ.get("DEVIN_PROXY_MIN_FREE_MB", "128"))
_SOFT_FREE_MB = int(os.environ.get("DEVIN_PROXY_SOFT_FREE_MB", "100"))
_HARD_FREE_MB = int(os.environ.get("DEVIN_PROXY_HARD_FREE_MB", "20"))
_RESERVE_MB = int(os.environ.get("DEVIN_PROXY_RESERVE_MB", "8"))
_LOG_MAX_MB = int(os.environ.get("DEVIN_PROXY_LOG_MAX_MB", "32"))
_EMERGENCY_ROWS = max(200, min(2000, _MAX_ROWS // 10))
_MAINT_INTERVAL_S = 45
_AUTO_CLEANUP_COOLDOWN_S = 20

# Three independent writer connections, one lock each. A slow diag write
# (multi-MB capture) can never stall core auth or the request-log insert.
_clock = threading.Lock()    # core: api_keys/accounts/sessions/meta
_llock = threading.Lock()    # log: requests
_dlock = threading.Lock()    # diag: captures/responses/request_blobs
_rlock = threading.Lock()    # all reader connections (they are query-only)
_cinit = threading.Lock()    # first-time init (migrations)
_con = None                  # core writer
_lcon = None                 # log writer
_dcon = None                 # diag writer
_rcon = None                 # core reader
_lrcon = None                # log reader
_drcon = None                # diag reader
_db_mem = False              # True when core storage fell back to :memory:
_log_mem = False             # same for the log db
_diag_mem = False            # same for the diag db
_insert_count = 0
_cap_saves = 0
_data_version = 0            # bumped on every request-log mutation
_stats_cache = {}            # hours -> (data_version, overview dict)
_meta_cache = {}             # meta k -> v (invalidated by meta_set)
_last_auto_cleanup = 0.0
_maint_started = False
_mig_started = False


# ---------------------------------------------------------------- core db

def _conn():
    global _con
    if _con is None:
        with _cinit:
            if _con is None:
                _init_all()
    return _con


def _open(path):
    """Connect (WAL, busy timeout) or degrade to :memory: so a full disk can
    never stop the proxy from *starting* — the request path degrades to
    non-persistent instead of crashing."""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        c = sqlite3.connect(path, check_same_thread=False)
        mem = False
    except (OSError, sqlite3.Error) as e:
        print(f"devin-proxy: cannot open {path} ({e}); "
              "falling back to an in-memory db", flush=True)
        c = sqlite3.connect(_MEM, check_same_thread=False)
        mem = True
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA temp_store=MEMORY")
    return c, mem


def _init_all():
    """First connection: open all three files, migrate, start background
    threads. Runs once under _cinit."""
    global _con, _lcon, _dcon, _db_mem, _log_mem, _diag_mem
    _con, _db_mem = _open(_DB_PATH)
    _lcon, _log_mem = _open(_LOG_DB)
    _dcon, _diag_mem = _open(_DIAG_DB)

    _con.executescript("""
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

        CREATE TABLE IF NOT EXISTS meta (
          k TEXT PRIMARY KEY,
          v TEXT
        );
        """)
    _migrate_keys()
    _add_columns("api_keys", {
        "models": "TEXT",
        "max_concurrent": "INTEGER DEFAULT 0",
    })
    _add_columns("accounts", {
        "max_concurrent": "INTEGER DEFAULT 0",
        "models": "TEXT",
    })
    _con.commit()

    _lcon.executescript("""
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
          endpoint TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_req_ts ON requests(ts);
        CREATE INDEX IF NOT EXISTS idx_req_model ON requests(model);
        CREATE INDEX IF NOT EXISTS idx_req_ok ON requests(ok);
        CREATE INDEX IF NOT EXISTS idx_req_account ON requests(account);
        """)
    _migrate_requests()
    _add_columns("requests", {
        "flags": "TEXT",
        "cached_tokens": "INTEGER DEFAULT 0",
        "cache_creation_tokens": "INTEGER DEFAULT 0",
        "gen_ms": "INTEGER",
        "tps": "REAL",
        "upstream_model": "TEXT",
        "upstream_msg_id": "TEXT",
        "upstream_req_id": "TEXT",
    }, con=_lcon)
    _lcon.commit()

    _dcon.executescript("""
        CREATE TABLE IF NOT EXISTS captures (
          request_id INTEGER PRIMARY KEY,
          ts REAL NOT NULL,
          ok INTEGER DEFAULT 1,
          data BLOB            -- gzip(json) full-fidelity capture
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
        -- per-request diagnostic payloads, one row per (request_id, kind).
        -- kind in (messages, request, events, sse); data = gzip(json).
        CREATE TABLE IF NOT EXISTS request_blobs (
          request_id INTEGER NOT NULL,
          kind TEXT NOT NULL,
          data BLOB,
          PRIMARY KEY (request_id, kind)
        );
        """)
    _dcon.commit()

    _ensure_reserve()
    _start_maintenance_thread()
    _start_blob_migration()


def _migrate_requests():
    """Add account/endpoint columns to a pre-existing requests table (in the
    log db). Also no-ops cleanly when the table lives in the old combined
    file — the legacy columns are just ignored on new writes."""
    cols = {r[1] for r in _lcon.execute("PRAGMA table_info(requests)")}
    for col in ("account", "endpoint"):
        if col not in cols:
            _lcon.execute(f"ALTER TABLE requests ADD COLUMN {col} TEXT")


# reader caches: db-name -> opened query-only connection (None = not tried yet)
_READERS = {"core": None, "log": None, "diag": None}
_READER_TRIED = {"core": False, "log": False, "diag": False}
_PATHS = {"core": "_DB_PATH", "log": "_LOG_DB", "diag": "_DIAG_DB"}
_MEMS = {"core": "_db_mem", "log": "_log_mem", "diag": "_diag_mem"}
_LOCKS = {"core": "_clock", "log": "_llock", "diag": "_dlock"}
_WRITERS = {"core": "_con", "log": "_lcon", "diag": "_dcon"}


def _q(sql, args=(), db="core"):
    """fetchall on a query-only reader connection (its own _rlock). When the
    db is :memory: or can't be opened for reading, falls back to the writer
    connection — callers then serialize on that db's writer lock instead.
    WAL mode means a real reader never blocks the writer, so a heavy
    dashboard scan can't stall request auth/logging."""
    lock = globals()[_LOCKS[db]]
    writer = globals()[_WRITERS[db]]
    if writer is None:
        # either never opened (core init covers all three) or the file was
        # just dropped — reopen under the write lock so reads keep working.
        if db == "core":
            _conn()
        else:
            with lock:
                globals()[_WRITERS[db]]
                if globals()[_WRITERS[db]] is None:
                    _reopen_db(db)
        writer = globals()[_WRITERS[db]]
    con = _READERS[db]
    if con is None and not _READER_TRIED[db]:
        _READER_TRIED[db] = True
        path = globals()[_PATHS[db]]
        mem = globals()[_MEMS[db]]
        try:
            if mem or path == _MEM:
                raise sqlite3.Error("memory db — use writer")
            c = sqlite3.connect(path, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA busy_timeout=5000")
            c.execute("PRAGMA query_only=ON")
            _READERS[db] = c
            con = c
        except (OSError, sqlite3.Error):
            con = writer
    if con is None:
        con = writer
    if con is writer:
        with lock:
            return con.execute(sql, args).fetchall()
    with _rlock:
        return con.execute(sql, args).fetchall()


def _invalidate_reader(db):
    """Drop a cached reader (used when its underlying file was deleted and
    recreated — a stale connection would keep writing/reading the old
    unlinked inode)."""
    c = _READERS[db]
    _READERS[db] = None
    _READER_TRIED[db] = False
    if c is not None:
        try:
            c.close()
        except sqlite3.Error:
            pass


def _q1(sql, args=(), db="core"):
    rows = _q(sql, args, db)
    return rows[0] if rows else None


def _gz(s):
    """str -> gzip blob for the tail columns; None passes through."""
    if s is None:
        return None
    if isinstance(s, str):
        s = s.encode("utf-8")
    return gzip.compress(s, compresslevel=6)


_BLOB_COLS = (("messages_json", "messages"), ("request_json", "request"),
              ("events_json", "events"), ("sse_json", "sse"))


def _inflate(d):
    """Row dict -> decode gzipped blobs into their *_json fields. Blobs now
    live in diag.request_blobs keyed by request_id, so this pulls each kind
    and decompresses. Missing diag db / missing rows just leave the fields
    absent (never raises)."""
    rid = d.get("id")
    if rid is None:
        return d
    try:
        rows = _q("SELECT kind,data FROM request_blobs WHERE request_id=?",
                  (rid,), db="diag")
    except sqlite3.Error:
        return d
    blob = {r["kind"]: r["data"] for r in rows}
    for tcol, kind in _BLOB_COLS:
        b = blob.get(kind)
        if b is None:
            continue
        try:
            d[tcol] = gzip.decompress(b).decode("utf-8")
        except Exception:
            pass
    return d


def _add_columns(table, cols, con=None):
    c = con or _con
    existing = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
    for col, ddl in cols.items():
        if col not in existing:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


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


# ---------- disk-space resilience ----------

def _db_dir():
    return os.path.dirname(_DB_PATH) or "."


def _reserve_path():
    return os.path.join(_db_dir(), ".devin-proxy-reserve")


def _ensure_reserve():
    """Best-effort: keep a small sacrificial file on disk. Deleting a file
    needs no free space of its own, so this guarantees we can always claw
    back a little room the instant we hit ENOSPC — enough for the DELETE +
    WAL-checkpoint below to actually run. Never raises."""
    if _DB_PATH == _MEM:
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


def _fsize(path):
    try:
        return os.path.getsize(path) if os.path.exists(path) else 0
    except OSError:
        return 0


def _db_size(path):
    if not path or path == _MEM:
        return 0
    return _fsize(path)


def disk_status():
    """Free/total bytes on the volume backing the dbs, plus per-file sizes
    so the UI can show which store is eating the disk."""
    try:
        u = shutil.disk_usage(_db_dir())
        free, total = u.free, u.total
    except OSError:
        free = total = None
    return {
        "free": free, "total": total,
        "low": free is not None and free < _MIN_FREE_MB * 1024 * 1024,
        "soft_mb": _SOFT_FREE_MB, "hard_mb": _HARD_FREE_MB,
        "reserve_mb": _RESERVE_MB,
        "reserve_active": os.path.exists(_reserve_path()),
        "db_size": _db_size(_DB_PATH),
        "per_db": {
            "core": _db_size(_DB_PATH),
            "log": _db_size(_LOG_DB),
            "diag": _db_size(_DIAG_DB),
        },
        "log_mem": _log_mem, "diag_mem": _diag_mem, "db_mem": _db_mem,
    }


def _safe_db_size():
    return _db_size(_DB_PATH)


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


def _checkpoint(con):
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass


def drop_diag_db():
    """Delete the diagnostic database file entirely — the nuclear option
    that always works because unlink needs no free space. The next write
    lazily recreates the schema in a fresh (empty) file. Returns freed bytes."""
    global _dcon, _diag_mem
    if _DIAG_DB == _MEM:
        return 0
    before = _db_size(_DIAG_DB)
    with _dlock:
        try:
            if _dcon is not None:
                _dcon.close()
        except sqlite3.Error:
            pass
        _dcon = None
        with _rlock:
            _invalidate_reader("diag")
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(_DIAG_DB + suffix)
            except OSError:
                pass
    return before


def drop_log_db():
    """Delete the request-log db (stats reset to zero). Same file-unlink
    trick — works at 0 free. Next insert recreates it empty."""
    global _lcon, _log_mem
    if _LOG_DB == _MEM:
        return 0
    before = _db_size(_LOG_DB)
    with _llock:
        try:
            if _lcon is not None:
                _lcon.close()
        except sqlite3.Error:
            pass
        _lcon = None
        with _rlock:
            _invalidate_reader("log")
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(_LOG_DB + suffix)
            except OSError:
                pass
    return before


def _reopen_db(name):
    """Recreate a deleted log/diag database. CALLER MUST ALREADY HOLD the
    owning write lock (_llock for 'log', _dlock for 'diag') — every caller
    in this file does. (Re-acquiring here would self-deadlock: the lock is
    not reentrant.)"""
    if name == "diag":
        global _dcon, _diag_mem
        _dcon, _diag_mem = _open(_DIAG_DB)
        _dcon.executescript("""
            CREATE TABLE IF NOT EXISTS captures (
              request_id INTEGER PRIMARY KEY,
              ts REAL NOT NULL,
              ok INTEGER DEFAULT 1,
              data BLOB);
            CREATE TABLE IF NOT EXISTS responses (
              id TEXT PRIMARY KEY, created REAL, model TEXT,
              status TEXT, account TEXT, session_key TEXT,
              items_json TEXT, response_json TEXT);
            CREATE TABLE IF NOT EXISTS request_blobs (
              request_id INTEGER NOT NULL, kind TEXT NOT NULL,
              data BLOB, PRIMARY KEY (request_id, kind));
            """)
        _dcon.commit()
        _invalidate_reader("diag")
        return _dcon
    if name == "log":
        global _lcon, _log_mem
        _lcon, _log_mem = _open(_LOG_DB)
        _lcon.executescript("""
            CREATE TABLE IF NOT EXISTS requests (
              id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
              model TEXT, resolved_model TEXT,
              stream INTEGER DEFAULT 0, ok INTEGER DEFAULT 1,
              status INTEGER DEFAULT 200, error TEXT,
              prompt_tokens INTEGER DEFAULT 0,
              completion_tokens INTEGER DEFAULT 0,
              latency_ms INTEGER DEFAULT 0, ttft_ms INTEGER,
              client TEXT, key_name TEXT, account TEXT, endpoint TEXT);
            CREATE INDEX IF NOT EXISTS idx_req_ts ON requests(ts);
            CREATE INDEX IF NOT EXISTS idx_req_model ON requests(model);
            CREATE INDEX IF NOT EXISTS idx_req_ok ON requests(ok);
            CREATE INDEX IF NOT EXISTS idx_req_account ON requests(account);
            """)
        _migrate_requests()
        _add_columns("requests", {
            "flags": "TEXT",
            "cached_tokens": "INTEGER DEFAULT 0",
            "cache_creation_tokens": "INTEGER DEFAULT 0",
            "gen_ms": "INTEGER",
            "tps": "REAL",
            "upstream_model": "TEXT",
            "upstream_msg_id": "TEXT",
            "upstream_req_id": "TEXT",
        }, con=_lcon)
        _lcon.commit()
        _invalidate_reader("log")
        return _lcon


def _dcon_or_reopen():
    return _dcon if _dcon is not None else _reopen_db("diag")


def _lcon_or_reopen():
    return _lcon if _lcon is not None else _reopen_db("log")


def free_space(aggressive=True, vacuum=True, drop_diag=False):
    """Reclaim disk space in escalating stages, each stage needing less
    free space than the last so it still works when the disk is at 0:

      1. release the sacrificial reserve file          (needs nothing)
      2. prune old log/diag rows + checkpoint WALs      (needs ~KB of WAL)
      3. optional VACUUM                               (needs ~db size free)
      4. drop_diag=True or ENOSPC during step 2:
         delete the whole diag db file                 (needs nothing)
      5. still failing -> drop the log db too           (needs nothing)

    Safe to call anytime, never raises. Also the manual admin action."""
    freed = {"reserve": False, "diag_db": 0, "log_db": 0,
             "db_size_before": 0, "db_size_after": 0}
    before = (_db_size(_DB_PATH) + _db_size(_LOG_DB) + _db_size(_DIAG_DB))
    freed["db_size_before"] = before
    freed["reserve"] = _release_reserve()

    keep = _EMERGENCY_ROWS if aggressive else max(_EMERGENCY_ROWS,
                                                  _MAX_ROWS // 2)
    cutoff = time.time() - (3600 if aggressive else 86400)

    # ---- log db: prune old request rows
    try:
        with _llock:
            con = _lcon_or_reopen()
            con.execute(
                "DELETE FROM requests WHERE id < "
                "(SELECT MIN(id) FROM (SELECT id FROM requests"
                " ORDER BY id DESC LIMIT ?))", (keep,))
            con.commit()
            global _data_version
            _data_version += 1
            _checkpoint(con)
            if vacuum:
                try:
                    con.execute("VACUUM")
                except sqlite3.Error:
                    pass
    except sqlite3.Error as e:
        if _is_space_error(e):
            freed["log_db"] = drop_log_db()

    # ---- diag db: prune captures + old responses + request blobs
    try:
        with _dlock:
            con = _dcon_or_reopen()
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
            con.execute("DELETE FROM responses WHERE created<?", (cutoff,))
            # request_blobs are tied to log rows; drop blobs whose request
            # fell outside the kept window.
            try:
                min_keep = _q1(
                    "SELECT MIN(id) m FROM (SELECT id FROM requests"
                    " ORDER BY id DESC LIMIT ?)", (keep,), db="log")["m"]
            except sqlite3.Error:
                min_keep = None
            if min_keep is not None:
                con.execute("DELETE FROM request_blobs WHERE request_id<?",
                            (min_keep,))
            con.commit()
            _checkpoint(con)
            if vacuum:
                try:
                    con.execute("VACUUM")
                except sqlite3.Error:
                    pass
    except sqlite3.Error as e:
        if _is_space_error(e):
            freed["diag_db"] = drop_diag_db()

    # ---- explicit nuclear option (manual or after an ENOSPC above)
    if drop_diag and _DIAG_DB != _MEM and freed["diag_db"] == 0:
        freed["diag_db"] = drop_diag_db()

    # ---- sessions live in core; cheap to prune
    try:
        with _clock:
            if _con is not None:
                _con.execute("DELETE FROM sessions WHERE updated<?", (cutoff,))
                _con.commit()
                _checkpoint(_con)
    except sqlite3.Error:
        pass

    after = (_db_size(_DB_PATH) + _db_size(_LOG_DB) + _db_size(_DIAG_DB))
    freed["db_size_after"] = after
    freed["freed_bytes"] = max(0, before - after)
    _ensure_reserve()
    return freed


def _trim_log_file(path, max_bytes):
    """Keep the tail of a growing log file: rewrite last half when it
    exceeds max_bytes. Needs ~max_bytes/2 of temp room, so skip when the
    disk is already under the hard floor (the file delete is the safer
    tool then — see free_space)."""
    try:
        sz = os.path.getsize(path)
    except OSError:
        return False
    if sz <= max_bytes:
        return False
    try:
        with open(path, "rb") as f:
            f.seek(max(0, sz - max_bytes // 2))
            tail = f.read()
        tmp = path + ".trim"
        with open(tmp, "wb") as f:
            f.write(tail)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def _maintain_process_logs():
    """proxy.log / proxy.err sit next to the package and grow forever;
    cap them so a noisy upstream can't itself fill the disk."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cap = _LOG_MAX_MB * 1024 * 1024
    for name in ("proxy.log", "proxy.err"):
        _trim_log_file(os.path.join(here, name), cap)


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
                free = st["free"]
                if free is not None:
                    if free < _HARD_FREE_MB * 1024 * 1024:
                        # ~0 free: file-unlink is the only guaranteed win
                        free_space(aggressive=True, vacuum=False,
                                   drop_diag=True)
                    elif free < _SOFT_FREE_MB * 1024 * 1024:
                        free_space(aggressive=True, vacuum=False)
                    elif not st["reserve_active"]:
                        _ensure_reserve()
                _maintain_process_logs()
                # keep WALs bounded so reads never merge a huge -wal
                for con, lock in ((_con, _clock), (_lcon, _llock),
                                  (_dcon, _dlock)):
                    if con is None:
                        continue
                    try:
                        with lock:
                            con.execute("PRAGMA wal_checkpoint(PASSIVE)")
                    except sqlite3.Error:
                        pass
            except Exception:
                pass
    threading.Thread(target=loop, daemon=True, name="devin-proxy-maint").start()


def _start_blob_migration():
    """One-time background pass: move the legacy per-request payloads into
    diag.request_blobs.

    Two legacy shapes exist, both in the *old combined* db:
      a) inline text columns messages_json/request_json/events_json/sse_json
      b) the later messages_gz/request_gz/events_gz/sse_gz blob columns
    New writes go straight to diag.db, so this only drains the old file.
    Chunked + yielding so it never hogs a lock; once drained the old file
    is VACUUMed when there's room (the freed pages would otherwise sit on
    the freelist forever)."""
    global _mig_started
    if _mig_started or _db_mem or _DB_PATH == _MEM:
        return
    _mig_started = True

    def cols_present(con, table, cols):
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        return [c for c in cols if c in have]

    def run():
        migrated = 0
        try:
            while True:
                with _clock:
                    # find rows still holding any legacy payload
                    legacy_txt = cols_present(
                        _con, "requests",
                        ["messages_json", "request_json",
                         "events_json", "sse_json"])
                    legacy_gz = cols_present(
                        _con, "requests",
                        ["messages_gz", "request_gz",
                         "events_gz", "sse_gz"])
                    if not legacy_txt and not legacy_gz:
                        break
                    sel = ",".join(["id"] + legacy_txt + legacy_gz)
                    cond = " OR ".join(
                        f"{c} IS NOT NULL" for c in legacy_txt + legacy_gz)
                    ids = [r["id"] for r in _con.execute(
                        f"SELECT id FROM requests WHERE {cond}"
                        " ORDER BY id LIMIT 40").fetchall()]
                if not ids:
                    break
                migrated += len(ids)
                for rid in ids:
                    with _clock:
                        row = _con.execute(
                            f"SELECT {sel[3:]} FROM requests WHERE id=?",
                            (rid,)).fetchone()
                        if row is None:
                            continue
                        vals = dict(row)
                        # gather payloads: prefer *_gz, fall back to *_json
                        blobs = {}
                        for tcol, kind in _BLOB_COLS:
                            gcol = tcol.replace("_json", "_gz")
                            if gcol in vals and vals[gcol] is not None:
                                blobs[kind] = vals[gcol]
                            elif tcol in vals and vals[tcol] is not None:
                                blobs[kind] = _gz(vals[tcol])
                        # write to diag
                        try:
                            with _dlock:
                                dc = _dcon_or_reopen()
                                dc.executemany(
                                    "INSERT OR REPLACE INTO request_blobs"
                                    " (request_id,kind,data) VALUES (?,?,?)",
                                    [(rid, k, v) for k, v in blobs.items()])
                                dc.commit()
                        except sqlite3.Error:
                            pass
                        # clear the legacy columns in the core db
                        nulls = ",".join(
                            f"{c}=NULL" for c in legacy_txt + legacy_gz)
                        _con.execute(
                            f"UPDATE requests SET {nulls} WHERE id=?", (rid,))
                        _con.commit()
                    time.sleep(0.01)
            with _clock:
                _con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            return
        if not migrated:
            return
        # VACUUM rebuilds into a temp copy — only when the disk clearly has
        # room for it; otherwise the freelist is just reused going forward.
        try:
            free = shutil.disk_usage(_db_dir()).free
            if free > max(512 * 1024 * 1024, _safe_db_size() * 2):
                with _clock:
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
    disk is full) — the request itself must still succeed either way.

    Two writes: the light request row goes to the log db (source of all
    stats), then the heavy diagnostic payloads go to the diag db keyed by
    the new row id. A diag failure only loses bodies — the request row
    and its stats are already committed."""
    global _insert_count, _data_version
    # compress off-lock: ~400KB of debug JSON -> ~40KB, and the CPU work
    # doesn't hold the writer up.
    blobs = {"messages": _gz(messages_json), "request": _gz(request_json),
             "events": _gz(events_json), "sse": _gz(sse_json)}
    with _llock:
        con = _lcon_or_reopen()
        cur = con.execute(
            "INSERT INTO requests (ts,model,resolved_model,stream,ok,status,error,"
            "prompt_tokens,completion_tokens,latency_ms,ttft_ms,client,key_name,"
            "account,endpoint,flags,cached_tokens,cache_creation_tokens,gen_ms,tps,"
            "upstream_model,upstream_msg_id,upstream_req_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), model, resolved_model, int(stream), int(ok), status, error,
             prompt_tokens, completion_tokens, latency_ms, ttft_ms, client, key_name,
             account, endpoint, flags, cached_tokens, cache_creation_tokens,
             gen_ms, tps, upstream_model, upstream_msg_id, upstream_req_id))
        rid = cur.lastrowid
        _insert_count += 1
        if _insert_count % 100 == 0:
            keep = con.execute(
                "SELECT MIN(id) m FROM (SELECT id FROM requests"
                " ORDER BY id DESC LIMIT ?)", (_MAX_ROWS,)).fetchone()["m"]
            if keep is not None:
                con.execute("DELETE FROM requests WHERE id<?", (keep,))
        con.commit()
        _data_version += 1
    # diag write outside the log lock — its failure must not roll back the
    # request row we just committed.
    try:
        with _dlock:
            dc = _dcon_or_reopen()
            dc.executemany(
                "INSERT OR REPLACE INTO request_blobs"
                " (request_id,kind,data) VALUES (?,?,?)",
                [(rid, k, v) for k, v in blobs.items() if v is not None])
            dc.commit()
    except sqlite3.Error as e:
        if _is_space_error(e):
            _maybe_auto_cleanup()
    return rid


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
    with _dlock:
        dc = _dcon_or_reopen()
        dc.execute(
            "INSERT OR REPLACE INTO captures (request_id,ts,ok,data)"
            " VALUES (?,?,?,?)",
            (request_id, time.time(), int(ok), blob))
        _cap_saves += 1
        if _cap_saves % _CAP_PRUNE_EVERY == 0:
            dc.execute(
                "DELETE FROM captures WHERE ok=1 AND request_id < "
                "(SELECT MIN(request_id) FROM (SELECT request_id"
                " FROM captures WHERE ok=1 ORDER BY request_id DESC"
                " LIMIT ?))", (_CAP_KEEP,))
            dc.execute(
                "DELETE FROM captures WHERE ok=0 AND request_id < "
                "(SELECT MIN(request_id) FROM (SELECT request_id"
                " FROM captures WHERE ok=0 ORDER BY request_id DESC"
                " LIMIT ?))", (_CAP_FAIL_KEEP,))
        dc.commit()


@_resilient(default=None)
def get_capture(request_id):
    r = _q1("SELECT data FROM captures WHERE request_id=?", (request_id,),
            db="diag")
    if not r:
        return None
    try:
        return json.loads(gzip.decompress(r["data"]).decode("utf-8"))
    except Exception:
        return None


@_resilient(default=list)
def export_captures():
    """[{request_id, ok, data(gzip blob)}] for the diagnostic bundle."""
    rows = _q("SELECT request_id,ok,data FROM captures ORDER BY request_id",
              db="diag")
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
    # has_cap lives in the diag db now — count it there instead of the old
    # same-db subquery. One extra tiny query per page, keeps the list light.
    base_cols = _REQ_LIST_COLS.replace(
        ",(SELECT COUNT(*) FROM captures c"
        " WHERE c.request_id=requests.id) has_cap", "")
    if before_id:
        where += " AND id<?"
        args.append(before_id)
        rows = _q(f"SELECT {base_cols} FROM requests{where}"
                  " ORDER BY id DESC LIMIT ?", args + [limit], db="log")
    else:
        rows = _q(f"SELECT {base_cols} FROM requests{where}"
                  " ORDER BY id DESC LIMIT ? OFFSET ?",
                  args + [limit, offset], db="log")
    total = _q1(f"SELECT COUNT(*) c FROM requests{where}", args, db="log")["c"]
    items = [dict(r) for r in rows]
    ids = [r["id"] for r in items]
    caps = set()
    if ids:
        try:
            ph = ",".join("?" * len(ids))
            for r in _q(f"SELECT request_id FROM captures"
                        f" WHERE request_id IN ({ph})", ids, db="diag"):
                caps.add(r["request_id"])
        except sqlite3.Error:
            pass
    for r in items:
        r["has_cap"] = 1 if r["id"] in caps else 0
    return items, total


@_resilient(default=None)
def get_request(rid):
    r = _q1("SELECT * FROM requests WHERE id=?", (rid,), db="log")
    return _inflate(dict(r)) if r else None


@_resilient(default=None)
def clear_requests():
    global _data_version
    with _llock:
        con = _lcon_or_reopen()
        con.execute("DELETE FROM requests")
        con.commit()
        _data_version += 1
    try:
        with _dlock:
            dc = _dcon_or_reopen()
            dc.execute("DELETE FROM captures")
            dc.execute("DELETE FROM request_blobs")
            dc.commit()
    except sqlite3.Error:
        pass


@_resilient(default=0)
def prune_requests(model=None, ok=None, q=None, account=None, flag=None,
                   before_ts=None):
    """Delete logged requests matching the same filters as list_requests,
    plus an optional age cutoff. Returns the number of rows removed;
    diag blobs/captures are deleted for exactly those rows."""
    global _data_version
    where, args = _where(model, ok, q, account, flag)
    if before_ts:
        where += " AND ts<?"
        args.append(before_ts)
    with _llock:
        con = _lcon_or_reopen()
        ids = [r["id"] for r in con.execute(
            f"SELECT id FROM requests{where}", args).fetchall()]
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            ph = ",".join("?" * len(chunk))
            con.execute(f"DELETE FROM requests WHERE id IN ({ph})", chunk)
            try:
                with _dlock:
                    dc = _dcon_or_reopen()
                    dc.execute(
                        f"DELETE FROM captures WHERE request_id IN ({ph})",
                        chunk)
                    dc.execute(
                        f"DELETE FROM request_blobs WHERE request_id IN ({ph})",
                        chunk)
                    dc.commit()
            except sqlite3.Error:
                pass
        con.commit()
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


# every column the overview needs — requests now holds *only* these light
# columns, so the single scan reads leaf pages end to end.
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
    con = _READERS["log"] if _READERS["log"] is not None else _lcon_or_reopen()
    # the reader may be a real query-only connection (own _rlock) or the
    # writer itself (:memory:); pick the right lock.
    lock = _rlock if con is not _lcon else _llock
    with lock:
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
        db_size = _db_size(_LOG_DB)

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
      FROM requests GROUP BY m ORDER BY n DESC""", db="log")
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
    with _clock:
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
    with _clock:
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
    with _clock:
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
    with _clock:
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
        _maybe_auto_cleanup()  # must run outside _clock — it re-acquires it
    if r is None:
        return None
    d = dict(r)
    d["models"] = [m for m in (d.get("models") or "").split(",") if m]
    return d


@_resilient(default=lambda: True)  # fail closed: assume auth required
def has_keys():
    with _clock:
        return _conn().execute(
            "SELECT COUNT(*) c FROM api_keys").fetchone()["c"] > 0


@_resilient(default=lambda: {"requests": 0, "keys": 0, "accounts": 0,
                             "captures": 0})
def counts():
    r = _q1("SELECT (SELECT COUNT(*) FROM requests) reqs", db="log")
    k = _q1("SELECT (SELECT COUNT(*) FROM api_keys) keys,"
            " (SELECT COUNT(*) FROM accounts) accs")
    c = _q1("SELECT (SELECT COUNT(*) FROM captures) caps", db="diag")
    return {"requests": (r or {"reqs": 0})["reqs"],
            "keys": (k or {"keys": 0})["keys"],
            "accounts": (k or {"accs": 0})["accs"],
            "captures": (c or {"caps": 0})["caps"]}


# ---------- upstream accounts ----------

@_resilient(default=None)
def add_account(token, name=None, email=None, api_server_url=None,
                devin_webapp_host=None, devin_api_url=None, source=None,
                plan=None):
    """Insert an upstream account; dedup by token. -> row dict or None if dup
    (or if the write couldn't be persisted, e.g. disk full)."""
    with _clock:
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
    with _clock:
        _conn().execute(f"UPDATE accounts SET {','.join(sets)} WHERE id=?",
                        args + [aid])
        _conn().commit()


@_resilient(default=None)
def get_account(aid):
    with _clock:
        r = _conn().execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
    return dict(r) if r else None


@_resilient(default=list)
def list_accounts():
    rows = _q("SELECT * FROM accounts ORDER BY id")
    return [dict(r) for r in rows]


@_resilient(default=None)
def delete_account(aid):
    with _clock:
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
      FROM requests WHERE account IS NOT NULL GROUP BY a""", db="log")
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
        (since, max(1, min(limit, 100))), db="log")
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
    with _clock:
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
    with _clock:
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
    with _clock:
        _conn().execute("DELETE FROM sessions WHERE session_key=?", (session_key,))
        _conn().commit()


@_resilient(default=None)
def prune_pins(ttl_s):
    with _clock:
        _conn().execute("DELETE FROM sessions WHERE updated<?",
                        (time.time() - ttl_s,))
        _conn().commit()


# ---------- stored responses (previous_response_id chain) ----------

@_resilient(default=None)
def save_response(rid, model, status, account, session_key, items_json, response_json):
    with _dlock:
        dc = _dcon_or_reopen()
        dc.execute(
            "INSERT OR REPLACE INTO responses"
            " (id,created,model,status,account,session_key,items_json,response_json)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (rid, time.time(), model, status, account, session_key,
             items_json, response_json))
        dc.commit()


@_resilient(default=None)
def get_response(rid):
    r = _q1("SELECT * FROM responses WHERE id=?", (rid,), db="diag")
    return dict(r) if r else None


@_resilient(default=lambda: False)
def delete_response(rid):
    with _dlock:
        cur = _dcon_or_reopen().execute(
            "DELETE FROM responses WHERE id=?", (rid,))
        _dcon_or_reopen().commit()
    return cur.rowcount > 0


@_resilient(default=None)
def prune_responses(ttl_s=86400):
    with _dlock:
        dc = _dcon_or_reopen()
        dc.execute("DELETE FROM responses WHERE created<?",
                   (time.time() - ttl_s,))
        dc.commit()


# ---------- meta kv ----------

@_resilient(default=None)
def meta_get(k):
    """meta values are tiny and read on every /v1 call (model resolve,
    aliases, efforts) — cache them; meta_set writes through."""
    try:
        return _meta_cache[k]
    except KeyError:
        pass
    with _clock:
        r = _conn().execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    v = r["v"] if r else None
    _meta_cache[k] = v
    return v


@_resilient(default=None)
def meta_set(k, v):
    with _clock:
        _conn().execute(
            "INSERT INTO meta (k,v) VALUES (?,?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
        _conn().commit()
    _meta_cache[k] = v


# ---------- diagnostic bundle ----------

@_resilient(default=list)
def export_requests(limit=500, hours=0):
    """Full request rows for offline analysis — every column, newest first.
    Blobs are re-attached from the diag db so the bundle format is unchanged."""
    sql, args = "SELECT * FROM requests", []
    if hours and hours > 0:
        sql += " WHERE ts>=?"
        args.append(time.time() - hours * 3600)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(limit, _MAX_ROWS)))
    return [_inflate(dict(r)) for r in _q(sql, args, db="log")]


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
        (max(1, min(limit, 5000)),), db="diag")]
