"""Model catalog — fetched from upstream, not hardcoded.

Sources merged into one catalog:

- remote  — GetCliModelConfigs against every enabled upstream account
            (the same RPC Devin CLI/Desktop makes at boot), unioned;
            per-account visibility is kept for scheduling
- url     — optional JSON catalog URL (DEVIN_PROXY_MODELS_URL or the
            admin setting) for lists maintained outside this repo

The snapshot is kept in memory and persisted in meta (`model_catalog_v1`),
so restarts and upstream outages keep the last good list. Refresh is
TTL-based and never blocks a request: maybe_refresh() kicks a daemon
thread when the snapshot is stale. With no snapshot the catalog is empty —
model names pass through to upstream untouched.
"""
import json
import os
import threading
import time

from . import store, upstream

TTL_S = int(os.environ.get("DEVIN_PROXY_MODELS_TTL", "3600"))
_URL_ENV = (os.environ.get("DEVIN_PROXY_MODELS_URL") or "").strip()

DEFAULT_UID = "claude-sonnet-5-medium"   # last-resort fallback only

# alias -> (family, preferred effort, fallback uid when catalog is empty)
ALIAS_DEFS = {
    "claude":  ("claude-sonnet-5",   "medium", "claude-sonnet-5-medium"),
    "sonnet":  ("claude-sonnet-5",   "medium", "claude-sonnet-5-medium"),
    "opus":    ("claude-opus-5",     "high",   "claude-opus-5-high"),
    "gemini":  ("gemini-3-8-flash",  "medium", "gemini-3-8-flash-medium"),
    "gpt":     ("gpt-5-6-sol",       "medium", "gpt-5-6-sol-medium"),
    "codex":   ("gpt-5-6-sol",       "medium", "gpt-5-6-sol-medium"),
    "swe":     ("swe-2",             "high",   "swe-2-high"),
    "default": None,    # -> default_uid()
    "auto":    None,
}

EFFORTS = ["none", "low", "medium", "high", "xhigh", "max"]
EFFORT_LABEL = {"none": "无思考", "low": "低", "medium": "中",
                "high": "高", "xhigh": "超高", "max": "满"}
# reasoning.effort / reasoning_effort -> uid suffix
EFFORT_SUFFIX = {"minimal": "none", "none": "none", "low": "low",
                 "medium": "medium", "high": "high",
                 "xhigh": "xhigh", "max": "max"}
_UID_SUFFIXES = set(EFFORTS)

# (prefix, label, vendor, desc, default effort for a bare family name) —
# display metadata only; the catalog itself comes from remote sync.
FAMILIES = [
    ("claude-opus-5",      "Claude Opus 5",     "Anthropic",
     "旗舰模型，最强推理与编码", "high"),
    ("claude-sonnet-5",    "Claude Sonnet 5",   "Anthropic",
     "均衡主力，日常首选", "medium"),
    ("claude-fable-5-1",   "Claude Fable 5.1",  "Anthropic",
     "轻量快速", "medium"),
    ("gpt-5-6-sol",        "GPT-5.6 Sol",       "OpenAI", "", "medium"),
    ("gpt-5-6-luna",       "GPT-5.6 Luna",      "OpenAI", "", "medium"),
    ("gemini-3-8-flash",   "Gemini 3.8 Flash",  "Google", "", "medium"),
    ("swe-1-7-lightning",  "SWE-1.7 Lightning", "Cognition",
     "极速变体", "medium"),
    ("swe-1-7",            "SWE-1.7",           "Cognition",
     "Cognition 自研 agentic 模型", "medium"),
    ("swe-2",              "SWE-2",             "Cognition",
     "Cognition 自研 agentic 模型", "high"),
]
_FAM_SORT = sorted(FAMILIES, key=lambda f: -len(f[0]))
# families tried in order when picking the default model
_DEFAULT_PREF = ("claude-sonnet-5", "gpt-5-6-sol", "swe-2",
                 "gemini-3-8-flash")

_VENDOR_GUESS = (("claude", "Anthropic"), ("gpt", "OpenAI"),
                 ("gemini", "Google"), ("swe", "Cognition"),
                 ("deepseek", "DeepSeek"), ("kimi", "Moonshot"),
                 ("glm", "Zhipu"), ("qwen", "Alibaba"), ("grok", "xAI"))
