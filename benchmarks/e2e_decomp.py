"""Stage ablations for the socket-level ``e2e`` benchmark.

Serve this module through ``benchmarks.wreath_server --app
benchmarks.e2e_decomp:app``.  The upstreams and production client/driver calls
are exactly the ones used by ``benchmarks.apps``; only their composition changes
so hardware counters can price the database round trip, HTTP round trip, and
overlap machinery independently.
"""

from __future__ import annotations

import asyncio

from wreath import Wreath

from .e2e_peer import ensure_e2e_peer as _e2e_ensure

app = Wreath()


@app.get("/db")
async def database_only(request):
    state = await _e2e_ensure()
    value = await state["connection"].fetchval("select $1::int4", 42)
    return {"db": value}


@app.get("/http")
async def http_only(request):
    state = await _e2e_ensure()
    response = await state["client"].get("/data")
    return {
        "upstream_status": response.status,
        "upstream_bytes": len(response.body),
    }


@app.get("/parallel")
async def parallel(request):
    state = await _e2e_ensure()
    fetch = asyncio.create_task(state["client"].get("/data"))
    try:
        value = await state["connection"].fetchval("select $1::int4", 42)
    except BaseException:
        fetch.cancel()
        raise
    response = await fetch
    return {
        "db": value,
        "upstream_status": response.status,
        "upstream_bytes": len(response.body),
    }
