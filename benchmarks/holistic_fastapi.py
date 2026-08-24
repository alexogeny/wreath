"""FastAPI ecosystem peer for :mod:`benchmarks.holistic_e2e`.

This is intentionally a production-shaped application, not a framework-only
microbenchmark.  It performs the same successful operations-dashboard
transaction as Wreath's maximal declarative target: HTTP policy middleware,
nested typed input, bearer authentication, Cedar authorization, concurrent
PostgreSQL and HTTP wire calls, a 730 x 48 x 6 sparse-to-dense series projection,
downsampling and SVG paths, temporal/geospatial/vector calculations, ranked
pagination, protobuf and MessagePack exports, escaped template rendering,
session mutation and compressed HTML emission.

The implementation uses the dependencies a pragmatic FastAPI service would
reach for instead of reimplementing numerical and serialization kernels in
Python: NumPy, Jinja, protobuf and msgspec alongside the ordinary FastAPI,
Starlette, Pydantic, cedarpy, asyncpg and aiohttp stack.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import math
from contextlib import asynccontextmanager
from typing import Annotated, Any
from zoneinfo import ZoneInfo

import aiohttp
import asyncpg
import msgspec
import numpy as np
from cedarpy import Decision, is_authorized
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from jinja2 import Environment
from pydantic import BaseModel, Field
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import HTMLResponse

from .e2e_upstream import BenchPostgres, BenchUpstreamHttp

_protobuf_descriptor: Any = descriptor_pb2
_protobuf_pool: Any = descriptor_pool

REQUEST_METHOD = "POST"
REQUEST_PATH = "/v1/holistic/42?limit=3&page=1&size=12&sort=-score"
REQUEST_BODY = (
    b'{"title":"Quarterly <report>","lines":['
    b'{"sku":"alpha-1","quantity":2,"price":12.5},'
    b'{"sku":"beta-2","quantity":1,"price":7.25},'
    b'{"sku":"gamma-3","quantity":4,"price":3.5}'
    b'],"labels":{"active":true,"reviewed":false}}'
)
REQUEST_HEADERS = {
    "Authorization": "Bearer holistic-user",
    "Accept-Encoding": "gzip",
    "Content-Type": "application/json",
    "Host": "operations.example.com",
    "Origin": "https://example.com",
    "Sec-Fetch-Site": "same-origin",
    "X-Trace": "holistic-trace-42",
    "Cookie": "session=holistic-session",
    "User-Agent": "Mozilla/5.0 (Linux; Android 14) Chrome/126.0 Mobile Safari/537.36",
    "X-Forwarded-For": "4.1.1.1",
}

_CEDAR_POLICY = """
@id("authenticated-render")
permit(principal, action == Action::"render", resource)
  when { context.flags.contains("dense_dashboard") };
@id("post-only")
forbid(principal, action == Action::"render", resource)
  unless { context.method == "POST" };
