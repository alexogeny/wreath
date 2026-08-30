"""Fast Sanic and BlackSheep peers for the holistic dashboard benchmark.

The two framework arms share this business kernel deliberately. Both use
msgspec for typed JSON decoding, cedarpy for authorization, asyncpg and aiohttp
for the two wire calls, NumPy for the dense calculations, Jinja for HTML, and
the same protobuf/MessagePack exporters as the FastAPI peer. Sanic owns its
native request/response path; BlackSheep owns its ASGI path and runs on Granian.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import hmac
import json
import os
from base64 import b64encode
from typing import Annotated, Any

import msgspec
import numpy as np
from cedarpy import Decision, is_authorized
from google.protobuf import message

from .holistic_fastapi import (
    _CEDAR_ENTITIES,
    _CEDAR_POLICY,
    _CEDAR_REQUEST,
    _DAYS,
    _TEMPLATE,
    _TEMPORAL_COUNTS,
    CompactExport,
    _Dependencies,
    _geospatial_summary,
    _OperationsExport,
    _project_series,
)

FRAMEWORK = os.environ.get("WREATH_HOLISTIC_FRAMEWORK", "sanic")
if FRAMEWORK not in {"sanic", "blacksheep"}:
    raise RuntimeError(
        f"WREATH_HOLISTIC_FRAMEWORK names {FRAMEWORK!r}; use 'sanic' or 'blacksheep'"
    )


class HolisticLine(msgspec.Struct, frozen=True):
    sku: Annotated[
        str,
        msgspec.Meta(min_length=3, max_length=24, pattern=r"^[a-z0-9-]+$"),
    ]
    quantity: Annotated[int, msgspec.Meta(ge=1, le=100)]
    price: Annotated[float, msgspec.Meta(gt=0, le=10_000)]


class HolisticPayload(msgspec.Struct, frozen=True):
    title: Annotated[str, msgspec.Meta(min_length=4, max_length=80)]
    lines: Annotated[list[HolisticLine], msgspec.Meta(min_length=1, max_length=8)]
    labels: dict[str, bool]


_STATE = _Dependencies()
_SESSION_KEY = b"holistic-e2e-session-key-material"
_BYTE_HEADERS = FRAMEWORK == "blacksheep"


def _required_header(headers: Any, name: str) -> str:
    key: str | bytes = name.encode() if _BYTE_HEADERS else name
    if _BYTE_HEADERS:
        values = headers.get(key)
        value = values[-1] if values else None
    else:
        value = headers.get(key)
    if value is None:
        raise ValueError(f"request header {name!r} is required")
    return value.decode("latin-1") if isinstance(value, bytes) else str(value)


def _integer(value: Any, name: str, minimum: int, maximum: int | None = None) -> int:
    if isinstance(value, list):
        value = value[-1] if value else None
    if value is None:
        raise ValueError(f"query parameter {name!r} must be an integer")
    try:
        parsed = int(value)
    except TypeError, ValueError:
        raise ValueError(f"query parameter {name!r} must be an integer") from None
    if parsed < minimum or (maximum is not None and parsed > maximum):
        upper = f" and <= {maximum}" if maximum is not None else ""
        raise ValueError(f"query parameter {name!r} must be >= {minimum}{upper}")
    return parsed


def _signed_session(item_id: int) -> str:
    payload = b64encode(json.dumps({"last_report": item_id}, separators=(",", ":")).encode())
    signature = hmac.digest(_SESSION_KEY, payload, hashlib.sha256)
    return f"{payload.decode()}.{b64encode(signature).decode()}"


async def _dashboard(
    body: bytes,
    headers: Any,
    cookies: Any,
    item_id: int,
    query: Any,
) -> tuple[bytes, list[tuple[str, str]]]:
    if _required_header(headers, "Host") not in {
        "operations.example.com",
        "127.0.0.1",
        "localhost",
        "testserver",
    }:
        raise ValueError("request Host is not trusted")
    if _required_header(headers, "Origin") != "https://example.com":
        raise ValueError("request Origin is not allowed")
    if _required_header(headers, "Authorization") != "Bearer holistic-user":
        raise ValueError("request bearer credential is invalid")
    if is_authorized(_CEDAR_REQUEST, _CEDAR_POLICY, _CEDAR_ENTITIES).decision != Decision.Allow:
        raise ValueError("request is forbidden by Cedar")

    payload = msgspec.json.decode(body, type=HolisticPayload)
    limit = _integer(query.get("limit"), "limit", 1, 8)
    page = _integer(query.get("page", 1), "page", 1)
    size = _integer(query.get("size", 12), "size", 1, 100)
    sort = query.get("sort", "-score")
    if isinstance(sort, list):
        sort = sort[-1] if sort else "-score"

    state = await _STATE.ensure()
    if state.http is None:
        raise RuntimeError("HTTP dependency was not initialized")
    fetch = asyncio.create_task(state.http.get("/data"))
    try:
        account_id = await state.database.fetchval("select $1::int4", 42)
    except BaseException:
        fetch.cancel()
        raise
    upstream = await fetch
    upstream_body = await upstream.read()
    upstream_status = upstream.status
    upstream.release()
    if not upstream_body:
        raise RuntimeError("upstream returned an empty body")

    selected_lines = payload.lines[:limit]
    _total = sum(line.quantity * line.price for line in selected_lines)
    series_count, paths, ticks, _tick_count = _project_series()
    hourly_count, weekly_count, monthly_count, next_run = _TEMPORAL_COUNTS
    occupied_cells, trail_speed = _geospatial_summary()

    vector_index = np.arange(128, dtype=np.float64)
    embedding = np.sin((vector_index + account_id) / 17.0) * np.cos((vector_index + item_id) / 29.0)
    scores = np.abs(embedding[:48])
    order = np.argsort(scores)
    if sort == "-score":
        order = order[::-1]
    offset = (page - 1) * size
    selected = order[offset : offset + size]
    incidents = [
        {
            "id": int(index) + 1,
            "score": float(scores[index]),
            "tenant": f"tenant-{int(index):02d}",
        }
        for index in selected
    ]
    export: message.Message = _OperationsExport(
        account_id=account_id,
        bucket_count=_DAYS,
        incident_ids=[incident["id"] for incident in incidents],
        similarity_scores=[incident["score"] for incident in incidents],
        generated_at=next_run,
    )
    protobuf_blob = export.SerializeToString()
    messagepack_blob = msgspec.msgpack.encode(
        CompactExport(
            incidents,
            occupied_cells,
            {
                "hour": hourly_count,
                "day": _DAYS,
                "week": weekly_count,
                "month": monthly_count,
            },
        )
    )
    document = _TEMPLATE.render(
        title=payload.title,
        trace=_required_header(headers, "X-Trace"),
        session=cookies.get("session", ""),
        principal="holistic-user",
        item_id=item_id,
        account_id=account_id,
        upstream_status=upstream_status,
        lines=selected_lines,
        series_count=series_count,
        spines=_DAYS + hourly_count + weekly_count + monthly_count,
        path_count=len(paths),
        speed=round(trail_speed, 3),
        occupied=len(occupied_cells),
        next_run=next_run,
        page_count=len(incidents),
        protobuf_bytes=len(protobuf_blob),
        msgpack_bytes=len(messagepack_blob),
        chart_path="".join(paths),
        ticks=ticks,
    )
    compressed = gzip.compress(document.encode(), compresslevel=9)
    return compressed, [
        ("access-control-allow-origin", "https://example.com"),
        ("cache-control", "private, no-store"),
        ("content-encoding", "gzip"),
        ("content-security-policy", "default-src 'self'; frame-ancestors 'none'"),
        ("permissions-policy", "geolocation=()"),
        ("server-timing", "app;dur=0"),
        ("set-cookie", f"wreath_state={_signed_session(item_id)}; Path=/; HttpOnly; SameSite=lax"),
        ("vary", "Accept-Encoding"),
        ("x-request-id", _required_header(headers, "X-Trace")),
    ]


if FRAMEWORK == "sanic":
    from sanic import Request as SanicRequest
    from sanic import Sanic
    from sanic.response import raw

    app = Sanic("wreath_holistic_sanic")

    @app.post("/v1/holistic/<item_id:int>")
    async def sanic_holistic(request: SanicRequest, item_id: int):
        body, headers = await _dashboard(
            request.body,
            request.headers,
            request.cookies,
            item_id,
            request.args,
        )
        return raw(
            body,
            headers=dict(headers),
            content_type="text/html; charset=utf-8",
        )

    @app.after_server_stop
    async def close_sanic(_app: Sanic, _loop: Any) -> None:
        await _STATE.close()

else:
    from blacksheep import Application, Content, Request, Response

    app = Application(show_error_details=os.environ.get("WREATH_BENCH_DEBUG") == "1")

    @app.router.post("/v1/holistic/{item_id}")
    async def blacksheep_holistic(request: Request, item_id: int) -> Response:
        content = request.content
        if content is None:
            raise ValueError("request body is required")
        body, headers = await _dashboard(
            await content.read(),
            request.headers,
            request.cookies,
            item_id,
            request.query,
        )
        return Response(
            200,
            [(name.encode(), value.encode()) for name, value in headers],
            Content(b"text/html; charset=utf-8", body),
        )

    @app.on_stop
    async def close_blacksheep(_app: Application) -> None:
        await _STATE.close()