_TOKEN = {"gpt": "GPT", "swe": "SWE", "glm": "GLM", "kimi": "Kimi",
          "xhigh": "XHigh"}


def split_effort(uid):
    """'claude-opus-5-high' -> ('claude-opus-5', 'high'); none -> (uid, None)."""
    base, _, suf = str(uid or "").rpartition("-")
    return (base, suf) if base and suf in _UID_SUFFIXES else (uid, None)


def _title(slug):
    return " ".join(_TOKEN.get(w, w.capitalize())
                    for w in str(slug or "").replace("_", "-").split("-")
                    if w)


def _vendor_of(uid):
    head = str(uid or "").split("-", 1)[0]
    for pre, v in _VENDOR_GUESS:
        if head.startswith(pre):
            return v
    return "Devin"


def _norm_fam(s):
    """Upstream family slugs sometimes use dots where uids use dashes
    ('gpt-5.6-sol' vs 'gpt-5-6-sol-medium') — compare normalized."""
    return str(s or "").replace(".", "-")


def _family_of(uid, slug=None):
    """Longest-prefix family match; falls back to the remote family slug
    or the uid minus its effort suffix."""
    for i, (pre, label, vendor, desc, dflt) in enumerate(_FAM_SORT):
        if uid == pre or uid.startswith(pre + "-"):
            return {"prefix": pre, "label": label, "vendor": vendor,
                    "desc": desc, "default": dflt, "order": i}
    fam = _norm_fam(slug) or split_effort(uid)[0] or uid
    return {"prefix": fam, "label": _title(fam), "vendor": _vendor_of(uid),
            "desc": "", "default": "medium", "order": 999}


def _label_of(uid, fam, effort):
    if effort:
        return f"{fam['label']} · {EFFORT_LABEL.get(effort, effort)}"
    return fam["label"]


def _eff_idx(e):
    try:
        return EFFORTS.index(e["effort"])
    except ValueError:
        return len(EFFORTS)


def _entry(uid, **kw):
    fam = _family_of(uid, kw.get("family_slug"))
    _, effort = split_effort(uid)
    fam_label = kw.get("family_label") or fam["label"]
    e = {"uid": uid, "family": fam["prefix"], "family_label": fam_label,
         "vendor": fam["vendor"], "effort": effort,
         "effort_label": EFFORT_LABEL.get(effort) if effort else None,
         "order": fam["order"],
         "label": kw.get("label") or _label_of(uid, fam, effort),
         "context": None, "max_output": None, "credit": None,
         "images": None, "thinking": None, "alias": None,
         "cost_summary": None, "pricing": None, "deployment": None,
         "remote_accounts": 0, "url": False}
    for k, v in kw.items():
        if k in e and v is not None:
            e[k] = v
    return e


# ---------- remote snapshot (memory + meta persistence) ----------

_lock = threading.Lock()
_remote = {"ts": 0, "models": {}, "errors": [], "url_err": None}
_loaded = False
_refreshing = False


def _load():
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        d = json.loads(store.meta_get("model_catalog_v1") or "")
        if isinstance(d.get("models"), dict):
            for k in ("ts", "models", "errors", "url_err"):
                if k in d:
                    _remote[k] = d[k]
    except Exception:
        pass


def user_aliases():
    """-> {alias: "uid" | "uid@effort"} as stored."""
    try:
        d = json.loads(store.meta_get("model_aliases") or "{}")
        return ({str(k).strip(): str(v).strip() for k, v in d.items()
                 if str(k).strip() and str(v).strip()}
                if isinstance(d, dict) else {})
    except Exception:
        return {}


def _alias_target(v):
    """'uid@effort' -> (uid, effort); 'uid' -> (uid, None)."""
    uid, _, eff = str(v or "").partition("@")
    eff = eff.strip().lower()
    return uid.strip(), (eff if eff in EFFORT_SUFFIX else None)


def alias_effort(name):
    """Per-alias default effort, e.g. 'opus=claude-opus-5@low'."""
    ua = user_aliases()
    if str(name or "").strip() in ua:
        return _alias_target(ua[str(name).strip()])[1]
    return None


def family_efforts():
    """-> {family: effort} per-family default variants (admin setting)."""
    try:
        d = json.loads(store.meta_get("family_efforts") or "{}")
        if isinstance(d, dict):
            return {_norm_fam(k): v.strip().lower()
                    for k, v in d.items()
                    if str(v).strip().lower() in EFFORT_SUFFIX}
    except Exception:
        pass
    return {}