"""
_CEDAR_ENTITIES = json.dumps([])
_CEDAR_REQUEST = {
    "principal": 'User::"holistic-user"',
    "action": 'Action::"render"',
    "resource": 'Document::"42"',
    "context": {"flags": ["dense_dashboard"], "method": "POST"},
}


class HolisticLine(BaseModel):
    sku: Annotated[str, Field(min_length=3, max_length=24, pattern=r"^[a-z0-9-]+$")]
    quantity: Annotated[int, Field(ge=1, le=100)]
    price: Annotated[float, Field(gt=0, le=10_000)]


class HolisticPayload(BaseModel):
    title: Annotated[str, Field(min_length=4, max_length=80)]
    lines: Annotated[list[HolisticLine], Field(min_length=1, max_length=8)]
    labels: dict[str, bool]


class CompactExport(msgspec.Struct, frozen=True):
    incidents: list[dict[str, Any]]
    occupied_cells: list[int]
    spines: dict[str, int]


class _Dependencies:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.database: Any = None
        self.http: aiohttp.ClientSession | None = None
        self.postgres_peer: BenchPostgres | None = None
        self.http_peer: BenchUpstreamHttp | None = None

    async def ensure(self) -> _Dependencies:
        if self.database is not None:
            return self
        async with self.lock:
            if self.database is not None:
                return self
            self.postgres_peer = BenchPostgres()
            dsn = await self.postgres_peer.start()

            async def no_reset(connection: Any) -> None:
                return None

            self.database = await asyncpg.create_pool(
                dsn,
                min_size=8,
                max_size=8,
                ssl=False,
                reset=no_reset,
            )
            self.http_peer = BenchUpstreamHttp()
            port = await self.http_peer.start()
            self.http = aiohttp.ClientSession(
                base_url=f"http://127.0.0.1:{port}",
                connector=aiohttp.TCPConnector(limit=8, limit_per_host=8),
                trust_env=False,
            )
        return self

    async def close(self) -> None:
        if self.http is not None:
            await self.http.close()
        if self.database is not None:
            await self.database.close()
        if self.http_peer is not None:
            await self.http_peer.close()
        if self.postgres_peer is not None:
            await self.postgres_peer.close()


_DEPENDENCIES = _Dependencies()


@asynccontextmanager
async def _lifespan(application: FastAPI):
    yield
    await _DEPENDENCIES.close()


app = FastAPI(lifespan=_lifespan)
app.add_middleware(GZipMiddleware, minimum_size=512)
app.add_middleware(
    SessionMiddleware,
    secret_key="holistic-e2e-session-key-material",
    session_cookie="wreath_state",
    https_only=False,
)
app.add_middleware(CORSMiddleware, allow_origins=["https://example.com"])
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["operations.example.com", "127.0.0.1", "localhost", "testserver"],
)


@app.middleware("http")
async def operational_headers(request: Request, call_next: Any) -> Any:
    response = await call_next(request)
    response.headers["cache-control"] = "private, no-store"
    response.headers["content-security-policy"] = "default-src 'self'; frame-ancestors 'none'"
    response.headers["permissions-policy"] = "geolocation=()"
    response.headers["x-request-id"] = request.headers.get("x-trace", "generated")
    response.headers["server-timing"] = "app;dur=0"
    return response


_BEARER = HTTPBearer(auto_error=False)


async def _authorized(
    credential: Annotated[HTTPAuthorizationCredentials | None, Security(_BEARER)],
) -> str:
    if (
        credential is None
        or credential.scheme.lower() != "bearer"
        or credential.credentials != "holistic-user"
    ):
        raise HTTPException(status_code=401, detail="invalid bearer token")
    decision = is_authorized(_CEDAR_REQUEST, _CEDAR_POLICY, _CEDAR_ENTITIES)
    if decision.decision != Decision.Allow:
        raise HTTPException(status_code=403, detail="forbidden")
    return credential.credentials


# The same immutable two-year, 48-tenant, six-measure fixture shape used by the
# Wreath target. Sparse coordinates stay compact globally; every request owns
# and fills its dense working array before calculating paths and axes.
_DAYS = 730
_TENANTS = 48
_MEASURES = 6
_ROWS = _TENANTS * _MEASURES
_day_grid = np.arange(_DAYS, dtype=np.int32)
_tenant_grid = np.arange(_TENANTS, dtype=np.int32)
_present = ((_day_grid[None, :] + _tenant_grid[:, None]) % 7) != 0
_SPARSE_ROWS, _SPARSE_DAYS = np.nonzero(np.repeat(_present, _MEASURES, axis=0))
_SPARSE_VALUES = (
    0.45
    + (_SPARSE_ROWS % _MEASURES) * 3.0
    + (_SPARSE_ROWS // _MEASURES) * 0.17
    + np.sin((_SPARSE_DAYS + _SPARSE_ROWS // _MEASURES) / 19.0)
).astype(np.float64)
_DOWNSAMPLE_ROWS = tuple(range(0, 24, 3))
_FULL_ROWS = (1, 3, 5)
_PROJECT_ROWS = _DOWNSAMPLE_ROWS + _FULL_ROWS
_ROW_FILLS = np.array([0.0, np.nan, 0.0, np.nan, 0.0, np.nan])
_PROJECT_SPARSE = tuple(
    (
        _SPARSE_DAYS[_SPARSE_ROWS == row],
        _SPARSE_VALUES[_SPARSE_ROWS == row],
    )
    for row in _PROJECT_ROWS
)
_DAY_INDICES = np.arange(_DAYS, dtype=np.int32)
_DOWNSAMPLE_WINDOWS = tuple(np.array_split(_DAY_INDICES, 128))
_TRAJECTORY_INDEX = np.arange(_DAYS, dtype=np.float64)
_TRAJECTORY_LAT = -27.7 + 0.3 * np.sin(_TRAJECTORY_INDEX / 31.0)
_TRAJECTORY_LON = 152.8 + 0.3 * np.cos(_TRAJECTORY_INDEX / 29.0)


def _project_series() -> tuple[int, list[str], str, int]:
    # Materialize only the eleven requested output rows. The former 288 x 730
    # array made every ecosystem arm reconcile 278 rows that never reached the
    # response, while Wreath's fused projector correctly stayed output-driven.
    # Row-local fills also remove the full-size isnan/tile/broadcast copies.
    dense = np.empty((len(_PROJECT_ROWS), _DAYS), dtype=np.float64)
    for output, (row, (days, values)) in enumerate(
        zip(_PROJECT_ROWS, _PROJECT_SPARSE, strict=True)
    ):
        dense[output].fill(_ROW_FILLS[row % _MEASURES])
        dense[output, days] = values

    # Eight representative screen series are reduced to 128 samples; three
    # diagnostic series retain their full 730 points. The deterministic
    # max-deviation selection is vectorized within each window, the shape a
    # NumPy-backed FastAPI service would naturally use.
    paths: list[str] = []
    for output in range(len(_DOWNSAMPLE_ROWS)):
        values = dense[output]
        chosen = np.fromiter(
            (
                window[np.nanargmax(np.abs(values[window] - np.nanmean(values[window])))]
                for window in _DOWNSAMPLE_WINDOWS
            ),
            dtype=np.int32,
            count=128,
        )
        paths.append(_svg_path(chosen, values[chosen]))
    for output in range(len(_DOWNSAMPLE_ROWS), len(_PROJECT_ROWS)):
        paths.append(_svg_path(_DAY_INDICES, dense[output]))
    tick_rows = dense[: len(_DOWNSAMPLE_ROWS)]
    tick_values = np.nanquantile(tick_rows, np.linspace(0.0, 1.0, 9), axis=1)
    tick_text = " ".join(f"{value:.9g}" for value in tick_values.ravel())
    return _ROWS, paths, tick_text, int(tick_values.size)


def _svg_path(indices: np.ndarray, values: np.ndarray) -> str:
    finite = np.isfinite(values)
    commands = (
        f"{'M' if position == 0 or not finite[position - 1] else 'L'}{int(index)},{value:.9g}"
        for position, (index, value) in enumerate(zip(indices[finite], values[finite], strict=True))
    )
    return "".join(commands)


def _temporal_counts() -> tuple[int, int, int, str]:
    timezone = ZoneInfo("Pacific/Auckland")
    start = datetime.datetime(2025, 12, 31, 11, tzinfo=datetime.UTC)
    end = datetime.datetime(2027, 12, 31, 11, tzinfo=datetime.UTC)
    local_start = start.astimezone(timezone)
    local_end = end.astimezone(timezone)
    weeks = math.ceil((local_end.date() - local_start.date()).days / 7)
    months = (local_end.year - local_start.year) * 12 + local_end.month - local_start.month
    hourly_start = datetime.datetime(2026, 3, 20, 11, tzinfo=datetime.UTC)
    hourly_end = datetime.datetime(2026, 5, 1, 12, tzinfo=datetime.UTC)
    hours = int((hourly_end - hourly_start).total_seconds() // 3600)
    next_run = datetime.datetime(2026, 3, 23, 3, 15, tzinfo=timezone).isoformat()
    return hours, weeks, months, next_run


def _geospatial_summary() -> tuple[list[int], float]:
    rows = np.floor((_TRAJECTORY_LAT + 28.0) / 0.18).astype(np.int32)
    columns = np.floor((_TRAJECTORY_LON - 152.4) / 0.2).astype(np.int32)
    occupied = np.unique(rows * 8 + columns)
    lat_delta = np.diff(np.radians(_TRAJECTORY_LAT))
    lon_delta = np.diff(np.radians(_TRAJECTORY_LON))
    mean_lat = np.radians((_TRAJECTORY_LAT[1:] + _TRAJECTORY_LAT[:-1]) / 2)
    metres = 6_371_008.8 * np.sqrt(lat_delta * lat_delta + (np.cos(mean_lat) * lon_delta) ** 2)
    return occupied.tolist(), float(np.mean(metres) / 86_400.0)


def _protobuf_type() -> type[Any]:
    file = _protobuf_descriptor.FileDescriptorProto(
        name="operations_export.proto",
        package="wreath.bench",
    )
    message = file.message_type.add(name="OperationsExport")
    fields = (
        ("account_id", 1, _protobuf_descriptor.FieldDescriptorProto.TYPE_INT64, False),
        ("bucket_count", 2, _protobuf_descriptor.FieldDescriptorProto.TYPE_INT32, False),
        ("incident_ids", 3, _protobuf_descriptor.FieldDescriptorProto.TYPE_INT64, True),
        ("similarity_scores", 4, _protobuf_descriptor.FieldDescriptorProto.TYPE_FLOAT, True),
        ("generated_at", 5, _protobuf_descriptor.FieldDescriptorProto.TYPE_STRING, False),
    )
    for name, number, kind, repeated in fields:
        field = message.field.add(name=name, number=number, type=kind)
        field.label = (
            _protobuf_descriptor.FieldDescriptorProto.LABEL_REPEATED
            if repeated
            else _protobuf_descriptor.FieldDescriptorProto.LABEL_OPTIONAL
        )
    descriptor = _protobuf_pool.DescriptorPool().Add(file).message_types_by_name[
        "OperationsExport"
    ]
    return message_factory.GetMessageClass(descriptor)


_OperationsExport = _protobuf_type()
_TEMPLATE = Environment(autoescape=True).from_string(
    "<!doctype html><title>{{ title }}</title>"
    '<main data-trace="{{ trace }}" data-session="{{ session }}" '
    'data-client-country="US" data-client-agent="Chrome" data-client-bot="false">'
    "<h1>{{ principal }} / {{ item_id }}</h1>"
    "<p>POST:/v1/holistic/42:{{ account_id }}:{{ upstream_status }}</p>"
    "<ul>{% for line in lines %}<li>{{ line.sku }} × {{ line.quantity }} @ "
    "{{ line.price }}</li>{% endfor %}</ul>"
    '<section data-buckets="730" data-lines="{{ series_count }}" data-spines="{{ spines }}" '
    'data-paths="{{ path_count }}" data-age="2 years ago" data-span="P2Y" '
    'data-distance="732" data-speed="{{ speed }}" data-grid="6x6:{{ occupied }}" '
    'data-next="{{ next_run }}" data-vector="128:128:30000/32" '
    'data-page="{{ page_count }}/48" data-protobuf="{{ protobuf_bytes }}" '
    'data-msgpack="{{ msgpack_bytes }}" data-metrics="5">'
    '<svg viewBox="0 0 365 120"><path d="{{ chart_path }}"></path></svg>'
    "<p>{{ ticks }}</p></section></main>"
)


@app.post("/v1/holistic/{item_id}", response_class=HTMLResponse)
async def holistic(
    request: Request,
    item_id: int,
    payload: HolisticPayload,
    principal: Annotated[str, Depends(_authorized)],
    limit: Annotated[int, Query(ge=1, le=8)],
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 12,
    sort: str = "-score",
) -> HTMLResponse:
    state = await _DEPENDENCIES.ensure()
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
    if len(upstream_body) == 0:
        raise RuntimeError("upstream returned an empty body")

    request.session["last_report"] = item_id
    selected_lines = payload.lines[:limit]
    _total = sum(line.quantity * line.price for line in selected_lines)
    series_count, paths, ticks, _tick_count = _project_series()
    hourly_count, weekly_count, monthly_count, next_run = _temporal_counts()
    occupied_cells, trail_speed = _geospatial_summary()

    vector_index = np.arange(128, dtype=np.float64)
    embedding = np.sin((vector_index + account_id) / 17.0) * np.cos(
        (vector_index + item_id) / 29.0
    )
    scores = np.abs(embedding[:48])
    order = np.argsort(scores)
    if sort == "-score":
        order = order[::-1]
    offset = (page - 1) * size
    selected = order[offset : offset + size]
    incidents = [
        {"id": int(index) + 1, "score": float(scores[index]), "tenant": f"tenant-{int(index):02d}"}
        for index in selected
    ]
    export = _OperationsExport(
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
            {"hour": hourly_count, "day": _DAYS, "week": weekly_count, "month": monthly_count},
        )
    )
    document = _TEMPLATE.render(
        title=payload.title,
        trace=request.headers["x-trace"],
        session=request.cookies["session"],
        principal=principal,
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
    return HTMLResponse(document)
