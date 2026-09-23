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
- alternative auth: a 60s ticket from GET /api/ws-ticket (itself behind
  the same session cookie) accepted via ?t= — survives proxies that
  rewrite/ strip every identifying header, and still can't be minted by
  a cross-site page
"""
import asyncio
import hmac
import json
import time
import urllib.parse

from fastapi import Depends, WebSocket

from .. import store
from ..admin_ctx import _SESS_COOKIE

_TICK_S = 1.0           # cooldown countdowns tick once a second
_HEARTBEAT_S = 10.0     # unchanged state is still re-sent this often —
                        # keeps the conn alive through proxies with
                        # aggressive idle timeouts (data frames only)
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
        """CSWSH check. Browsers always send Origin on the handshake — its
        hostname must match the host the page was served from. Reverse
        proxies may rewrite Host (nginx's default $proxy_host), so the
        client-facing host also comes from X-Forwarded-Host / Forwarded.
        Compared by hostname only: a port rewrite mid-proxy doesn't turn
        the page cross-site, and an attacker page can never make its
        Origin hostname equal ours anyway."""
        origin = ws.headers.get("origin")
        if not origin:
            return True                 # non-browser clients carry no Origin
        try:
            ohost = urllib.parse.urlparse(origin).hostname
        except Exception:
            return False
        if not ohost:
            return False                # e.g. Origin: null
        ohost = ohost.lower().rstrip(".")
        cand = {ws.headers.get("host", "")}
        for h in ws.headers.get("x-forwarded-host", "").split(","):
            cand.add(h.strip())
        for part in ws.headers.get("forwarded", "").replace(",", ";").split(";"):
            part = part.strip()
            if part.lower().startswith("host="):
                cand.add(part[5:].strip('"').strip())
        for c in cand:
            try:
                chost = urllib.parse.urlparse("//" + c).hostname
            except Exception:
                continue
            if chost and hmac.compare_digest(ohost, chost.lower()):
                return True
        print(f"admin ws: rejected — origin host {ohost!r} not in "
              f"{sorted(c for c in cand if c)}")
        return False

    @router.get("/api/ws-ticket", dependencies=[Depends(admin_key)])
    def ws_ticket():
        """Mint a short-lived credential for the ws handshake. The SPA
        fetches it right before connecting, so expiry only gates entry —
        the socket itself is not bound by the ticket's ttl."""
        return {"ticket": ctx.ws_ticket(), "ttl": 60}

    @router.websocket("/api/ws")
    async def live_ws(ws: WebSocket):
        by_ticket = ctx.ws_ticket_ok(ws.query_params.get("t") or "")
        if not by_ticket:
            if not _ws_origin_ok(ws):
                await ws.close(code=4403)
                return
            if not _ws_authed(ws):
                print("admin ws: rejected — bad/missing credential")
                await ws.close(code=4401)
                return
        feed.bind(asyncio.get_running_loop())
        if len(feed._subs) >= _MAX_SUBS:
            await ws.close(code=1013)       # try again later
            return
        await ws.accept()
        deadline = None
        if not by_ticket:
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
        t_open = time.time()
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
        except Exception as e:
            print(f"admin ws: dropped after "
                  f"{time.time() - t_open:.0f}s — {type(e).__name__}: {e}")
        finally:
            stopped.set()
            reader.cancel()
            feed.unsubscribe(q)
            await asyncio.gather(reader, return_exceptions=True)
            try:
                await ws.close()
            except Exception:
                pass