def default_effort():
    """Global default effort (admin setting), or None."""
    e = (store.meta_get("default_effort") or "").strip().lower()
    return e if e in EFFORT_SUFFIX else None


def hidden_aliases():
    """Builtin/remote aliases the admin hid (they can't be deleted at
    the source, so they're filtered out here)."""
    try:
        d = json.loads(store.meta_get("alias_hide") or "[]")
        return {str(x) for x in d} if isinstance(d, list) else set()
    except Exception:
        return set()


def hidden_models():
    """Variant uids the admin hid — dropped from the catalog listing and
    default-variant picks; explicit uid requests still pass through."""
    try:
        d = json.loads(store.meta_get("model_hide") or "[]")
        return {str(x) for x in d} if isinstance(d, list) else set()
    except Exception:
        return set()


def _family_of_name(n):
    """Alias/bare name -> family slug: builtin alias spec, remote alias,
    else the name itself. Hidden aliases don't route."""
    if n in hidden_aliases():
        return n
    spec = ALIAS_DEFS.get(n)
    if n in ALIAS_DEFS:
        return spec[0] if spec else None
    ra = _remote_aliases()
    if n in ra:
        return ra[n]
    return n


def auto_effort(name):
    """Effort to auto-apply when the request didn't specify
    reasoning.effort. Precedence: alias's own @effort > per-family
    default > global default. A variant suffix on the requested name
    ('…-high') pins it; the configured default_model's suffix does not
    suppress the global default."""
    n = str(name or "").strip()
    pinned = bool(n)                 # an explicitly requested model
    if not n:
        n = (store.meta_get("default_model") or "").strip()
        if not n:
            return default_effort()
    if pinned and split_effort(n)[1] is not None:
        return None
    ua = user_aliases()
    if n in ua:
        uid, eff = _alias_target(ua[n])
        if eff:
            return eff
        if pinned and split_effort(uid)[1] is not None:
            return None
        n = uid                      # target may be a family name
    fam = _family_of_name(n)
    if fam:
        fam = _norm_fam(split_effort(fam)[0] or fam)
    fe = family_efforts().get(fam) if fam else None
    return fe or default_effort()


def entries(include_hidden=False):
    """Merged catalog -> [entry] sorted by family order then effort.
    Remote-only: empty until the first successful sync."""
    _load()
    all_hid = hidden_models()
    hid = set() if include_hidden else all_hid
    with _lock:
        rem = dict(_remote["models"])
    out = []
    for uid, m in rem.items():
        if uid in hid:
            continue
        e = _entry(uid, family_slug=m.get("family_slug"),
                   family_label=m.get("family_label"),
                   label=m.get("label"))
        for k in ("context", "max_output", "credit", "images",
                  "thinking", "alias", "cost_summary", "pricing",
                  "deployment"):
            if m.get(k) is not None:
                e[k] = m[k]
        if m.get("accounts"):
            e["remote_accounts"] = len(set(m["accounts"]))
        if m.get("url"):
            e["url"] = True
        e["hidden"] = uid in all_hid
        out.append(e)
    return sorted(out, key=lambda e: (e["order"], _eff_idx(e), e["uid"]))


def uids():
    return set(_remote_uids())


def _remote_uids():
    _load()
    with _lock:
        return _remote["models"].keys()


def family_default(name, prefer=None):
    """Bare family slug -> a catalog uid: preferred effort if present."""
    n = _norm_fam(str(name or "").strip())
    if not n:
        return None
    fam = next((f for f in _FAM_SORT if f[0] == n), None)
    cands = [e for e in entries()
             if e["family"] == (fam[0] if fam else n)]
    if not cands:
        return None
    over = family_efforts().get(fam[0] if fam else n)
    for want in [over, prefer or (fam[4] if fam else "medium"),
                 "medium"]:
        for e in cands:
            if e["effort"] == want:
                return e["uid"]
    return cands[0]["uid"]


def _remote_aliases():
    """alias -> family slug, from upstream model_info.alias (each
    variant config repeats the family alias, e.g. 'opus')."""
    _load()
    out = {}
    with _lock:
        for m in _remote["models"].values():
            a, fam = m.get("alias"), m.get("family_slug")
            if a and fam and a not in out:
                out[a] = fam
    return out


