"""Request-log admin routes: list/detail/capture, filtered prune,
CSV/JSONL export, error rollup."""
import io
import json
import time

from fastapi import Depends, HTTPException, Response

from .. import store
from ..admin_ctx import AdminCtx

_MAX_EXPORT = 20000          # row cap for CSV/JSONL log export


def register(router, ctx, admin_key):
    @router.get("/api/requests", dependencies=[Depends(admin_key)])
    def requests(limit: int = 50, offset: int = 0, before: int = None,
                 model: str = None, ok: int = None, q: str = None,
                 account: str = None, flag: str = None):
        limit = max(1, min(limit, 200))
        items, total = store.list_requests(limit, offset, model, ok, q,
                                           account, flag, before_id=before)
        # next_cursor lets the UI page by keyset instead of offset —
        # stable while new requests arrive between pages.
        return {"items": items, "total": total,
                "next_cursor": items[-1]["id"] if items else None}

    @router.get("/api/requests/export", dependencies=[Depends(admin_key)])
    def requests_export(fmt: str = "jsonl", limit: int = 1000,
                        model: str = None, ok: int = None, q: str = None,
                        account: str = None, flag: str = None):
        """Download the filtered request log as JSONL or CSV (summary
        columns — no request/response bodies; use /api/export for full
        fidelity). Same filters as the list endpoint."""
        import csv
        if fmt not in ("jsonl", "csv"):
            raise HTTPException(400, "fmt must be jsonl or csv")
        items, _ = store.list_requests(max(1, min(limit, _MAX_EXPORT)),
                                       0, model, ok, q, account, flag)
        fn = "requests-" + time.strftime("%Y%m%d-%H%M%S")
        if fmt == "csv":
            buf = io.StringIO()
            if items:
                w = csv.DictWriter(buf, fieldnames=list(items[0].keys()))
                w.writeheader()
                w.writerows(items)
            return Response(
                buf.getvalue(), media_type="text/csv",
                headers={"Content-Disposition":
                         f'attachment; filename="{fn}.csv"'})
        body = "".join(json.dumps(r, ensure_ascii=False, default=str)
                       + "\n" for r in items)
        return Response(
            body, media_type="application/x-ndjson",
            headers={"Content-Disposition":
                     f'attachment; filename="{fn}.jsonl"'})

    @router.get("/api/requests/errors", dependencies=[Depends(admin_key)])
    def request_errors(hours: int = 24):
        """Error rollup for the log page — failures grouped by a stripped
        signature so 'timeout after 31.4s' / '…29.9s' count together."""
        return {"errors": store.error_stats(hours if hours >= 0 else 24)}

    @router.get("/api/requests/{rid}", dependencies=[Depends(admin_key)])
    def request_detail(rid: int):
        r = store.get_request(rid)
        if not r:
            raise HTTPException(404, "not found")
        return r

    @router.get("/api/requests/{rid}/capture",
                dependencies=[Depends(admin_key)])
    def request_capture(rid: int):
        """Download the full-fidelity capture for one request — raw inbound
        body/headers, exact upstream protobuf + every wire frame, outbound
        SSE. Untruncated."""
        c = store.get_capture(rid)
        if c is None:
            raise HTTPException(404, "no capture stored for this request")
        body = json.dumps(c, ensure_ascii=False, indent=1, default=str)
        return Response(
            content=body, media_type="application/json",
            headers={"Content-Disposition":
                     f'attachment; filename="req-{rid}-capture.json"'})

    @router.post("/api/requests/clear", dependencies=[Depends(admin_key)])
    def clear_requests(model: str = None, ok: int = None, q: str = None,
                       account: str = None, flag: str = None,
                       older_than_hours: float = None):
        """Delete logged requests. With no filters this wipes everything;
        with filters (same as GET /api/requests plus an age cutoff) it prunes
        just the matching rows — keeps the useful history intact."""
        if not any(v is not None for v in
                   (model, ok, q, account, flag, older_than_hours)):
            store.clear_requests()
            return {"ok": True, "deleted": "all"}
        n = store.prune_requests(
            model=model, ok=ok, q=q, account=account, flag=flag,
            before_ts=(time.time() - older_than_hours * 3600
                       if older_than_hours else None))
        return {"ok": True, "deleted": n}

    def _git_head():
        try:
            import subprocess
            here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                                 cwd=here, capture_output=True, timeout=3,
                                 text=True)
            return out.stdout.strip() or None
        except Exception:
            return None

