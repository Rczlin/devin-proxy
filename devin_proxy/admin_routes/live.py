"""Live dashboard feed: /admin/api/ws pushes pool state over a WebSocket
so the console shows in-flight concurrency / cooldowns in real time
instead of polling /api/overview.

Security model (same bar as the REST routes):
- auth: the dp_admin session cookie or Bearer master key, checked at the
  handshake; unauthenticated/foreign-origin handshakes are refused before
  accept, so no frames ever flow
- CSWSH: browsers always send Origin on the ws handshake — it must match
  the Host the console was served from (SameSite=lax already withholds
  the cookie cross-site; this is defense in depth). Origin-less clients
  (curl/scripts) pass — they needed credentials to get this far anyway
- session expiry: a cookie-authed socket is closed when the token it
  presented expires, so a stale tab can't outlive its grant
- the channel is server-push only; inbound frames are drained purely to
  notice disconnects, and an oversized frame closes the socket
- frames are memory-only (pool objects + store data_version) — this fires
  on every request start/finish, so no SQL here; heavy stats stay on
  GET /api/overview, which the client refetches when data_v moves
"""
import asyncio
import hmac
import json
import time
import urllib.parse

from fastapi import WebSocket

from .. import store
from ..admin_ctx import _SESS_COOKIE

_TICK_S = 1.0           # cooldown countdowns tick once a second
_HEARTBEAT_S = 30.0     # unchanged state is still re-sent this often
_MAX_MSG = 2048         # inbound frame cap — the channel is push-only
_MAX_SUBS = 64          # fd/memory abuse guard


class LiveFeed:
    """Thread-safe fanout between the Pool (worker threads) and websocket
    tasks (event loop). Each subscriber has a 1-slot queue so a burst of
    pick/release notifications coalesces into a single push."""

    def __init__(self):
        self._subs = set()
        self._loop = None

    def bind(self, loop):
        self._loop = loop

    def notify(self):
        loop = self._loop
        if loop is None or not self._subs:
            return
        try:
            loop.call_soon_threadsafe(self._kick)
        except RuntimeError:
            pass                    # loop is shutting down

    def _kick(self):
        for q in list(self._subs):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass                # already pending — coalesced

    def subscribe(self):
        q = asyncio.Queue(maxsize=1)
        self._subs.add(q)
        return q

    def unsubscribe(self, q):
        self._subs.discard(q)


def _live_frame(ctx):
    pool = ctx.app.state.pool
    accs = pool.accounts()
    now = time.time()
    p = pool.summary()
    p["in_flight"] = sum(a.in_flight for a in accs)
    p["accounts"] = [{
        "id": a.id, "label": a.display(), "plan": a.plan,
        "state": "disabled" if a.disabled else (
            "cooldown" if a.cooldown_until > now else "ready"),
        "in_flight": a.in_flight,
        "max_concurrent": a.max_concurrent,
        "cooldown_s": max(0, round(a.cooldown_until - now)),
        "fails": a.consecutive_fails,
    } for a in accs]
    return {"type": "live", "ts": now, "uptime_s": now - ctx.started,
            "data_v": store._data_version, "pool": p}


def register(router, ctx, admin_key):
    feed = LiveFeed()
    ctx.app.state.live_feed = feed
    ctx.app.state.pool.on_change(feed.notify)

    def _ws_authed(ws):
        if ctx.sess_ok(ws.cookies.get(_SESS_COOKIE, "")):
            return True
        key = ctx.app.state.proxy_key
        if not key:
            return False
        auth = ws.headers.get("authorization", "")
        return auth.startswith("Bearer ") \
            and hmac.compare_digest(auth[7:], key)

    def _ws_origin_ok(ws):
        origin = ws.headers.get("origin")
        if not origin:
            return True
        try:
            ohost = urllib.parse.urlparse(origin).netloc
        except Exception:
            return False
        return bool(ohost) and hmac.compare_digest(
            ohost.lower(), ws.headers.get("host", "").lower())

    @router.websocket("/api/ws")
    async def live_ws(ws: WebSocket):
        if not _ws_origin_ok(ws):
            await ws.close(code=4403)
            return
        if not _ws_authed(ws):
            await ws.close(code=4401)
            return
        feed.bind(asyncio.get_running_loop())
        if len(feed._subs) >= _MAX_SUBS:
            await ws.close(code=1013)       # try again later
            return
        await ws.accept()
        deadline = None
        try:
            deadline = int(ws.cookies.get(
                _SESS_COOKIE, "").split(".", 1)[0])
        except (ValueError, IndexError):
            pass
        q = feed.subscribe()
        stopped = asyncio.Event()

        async def _drain():
            try:
                while True:
                    m = await ws.receive()
                    if m["type"] == "websocket.disconnect":
                        break
                    data = m.get("text") or m.get("bytes") or ""
                    if len(data) > _MAX_MSG:
                        break
            except Exception:
                pass
            stopped.set()

        reader = asyncio.create_task(_drain())
        last_key, last_sent = None, 0.0
        try:
            while not stopped.is_set():
                if deadline and time.time() >= deadline:
                    await ws.close(code=4401)
                    break
                f = _live_frame(ctx)
                key = json.dumps({"p": f["pool"], "v": f["data_v"]},
                                 ensure_ascii=False)
                if key != last_key or time.time() - last_sent > _HEARTBEAT_S:
                    await ws.send_text(json.dumps(f, ensure_ascii=False))
                    last_key, last_sent = key, time.time()
                wait = _TICK_S
                if deadline:
                    wait = max(0.1, min(wait, deadline - time.time()))
                try:
                    await asyncio.wait_for(q.get(), wait)
                except asyncio.TimeoutError:
                    pass
        except Exception:
            pass                            # send/close raced — drop it
        finally:
            stopped.set()
            reader.cancel()
            feed.unsubscribe(q)
            await asyncio.gather(reader, return_exceptions=True)
            try:
                await ws.close()
            except Exception:
                pass