def _resolve(name, use_defaults=True):
    n = str(name or "").strip()
    if not n:
        return None
    ua = user_aliases()
    if n in ua:
        t, _ = _alias_target(ua[n])
        return t if t in uids() else (family_default(t) or t)
    hidden = hidden_aliases()
    spec = ALIAS_DEFS.get(n)
    if n in ALIAS_DEFS and n not in hidden:
        if spec is None:
            return default_uid() if use_defaults else DEFAULT_UID
        fam, pref, fallback = spec
        return family_default(fam, pref) or fallback
    if n in uids():
        return n
    ra = _remote_aliases()
    if n in ra and n not in hidden:
        return family_default(ra[n]) or n
    return family_default(n) or n


def resolve(name):
    """Requested model name -> uid: user alias, builtin alias, known uid,
    bare family name, else the name itself (pass-through — upstream may
    know models our snapshot doesn't)."""
    return _resolve(name, use_defaults=True)


def default_uid():
    m = (store.meta_get("default_model") or "").strip()
    if m:
        return _resolve(m, use_defaults=False) or DEFAULT_UID
    # pick a sensible catalog default when no override is set
    for fam in _DEFAULT_PREF:
        hit = family_default(fam)
        if hit:
            return hit
    ents = entries()
    return ents[0]["uid"] if ents else DEFAULT_UID


def aliases():
    """alias -> resolved uid. Builtin aliases are listed only while their
    target exists in the catalog (or the catalog is still empty)."""
    known = uids()
    hidden = hidden_aliases()
    out = {}
    for a in user_aliases():
        t = _resolve(a)
        if t:
            out[a] = t
    for a in ALIAS_DEFS:
        if a in hidden:
            continue
        t = _resolve(a)
        if t and (not known or t in known):
            out.setdefault(a, t)
    for a in _remote_aliases():
        if a in hidden:
            continue
        t = _resolve(a)
        if t and (not known or t in known):
            out.setdefault(a, t)
    return out


def apply_effort(uid, effort):
    """reasoning effort -> swap the uid's effort suffix. Gated on the
    catalog when one has been fetched; best-effort otherwise."""
    suf = EFFORT_SUFFIX.get(str(effort or "").strip().lower())
    if not suf:
        return uid
    base, cur = split_effort(uid)
    if cur is None:
        return uid
    cand = f"{base}-{suf}"
    known = uids() - hidden_models()
    return cand if (not known or cand in known) else uid


def max_output_for(uid):
    """Catalog max_output for a resolved uid, or None when unknown."""
    for e in entries(include_hidden=True):
        if e["uid"] == uid:
            return e.get("max_output")
    return None


def grouped(include_hidden=False):
    """entries() grouped by family -> [{prefix,label,vendor,desc,models}].

    f["default"] is the effort a request without reasoning.effort actually
    lands on: family override > global default > builtin preference,
    restricted to variants the family really has (auto_effort/apply_effort
    degrade the same way). f["default_override"] is the raw admin override
    so UIs can tell a set value apart from an inherited one."""
    fams = {}
    for e in entries(include_hidden):
        f = fams.setdefault(e["family"], {
            "prefix": e["family"], "label": e["family_label"],
            "vendor": e["vendor"], "desc": "", "models": []})
        f["models"].append(e)
    out = list(fams.values())
    overs = family_efforts()
    glob = default_effort()
    for f in out:
        meta = next((x for x in FAMILIES if x[0] == f["prefix"]), None)
        f["desc"] = meta[3] if meta else ""
        have = sorted({m["effort"] for m in f["models"]
                       if m["effort"] and not m.get("hidden")},
                      key=lambda e: EFFORTS.index(e)
                      if e in EFFORTS else len(EFFORTS))
        f["default"] = next(
            (e for e in (overs.get(f["prefix"]), glob,
                         meta[4] if meta else None, "medium")
             if e in have), have[0] if have else None)
        f["default_override"] = overs.get(f["prefix"])
        f["order"] = min(m["order"] for m in f["models"])
    return sorted(out, key=lambda f: (f["order"], f["prefix"]))


def models_url():
    return (store.meta_get("models_url") or _URL_ENV or "").strip()


