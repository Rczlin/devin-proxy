"""Playground: chat test routed through the normal pipeline."""
import time

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from typing import Optional


def register(router, ctx, admin_key):
    @router.post("/api/playground", dependencies=[Depends(admin_key)])
    async def playground(request: Request):
        """Chat test routed through the normal pipeline (logged, no key needed)."""
        body = await request.json()
        t0 = time.perf_counter()
        force = None
        if body.get("account_id"):
            try:
                aid = int(body["account_id"])
            except (TypeError, ValueError):
                raise HTTPException(400, "account_id must be an integer")
            force = ctx.app.state.pool.get(aid)
            if not force:
                raise HTTPException(404, "account not found")
        request.state.key_name = "admin"
        if body.get("stream"):
            return StreamingResponse(
                ctx.app.state.sse_stream(request, body,
                                     ctx.app.state.resolve_model(body),
                                     "admin:playground", t0, force),
                media_type="text/event-stream")
        resp = await run_in_threadpool(
            ctx.app.state.collect, request, body,
            ctx.app.state.resolve_model(body), "admin:playground", t0, force)
        return JSONResponse(resp)