def sync_info():
    _load()
    with _lock:
        per_acct = {}
        for m in _remote["models"].values():
            for aid in m.get("accounts") or []:
                per_acct[aid] = per_acct.get(aid, 0) + 1
        return {"ts": _remote["ts"] or 0,
                "count": len(_remote["models"]),
                "accounts": per_acct,
                "errors": list(_remote["errors"]),
                "url": models_url(),
                "url_err": _remote["url_err"],
                "refreshing": _refreshing, "ttl": TTL_S}


def served_by(want):
    """Which accounts advertised any of `want` uids.
    -> (allowed_ids, known_ids) or None when the snapshot can't say
    (empty, or the wanted uid isn't covered by remote data)."""
    _load()
    with _lock:
        rem = _remote["models"]
    if not rem:
        return None
    allowed, known, hit = set(), set(), False
    for uid, m in rem.items():
        aids = set(m.get("accounts") or [])
        known |= aids
        if uid in want and aids:
            allowed |= aids
            hit = True
    return (allowed, known) if hit else None


def stale():
    _load()
    return time.time() - (_remote["ts"] or 0) > TTL_S


def maybe_refresh(client, pool):
    """Kick a background sync when the snapshot is stale; never blocks."""
    if not stale():
        return
    accs = [a for a in pool.accounts() if not a.disabled] if pool else []
    if not accs and not models_url():
        return
    threading.Thread(target=refresh, args=(client, accs),
                     kwargs={"force": True}, daemon=True).start()


def _fetch_url(client, models):
    """Optional remote JSON catalog: [uid,...] / [{uid|id,label,...},...] /
    {"models": [...]}. Returns an error string or None."""
    url = models_url()
    if not url:
        return None
    try:
        r = client.get(url, timeout=15)
        r.raise_for_status()
        d = r.json()
        if isinstance(d, dict):
            items = d.get("models") or d.get("data") or []
        elif isinstance(d, list):
            items = d
        else:
            items = []
        for it in items:
            if isinstance(it, str):
                it = {"uid": it}
            if not isinstance(it, dict):
                continue
            uid = str(it.get("uid") or it.get("id") or "").strip()
            if not uid:
                continue
            e = models.get(uid)
            if e is None:
                e = models[uid] = {"uid": uid, "accounts": [], "url": False}
            e["url"] = True
            for dst, keys in (("label", ("label", "name")),
                              ("context", ("context", "context_window",
                                           "max_tokens")),
                              ("max_output", ("max_output",
                                              "max_output_tokens"))):
                for kk in keys:
                    if e.get(dst) is None and it.get(kk) is not None:
                        e[dst] = it[kk]
                        break
            for k in ("images", "thinking", "credit"):
                if e.get(k) is None and it.get(k) is not None:
                    e[k] = it[k]
        return None
    except Exception as ex:
        return str(ex)[:300]


def refresh(client, accounts, force=False):
    """Fetch remote model configs from every enabled account (+ the URL
    source), merge, persist. Returns a result dict for the admin UI."""
    global _refreshing
    _load()
    with _lock:
        if _refreshing:
            return {"ok": False, "skipped": "sync already running"}
        if not force and not stale():
            return {"ok": True, "skipped": "fresh", "ts": _remote["ts"],
                    "count": len(_remote["models"])}
        _refreshing = True
    try:
        models, errors = {}, []
        for a in accounts or []:
            try:
                for m in upstream.fetch_model_configs(
                        client, a.api_server_url, a.token):
                    e = models.get(m["uid"])
                    if e is None:
                        e = models[m["uid"]] = {"uid": m["uid"],
                                                "accounts": [],
                                                "url": False}
                    for k, v in m.items():
                        if k != "uid" and e.get(k) is None:
                            e[k] = v
                    if a.id not in e["accounts"]:
                        e["accounts"].append(a.id)
            except Exception as ex:
                errors.append({"account": a.display(),
                               "error": str(ex)[:200]})
        url_err = _fetch_url(client, models)
        with _lock:
            _remote.update({"ts": time.time(), "models": models,
                            "errors": errors, "url_err": url_err})
            snap = {"ts": _remote["ts"], "models": _remote["models"],
                    "errors": _remote["errors"],
                    "url_err": _remote["url_err"]}
        try:
            store.meta_set("model_catalog_v1", json.dumps(snap))
        except Exception:
            pass
        return {"ok": bool(models) or not errors, "count": len(models),
                "errors": errors, "url_err": url_err, "ts": snap["ts"]}
    finally:
        with _lock:
            _refreshing = False
