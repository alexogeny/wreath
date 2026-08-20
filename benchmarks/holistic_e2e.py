"""A maximal declarative application for whole-framework instruction accounting.

Unlike the ordinary ``e2e`` scenario, which isolates authentication plus one
PostgreSQL and one HTTP round trip, this route deliberately wears the broad
application surface a mature SaaS endpoint accumulates: first-class HTTP
policy, typed path/query/header/cookie/body binding, nested validation,
dependency injection, bearer authentication, Cedar authorization, concurrent
driver/client work, native ORM model storage, compiled templates, and HTML
response emission.

The subject is one operations-intelligence page rather than a bag of unrelated
calls: temporal spines and calculated series drive its charts, a trajectory and
grid locate field activity, pgvector/halfvec/sparsevec fields shape a
similar-incident panel, pagination bounds that panel, metrics summarize the
work, and protobuf plus MessagePack produce the two compact export forms the
same page can hand to an agent or mobile client.

The dashboard remains one independently measurable request. Sibling arms cover
the declarations whose honest shape is a different route or protocol --
GraphQL, generated CRUD, negotiated output, multipart, MCP, gRPC and SSE --
rather than hiding nested dispatch inside the dashboard handler. Every route is
mounted through one ``VersionedRouter`` under ``/v1``.

The two wire dependencies are the deterministic in-process peers used by the
ordinary e2e workload. No external service, filesystem access, random value,
or clock read enters a measured request.
"""

from __future__ import annotations

import asyncio
import datetime
import math
import os
from dataclasses import dataclass
from pathlib import Path as FilePath
from typing import Annotated, Any

from wreath import Wreath
from wreath._devtools.sample_app import CSRF_SECRET
from wreath._projector import ProjectorLoss, ProjectorSnapshot, RouteMetric
from wreath._prometheus import metrics_router
from wreath.admin import Admin
from wreath.auth import BearerTokenBackend, Identity
from wreath.authorization import (
    CedarAuthorizer,
    CedarPolicies,
    EntityUid,
    authorize,
    permissions,
    roles,
)
from wreath.background import BackgroundTask
from wreath.binding import Body, Cookie, Depends, Field, File, Form, Header, Path, Query
from wreath.cache_control import CacheControl
from wreath.client_facts import (
    ClientFacts,
)
from wreath.crud import Access, crud_router
from wreath.flags import FlagView, flags_dependency
from wreath.geospatial import BoundingBox, Coordinate, Trajectory, distance, grid
from wreath.graphql import GraphQL
from wreath.grpc import GrpcService, frame_message
from wreath.health import callable_check, health_router
from wreath.mcp import MCP, PROTOCOL_VERSION
from wreath.metrics import Counters, flatten
from wreath.negotiation import MSGPACK, serialize
from wreath.organizations import (
    InMemoryOrganizationStore,
    Memberships,
    Organization,
    scim_router,
)
from wreath.orm import FromORM, Mapped, Model, Session, column
from wreath.orm.types import (
    Bool,
    Float64,
    Halfvec,
    Int32,
    Int64,
    Sparsevec,
    Text,
    TimestampTz,
    Vector,
)
from wreath.pagination import Page, PageParams, _rank_indices, page_params
from wreath.policy import AIScrapingPolicy, HttpPolicy
from wreath.policy.cache import CachePolicy
from wreath.policy.compression import CompressionPolicy
from wreath.policy.cors import CorsPolicy
from wreath.policy.csrf import CsrfPolicy
from wreath.policy.proxy import ProxyPolicy
from wreath.policy.ratelimit import RateLimitPolicy, TieredRateLimitPolicy
from wreath.policy.request_id import RequestIdPolicy
from wreath.policy.security import SecurityHeadersPolicy, TrustedHostPolicy
from wreath.policy.sessions import SessionPolicy
from wreath.policy.timing import ServerTimingPolicy
from wreath.postgres import SparseVector
from wreath.protobuf import encode as protobuf_encode
from wreath.protobuf import field as protobuf_field
from wreath.protobuf import message
from wreath.queries import Param, Queries, query
from wreath.quota import Quotas
from wreath.request import UploadedFile
from wreath.response import HTMLResponse, SSEResponse
from wreath.rooms import RoomRegistry
from wreath.router import Router
from wreath.series import ChartData, Range, Series, avg, count, sum_
from wreath.sync import Sync
from wreath.templates import Template
from wreath.temporal import (
    Day,
    Hour,
    Instant,
    Month,
    Recurrence,
    Week,
    format_duration,
    format_iso,
    relative,
    spine_length,
    spine_lengths,
    zone,
)
from wreath.temporal import (
    spine as _fixture_spine,
)
from wreath.tenancy import (
    InMemoryTenantDirectory,
    Tenancy,
    TenancyMiddleware,
    Tenant,
    TenantHeader,
    current_tenant,
)
from wreath.users import InMemoryUserStore
from wreath.versioning import VersionedRouter
from wreath.webhooks import (
    HMACWebhookSigner,
    HMACWebhookVerifier,
    LocalReplayStore,
    WebhookContext,
    WebhookEnvelope,
)
from wreath.workflows import InMemoryWorkflowStore, Workflow

from .apps import _e2e_ensure

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
    "Accept-Encoding": "zstd",
    "Content-Type": "application/json",
    "Host": "operations.example.com",
    "Origin": "https://example.com",
    "Sec-Fetch-Site": "same-origin",
    "X-Trace": "holistic-trace-42",
    "Cookie": "session=holistic-session",
    "User-Agent": "Mozilla/5.0 (Linux; Android 14) Chrome/126.0 Mobile Safari/537.36",
    "X-Forwarded-For": "4.1.1.1",
}


@dataclass(frozen=True, slots=True)
class Arm:
    """One independently measurable request into the holistic application."""

    method: str
    path: str
    headers: dict[str, str]
    body: bytes = b""
    http_version: str = "1.1"
    setup: str | None = None
    driver: str = "http"


@dataclass(frozen=True, slots=True)
class HolisticLine:
    sku: Annotated[str, Field(min_length=3, max_length=24, pattern=r"^[a-z0-9-]+$")]
    quantity: Annotated[int, Field(ge=1, le=100)]
    price: Annotated[float, Field(gt=0, le=10_000)]


@dataclass(frozen=True, slots=True)
class HolisticPayload:
    title: Annotated[str, Field(min_length=4, max_length=80)]
    lines: Annotated[list[HolisticLine], Field(min_length=1, max_length=8)]
    labels: dict[str, bool]


@dataclass(frozen=True, slots=True)
class ImportManifest:
    """The form half of the multipart report-import arm."""

    source: Annotated[str, Field(min_length=3, max_length=24)]
    observations: Annotated[int, Field(ge=1, le=10_000)]
    dry_run: bool = False


class OperationsAccount(Model, table="operations_accounts"):
    """The small native-driver row shared by REST, CRUD and GraphQL arms."""

    id: Mapped[int] = column(Int32, primary_key=True)


class AccountQueries(Queries[OperationsAccount]):
    """Named reads used by more than one protocol arm."""

    by_id = query(OperationsAccount.id == Param("account_id")).one()


class HolisticView(Model, table="holistic_views"):
    id: Mapped[int] = column(Int64, primary_key=True)
    account_id: Mapped[int] = column(Int64)
    sequence: Mapped[int] = column(Int32)
    title: Mapped[str] = column(Text)
    trace: Mapped[str] = column(Text)
    session: Mapped[str] = column(Text)
    active: Mapped[bool] = column(Bool)
    total: Mapped[float] = column(Float64)


class IncidentProjection(Model, table="incident_projections"):
    """The retrieval record behind the dashboard's similar-incident panel."""

    id: Mapped[int] = column(Int64, primary_key=True)
    embedding: Mapped[list[float]] = column(
        Vector(128), index="hnsw", index_ops="vector_cosine_ops"
    )
    compact_embedding: Mapped[list[float]] = column(
        Halfvec(128), index="hnsw", index_ops="halfvec_cosine_ops"
    )
    terms: Mapped[SparseVector] = column(
        Sparsevec(30_000), index="hnsw", index_ops="sparsevec_l2_ops"
    )


class ActivityReading(Model, table="activity_readings"):
    """Source rows for the declared calculated-series arm."""

    id: Mapped[int] = column(Int64, primary_key=True)
    account_id: Mapped[int] = column(Int64)
    tenant: Mapped[str] = column(Text)
    happened_at: Mapped[datetime.datetime] = column(TimestampTz)
    requests: Mapped[float] = column(Float64)
    latency: Mapped[float] = column(Float64)


@dataclass(frozen=True, slots=True)
class FieldObservation:
    account_id: Annotated[int, Field(ge=1)]
    station: Annotated[str, Field(min_length=3, max_length=32)]
    severity: Annotated[int, Field(ge=1, le=5)]


@dataclass(frozen=True, slots=True)
class OperationsSummary:
    account_id: int
    bucket_count: int
    active_incidents: int
    region: str


@message
class OperationsExport:
    account_id: int = protobuf_field(1)
    bucket_count: int = protobuf_field(2)
    incident_ids: list[int] = protobuf_field(3)
    similarity_scores: list[float] = protobuf_field(4, kind="float")
    generated_at: str = protobuf_field(5)


@message
class OperationsQuery:
    account_id: int = protobuf_field(1)
    include_vectors: bool = protobuf_field(2)


_PAGE = Template.from_string(
    "<!doctype html><title>{{ view.title }}</title>"
    '<main data-trace="{{ view.trace }}" data-session="{{ view.session }}" '
    'data-client-country="{{ client_country }}" '
    'data-client-agent="{{ client_agent }}" data-client-bot="{{ client_bot }}">'
    "<h1>{{ principal }} / {{ view.id }}</h1>"
    "<p>{{ dependency }}:{{ view.account_id }}:{{ upstream_status }}</p>"
    "<ul>{% for line in lines %}"
    "<li>{{ line.sku }} × {{ line.quantity }} @ {{ line.price }}</li>"
    "{% endfor %}</ul>"
    '<section data-buckets="{{ chart_buckets }}" data-lines="{{ chart_lines }}" '
    'data-spines="{{ chart_spines }}" data-paths="{{ chart_paths }}" '
    'data-age="{{ chart_age }}" data-span="{{ chart_span }}" '
    'data-distance="{{ chart_distance }}" data-speed="{{ chart_speed }}" '
    'data-grid="{{ chart_grid }}" data-next="{{ chart_next }}" '
    'data-vector="{{ vector_shape }}" data-page="{{ incident_page }}" '
    'data-protobuf="{{ protobuf_bytes }}" data-msgpack="{{ msgpack_bytes }}" '
    'data-metrics="{{ metric_count }}">'
    '<svg viewBox="0 0 365 120"><path d="{{ chart_path }}"></path></svg>'
    "<p>{{ chart_ticks }}</p>"
    "</section>"
    "</main>",
    name="holistic-e2e.html",
)

# A full in-memory calculated-view result. The sparse readings are immutable
# benchmark fixture data; every request still pays for the DST-correct dense
# range, 730 × 48 × 6 reconciliation, downsampling, tick selection, gap-aware
# path emission, and template egress. The range stays native-owned until final
# paths and axes materialise. This is deliberately much larger than a screen
# needs: the target exists to make ownership drift visible in retired
# instructions, not to model a minimalist dashboard.
_SERIES_ZONE = zone("Pacific/Auckland")
_SERIES_START = Instant.of(datetime.datetime(2025, 12, 31, 11, tzinfo=datetime.UTC))
_SERIES_END = Instant.of(datetime.datetime(2027, 12, 31, 11, tzinfo=datetime.UTC))
_SERIES_BUCKETS = _fixture_spine(_SERIES_START, _SERIES_END, bucket=Day, in_zone=_SERIES_ZONE)
_SERIES_FILLS = {
    "requests": 0.0,
    "latency": None,
    "revenue": 0.0,
    "saturation": None,
    "errors": 0.0,
    "queue": None,
}
_SERIES_SPARSE = {
    (f"tenant-{tenant:02d}", False): {
        bucket: {
            "requests": float(800 + tenant * 17 + index % 41),
            "latency": None
            if (index + tenant) % 29 == 0
            else 18.0 + 7.0 * math.sin((index + tenant) / 19.0),
            "revenue": float((index % 13 + 1) * (tenant + 3)),
            "saturation": None
            if index % 31 == 0
            else 0.45 + 0.4 * math.sin((index + tenant) / 37.0),
            "errors": float((index * (tenant + 1)) % 17),
            "queue": None
            if (index + tenant) % 43 == 0
            else 6.0 + 5.0 * math.cos((index + tenant) / 23.0),
        }
        for index, bucket in enumerate(_SERIES_BUCKETS)
        if (index + tenant) % 7 != 0
    }
    for tenant in range(48)
}
_SERIES_DATA = ChartData(_SERIES_BUCKETS, _SERIES_SPARSE, _SERIES_FILLS)
_DEPOT = Coordinate(lat=-27.4698, lon=153.0251)
_SITE = Coordinate(lat=-33.8688, lon=151.2093)
_HOURLY_START = Instant.of(datetime.datetime(2026, 3, 20, 11, tzinfo=datetime.UTC))
_HOURLY_END = Instant.of(datetime.datetime(2026, 5, 1, 12, tzinfo=datetime.UTC))
_SCHEDULE = Recurrence.calendar(
    "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=3;BYMINUTE=15",
    tz=_SERIES_ZONE,
)
_MAP_GRID = grid(BoundingBox(-28.0, -27.0, 152.4, 153.4), metres=20_000)
_TRAJECTORY = Trajectory(
    (
        bucket,
        Coordinate(
            lat=-27.7 + 0.3 * math.sin(index / 31.0),
            lon=152.8 + 0.3 * math.cos(index / 29.0),
        ),
    )
    for index, bucket in enumerate(_SERIES_BUCKETS)
)

_ACTIVITY_START = datetime.datetime(2026, 2, 1, tzinfo=datetime.UTC)
_ACTIVITY_END = datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC)
_ACTIVITY_RANGE = Range(_ACTIVITY_START, _ACTIVITY_END)
_ACTIVITY_VIEW = (
    Series(
        ActivityReading,
        at=ActivityReading.happened_at,
        bucket=Day,
        stored_in=_SERIES_ZONE,
    )
    .where(ActivityReading.account_id == Param("account_id"))
    .measure(
        samples=count(),
        requests=sum_(ActivityReading.requests, unit="requests"),
        latency=avg(ActivityReading.latency, unit="ms"),
    )
    .by(ActivityReading.tenant, top=3)
)
_ACTIVITY_ROWS = tuple(
    (
        _ACTIVITY_START + datetime.timedelta(days=day),
        f"tenant-{tenant:02d}",
        False,
        1,
        float(800 + tenant * 37 + day * 11),
        14.0 + tenant * 2.5 + math.sin(day / 3.0),
    )
    for day in range((_ACTIVITY_END - _ACTIVITY_START).days)
    for tenant in range(3)
)

_TENANT_DIRECTORY = InMemoryTenantDirectory((Tenant("acme", "tenant_acme", "tenant_acme_role"),))
_TENANCY = Tenancy(directory=_TENANT_DIRECTORY, source=TenantHeader("x-tenant"))
_ORGANIZATIONS = InMemoryOrganizationStore(
    roles={"admin", "member", "billing"},
    organizations=(Organization("acme", "Acme Field Operations"),),
)
_USERS = InMemoryUserStore()
_MEMBERSHIPS = Memberships(_ORGANIZATIONS)

_REPORT_WORKFLOW = Workflow("operations-report")


@_REPORT_WORKFLOW.step
def validate_report(context: Any) -> dict[str, Any]:
    return {"account_id": 42, "validated": True}


@_REPORT_WORKFLOW.step
async def reserve_projection(context: Any) -> dict[str, Any]:
    return {"slots": 12, "region": "au-east"}


@_REPORT_WORKFLOW.step
def publish_report(context: Any) -> dict[str, Any]:
    return {"published": True, "format": "operations-summary"}


class _MetricSource:
    """Stable projector snapshot for the operational metrics arm."""

    __slots__ = ("_snapshot",)

    def __init__(self) -> None:
        buckets = [0] * 64
        buckets[8] = 72_000
        buckets[11] = 8_000
        self._snapshot = ProjectorSnapshot(
            assembled=80_000,
            recent=(),
            failures=(),
            routes=(
                RouteMetric(
                    route_id=42,
                    count=80_000,
                    errors=17,
                    duration_us_sum=48_000_000,
                    duration_us_max=18_500,
                    buckets=buckets,
                ),
            ),
            loss=ProjectorLoss(),
            pending=3,
        )

    def snapshot(self) -> ProjectorSnapshot:
        return self._snapshot


_METRIC_SOURCE = _MetricSource()
_ROOMS = RoomRegistry()

_CEDAR = CedarPolicies(
    """
    @id("authenticated-render")
    permit(principal, action == Action::"render", resource)
      when { context.flags.contains("dense_dashboard") };

    @id("graphql-read")
    permit(principal, action == Action::"read", resource);

    @id("grpc-compile")
    permit(principal, action == Action::"Operations::compile", resource);

    @id("mcp-inspect")
    permit(principal, action == Action::"Operations::inspect", resource);

    @id("tenant-operations")
    permit(principal, action == Action::"tenant_read", resource)
      when { context.organizations.contains("acme") &&
             context.org_roles.contains("acme:admin") };

    @id("directory-read")
    permit(principal == User::"operations-directory",
           action == Action::"scim_read",
           resource == Organization::"acme");

    @id("directory-write")
    permit(principal == User::"operations-directory",
           action == Action::"scim_write",
           resource == Organization::"acme");

    @id("post-only")
    forbid(principal, action == Action::"render", resource)
      unless { context.method == "POST" };
    """
)


async def _verify(token: str) -> Identity | None:
    if token == "operations-directory":
        return Identity(
            "operations-directory",
            permissions=frozenset({"operations:access"}),
        )
    if token != "holistic-user":
        return None
    return Identity(
        "holistic-user",
        roles=frozenset({"staff", "billing"}),
        permissions=frozenset(
            {
                "crud:read",
                "documents:render",
                "events:read",
                "exports:read",
                "graphql:read",
                "grpc:invoke",
                "imports:write",
                "mcp:use",
                "operations:access",
                "admin:read",
                "sync:read",
            }
        ),
    )


def _request_dependency(request: Any) -> str:
    return f"{request.method}:{request.path}"


async def _record_report_projection(export: OperationsExport) -> None:
    """Serialize the durable audit payload after the response is emitted."""

    protobuf_encode(export)


class _HolisticDatabase:
    """Lazy database adapter over the benchmark's native PostgreSQL peer."""

    name = "holistic"

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def pool(self, workload: str) -> _HolisticDatabase:
        return self

    async def acquire(self, workload: str = "read") -> Any:
        return (await _e2e_ensure())["connection"]

    async def release(self, workload: str, connection: Any) -> None:
        return None


_QUOTAS = Quotas()
_REQUEST_QUOTA = _QUOTAS.declare("operations_requests", limit=1_000_000_000, period="P30D")


def _csrf_exempt(request: Any) -> bool:
    return request.method == "POST" and request.path == "/v1/webhooks/field-observations"


_COMPRESSION = CompressionPolicy(
    minimum_size=512,
    compress_authenticated=True,
)
if os.environ.get("WREATH_BENCH_OPTIMAL_COMPRESSION") == "1":
    # A retained response for neighbouring resource 41 is the client-held raw
    # dictionary for resource 42.  It differs at two dynamic byte positions;
    # bytes 249 onward are an independently verified stable template span.
    _DICTIONARY = (
        FilePath(__file__).with_name("data").joinpath("holistic-dictionary-v1.html")
    ).read_bytes().removesuffix(b"\n")
    _COMPRESSION._configure_dcz_dictionary("html", _DICTIONARY)
    _COMPRESSION._configure_gzip_fragment(
        "html",
        _DICTIONARY,
        prefix_bytes=249,
        suffix_bytes=0,
    )


_HTTP_POLICY = HttpPolicy(
    proxy=ProxyPolicy(trusted=["127.0.0.1"]),
    trusted_host=TrustedHostPolicy(
        ["operations.example.com", "127.0.0.1", "localhost", "testserver"]
    ),
    ai_scraping=AIScrapingPolicy(allow=("oai-searchbot",)),
    rate_limit=RateLimitPolicy(limit=1_000_000_000),
    principal_rate_limit=TieredRateLimitPolicy(
        tiers={"billing": (1_000_000_000, 60.0)},
        default=(1_000_000_000, 60.0),
        quota=_REQUEST_QUOTA,
    ),
    request_id=RequestIdPolicy(),
    server_timing=ServerTimingPolicy(),
    cors=CorsPolicy(allow_origins=["https://example.com"]),
    csrf=CsrfPolicy(CSRF_SECRET, secure=False, exempt=_csrf_exempt),
    security_headers=SecurityHeadersPolicy(
        content_security_policy="default-src 'self'; frame-ancestors 'none'",
        permissions_policy="geolocation=()",
    ),
    session=SessionPolicy(
        CSRF_SECRET,
        cookie="wreath_state",
        secure=False,
    ),
    cache_control=CachePolicy(CacheControl(private=True, no_store=True)),
    compression=_COMPRESSION,
)
app = Wreath(http_policy=_HTTP_POLICY)
_CLIENT_FACTS = app.client_facts("holistic")
_CLIENT_FACTS_DEPENDENCY = _CLIENT_FACTS
app.add_global_middleware(TenancyMiddleware(_TENANCY, optional=True))
_FLAGS = app.flags(dense_dashboard=True, compact_exports="50%")
_FLAGS_DEPENDENCY = flags_dependency(_FLAGS)
_AUTHORIZER = CedarAuthorizer(
    engine=_CEDAR,
    flags=_FLAGS,
    quota=_QUOTAS,
    organizations=_MEMBERSHIPS,
)
app.configure_auth(BearerTokenBackend(_verify), _AUTHORIZER)

# The benchmark peer is created lazily by `_e2e_ensure`, after lifespan has
# started. This adapter is the same application-owned database seam `app.orm`
# normally receives from `app.postgres`, while retaining the deterministic peer.
_DATABASE = _HolisticDatabase()
app._databases[_DATABASE.name] = _DATABASE
_REGISTRY = app.orm(
    database=_DATABASE.name,
    models=[OperationsAccount, ActivityReading],
    validate_schema="off",
)


@app.on_startup
async def _seed_enterprise_fixtures(application: Wreath) -> None:
    user = await _USERS.get_by_email("holistic-user@example.com")
    if user is None:
        user = await _USERS.create("holistic-user@example.com", "benchmark-disabled")
    await _ORGANIZATIONS.add_member("acme", user.id, roles=("member",))
    await _ORGANIZATIONS.add_member(
        "acme",
        "holistic-user",
        roles=("admin", "billing"),
    )


class _SeriesSession:
    """Declared SQL plus deterministic rows from the benchmark PostgreSQL peer."""

    registry = _REGISTRY

    async def declared(self, sql: str, values: tuple[Any, ...]) -> list[Any]:
        state = await _e2e_ensure()
        await state["connection"].fetch("select $1::int4", 42)
        return list(_ACTIVITY_ROWS)


class _AdminSession:
    """One generated-admin read backed by the same measured wire round trip."""

    async def get(self, model: type, primary_key: Any) -> HolisticView | None:
        state = await _e2e_ensure()
        rows = await state["connection"].fetch("select $1::int4", int(primary_key))
        if not rows:
            return None
        return HolisticView(
            id=int(primary_key),
            account_id=42,
            sequence=730,
            title="Acme operational intelligence",
            trace="admin-holistic-42",
            session="operator-session",
            active=True,
            total=49.75,
        )

    async def close(self) -> None:
        return None


class _SyncSession:
    """A bounded query answer for the declarative sync snapshot."""

    async def fetch(self, query: Any) -> list[OperationsAccount]:
        state = await _e2e_ensure()
        await state["connection"].fetch("select $1::int4", 42)
        return [OperationsAccount(id=index) for index in range(31, 43)]


_ACCOUNT_SYNC = Sync(OperationsAccount, max_rows=64)


@_ACCOUNT_SYNC.shape("mine")
def _my_accounts(principal: Any) -> Any:
    return OperationsAccount.select().limit(12)


def _open_admin_session(request: Any) -> _AdminSession:
    return _AdminSession()


_ADMIN = Admin(
    _open_admin_session,
    authorize=Access.permissions("admin:read"),
    title="Operations administration",
)
_ADMIN.register(
    HolisticView,
    slug="operations-view",
    label="Operations view",
    list_columns=("id", "account_id", "title", "active", "total"),
    operations=("list", "retrieve"),
)

_API = VersionedRouter()
_V1 = _API.version("1")


@_V1.post("/holistic/{item_id}")
@permissions("documents:render")
@roles("staff", "billing")
@authorize(action="render", resource='Document::"42"')
async def holistic(
    request: Any,
    item_id: Annotated[int, Path()],
    limit: Annotated[int, Query(minimum=1, maximum=8)],
    x_trace: Annotated[str, Header(alias="x-trace")],
    session_id: Annotated[str, Cookie(alias="session")],
    payload: Annotated[HolisticPayload, Body()],
    orm_session: Annotated[Session, FromORM("holistic", workload="read")],
    dependency: str = Depends(_request_dependency),
    pagination: PageParams = Depends(page_params),
    feature_flags: FlagView = Depends(_FLAGS_DEPENDENCY),
    client_facts: ClientFacts = Depends(_CLIENT_FACTS_DEPENDENCY),
) -> HTMLResponse:
    state = await _e2e_ensure()
    fetch = asyncio.create_task(state["client"].get("/data"))
    try:
        account = await AccountQueries(orm_session).by_id(account_id=42)
    except BaseException:
        fetch.cancel()
        raise
    upstream = await fetch
    account_id = account.id
    request.state.session["last_report"] = item_id

    selected_lines = payload.lines[:limit]
    total = sum(line.quantity * line.price for line in selected_lines)
    view = HolisticView(
        id=item_id,
        account_id=account_id,
        sequence=len(selected_lines),
        title=payload.title,
        trace=x_trace,
        session=session_id,
        active=(payload.labels.get("active", False) and feature_flags.enabled("dense_dashboard")),
        total=total,
    )
    daily_count = len(_SERIES_BUCKETS)
    hourly_count = spine_length(_HOURLY_START, _HOURLY_END, bucket=Hour, in_zone=_SERIES_ZONE)
    weekly_count, monthly_count = spine_lengths(
        _SERIES_START,
        _SERIES_END,
        buckets=(Week, Month),
        in_zone=_SERIES_ZONE,
    )
    series_count, series_keys, paths, tick_text, tick_count = _SERIES_DATA.project_chart_text(
        downsample_rows=range(0, 24, 3),
        full_rows=(1, 3, 5),
        threshold=128,
        tick_target=9,
    )
    next_run = _SCHEDULE.next_after(_HOURLY_START)
    occupied_cells, trail_speed = _TRAJECTORY.grid_summary(_SERIES_START, _SERIES_END, _MAP_GRID)
    embedding = [
        math.sin((index + account_id) / 17.0) * math.cos((index + item_id) / 29.0)
        for index in range(128)
    ]
    terms = SparseVector(
        30_000,
        {
            1 + ((index * 1229 + item_id) % 30_000): abs(value) + 0.01
            for index, value in enumerate(embedding[::4])
        },
    )
    projection = IncidentProjection(
        id=item_id,
        embedding=embedding,
        compact_embedding=embedding,
        terms=terms,
    )
    reverse = pagination.sort == ("-score",)
    candidate_count = 48
    scores = tuple(abs(embedding[index]) for index in range(candidate_count))
    selected = _rank_indices(scores, page=pagination.page, size=pagination.size, descending=reverse)
    incident_page = Page(
        tuple(
            {"id": index + 1, "score": scores[index], "tenant": series_keys[index][0]}
            for index in selected
        ),
        total=candidate_count,
        page=pagination.page,
        size=pagination.size,
    )
    exported_at = format_iso(next_run)
    export = OperationsExport(
        account_id=account_id,
        bucket_count=daily_count,
        incident_ids=[item["id"] for item in incident_page.items],
        similarity_scores=[item["score"] for item in incident_page.items],
        generated_at=exported_at,
    )
    protobuf_blob = protobuf_encode(export)
    messagepack_blob = MSGPACK.encode(
        {
            "incidents": incident_page.as_dict(),
            "occupied_cells": sorted(occupied_cells),
            "spines": {
                "hour": hourly_count,
                "day": daily_count,
                "week": weekly_count,
                "month": monthly_count,
            },
        }
    )
    counters = flatten(
        (
            Counters(
                "series",
                "operations",
                {
                    "dense_cells": daily_count * series_count,
                    "paths": len(paths),
                    "ticks": tick_count,
                },
            ),
            Counters(
                "retrieval",
                "incidents",
                {"candidates": candidate_count, "sparse_terms": len(projection.terms)},
            ),
        ),
        namespace="holistic",
    )
    document = _PAGE.render_bytes(
        {
            "view": view,
            "principal": request.identity.id,
            "dependency": dependency,
            "upstream_status": upstream.status,
            "lines": selected_lines,
            "chart_buckets": daily_count,
            "chart_lines": series_count,
            "chart_spines": daily_count + hourly_count + weekly_count + monthly_count,
            "chart_paths": len(paths),
            "chart_age": relative(_SERIES_START, now=_SERIES_END),
            "chart_span": format_duration(_SERIES_END - _SERIES_START),
            "chart_distance": round(distance(_DEPOT, _SITE) / 1000),
            "chart_speed": round(trail_speed or 0.0, 3),
            "chart_grid": f"{_MAP_GRID.rows}x{_MAP_GRID.columns}:{len(occupied_cells)}",
            "chart_next": exported_at,
            "vector_shape": (
                f"{len(projection.embedding)}:{len(projection.compact_embedding)}:"
                f"{projection.terms.dim}/{len(projection.terms)}"
            ),
            "incident_page": f"{len(incident_page.items)}/{incident_page.total}",
            "protobuf_bytes": len(protobuf_blob),
            "msgpack_bytes": len(messagepack_blob),
            "metric_count": len(counters),
            "client_country": (
                "unknown"
                if client_facts.ip is None or client_facts.ip.geo is None
                else client_facts.ip.geo.country or "unknown"
            ),
            "client_agent": client_facts.user_agent.browser or "unknown",
            "client_bot": str(client_facts.user_agent.bot).lower(),
            "chart_path": "".join(paths),
            "chart_ticks": tick_text,
        }
    )
    return HTMLResponse(
        document,
        background=BackgroundTask(_record_report_projection, export),
    )


# -- calculated series ------------------------------------------------------


@_V1.get("/accounts/{account_id}/activity-series")
@permissions("operations:access")
async def account_activity_series(
    request: Any,
    account_id: Annotated[int, Path()],
) -> dict[str, Any]:
    result = await _ACTIVITY_VIEW.run(
        _SeriesSession(),
        account_id=account_id,
        range=_ACTIVITY_RANGE,
        zone=_SERIES_ZONE,
    )
    return result.as_dict()


# -- tenancy and organizations ---------------------------------------------


@_V1.get("/enterprise/accounts/{account_id}")
@authorize(action="tenant_read", resource='Organization::"acme"')
async def enterprise_account(
    request: Any,
    account_id: Annotated[int, Path()],
) -> dict[str, Any]:
    tenant = current_tenant()
    memberships = _MEMBERSHIPS.for_request(request)
    return {
        "account_id": account_id,
        "tenant": tenant.key,
        "schema": tenant.schema,
        "organizations": [membership.organization for membership in memberships],
        "roles": sorted(
            role for membership in memberships for role in membership.qualified_roles()
        ),
    }


# -- generated administration ---------------------------------------------


_V1.include_router(_ADMIN.router("/admin"))


# -- organization provisioning ---------------------------------------------


_V1.include_router(
    scim_router(
        app,
        users=_USERS,
        organizations=_ORGANIZATIONS,
        organization="acme",
        prefix="/scim/v2",
        page_size=20,
        max_page_size=100,
    )
)


# -- bounded sync snapshot --------------------------------------------------


@_V1.get("/sync/accounts/mine")
@permissions("sync:read")
async def sync_accounts(request: Any) -> Any:
    snapshot = await _ACCOUNT_SYNC.evaluate(_SyncSession(), "mine", request.identity)
    return snapshot


# -- live room --------------------------------------------------------------


@_V1.websocket("/live/accounts/{account_id}", permissions=("events:read",))
async def live_account_room(websocket: Any) -> None:
    room = f"account:{websocket.path_params['account_id']}"
    await websocket.accept()
    await _ROOMS.join(room, websocket)
    try:
        async for payload in websocket:
            await _ROOMS.broadcast(room, payload)
    finally:
        await _ROOMS.leave(room, websocket)


# -- workflow ---------------------------------------------------------------


@_V1.post("/workflows/reports")
@permissions("documents:render")
async def run_report_workflow(request: Any) -> dict[str, Any]:
    outcome = await _REPORT_WORKFLOW.run(
        store=InMemoryWorkflowStore(),
        key="operations-report:42",
    )
    return {
        "key": outcome.key,
        "state": outcome.state,
        "completed": outcome.completed,
        "results": outcome.results,
    }


# -- signed webhook ---------------------------------------------------------


_WEBHOOK_KEYS = {"operations": b"holistic-webhook-secret-material-2026"}
_WEBHOOK_HUB = app.webhooks("holistic-operations")
_WEBHOOK_SOURCE = _WEBHOOK_HUB.source(
    "field-units",
    path="/v1/webhooks/field-observations",
    verifier=HMACWebhookVerifier(_WEBHOOK_KEYS, max_age=10 * 365 * 24 * 60 * 60),
    replay=LocalReplayStore(max_entries=100_000, ttl=10 * 60),
)


@_WEBHOOK_SOURCE.event("field.observation", payload=FieldObservation)
async def receive_field_observation(
    context: WebhookContext,
    observation: FieldObservation,
) -> None:
    protobuf_encode(
        OperationsExport(
            account_id=observation.account_id,
            bucket_count=observation.severity,
            incident_ids=[],
            similarity_scores=[],
            generated_at=context.envelope.timestamp.isoformat(),
        )
    )


# -- operational endpoints --------------------------------------------------


async def _database_ready() -> dict[str, Any]:
    state = await _e2e_ensure()
    rows = await state["connection"].fetch("select $1::int4", 42)
    return {"value": rows[0][0]}


async def _upstream_ready() -> dict[str, Any]:
    state = await _e2e_ensure()
    response = await state["client"].get("/data")
    return {"upstream_status": response.status}


_V1.include_router(
    health_router(
        (
            callable_check("postgres", _database_ready),
            callable_check("operations-service", _upstream_ready, critical=False),
        )
    )
)
_V1.include_router(
    metrics_router(
        _METRIC_SOURCE,
        namespace="wreath_holistic",
        route_labels={42: {"method": "POST", "path": "/v1/holistic/{item_id}"}},
        app=app,
    )
)


# -- GraphQL ---------------------------------------------------------------

_GRAPHQL = GraphQL(
    _REGISTRY,
    models=[OperationsAccount],
    dataclasses=[OperationsSummary],
    authorizer=_AUTHORIZER,
)


@_GRAPHQL.query(
    "operationsSummary",
    returns="OperationsSummary",
    policy="Query.operationsSummary",
    cost=25,
)
async def graphql_operations_summary(info: Any) -> OperationsSummary:
    account = await AccountQueries(info.session).by_id(account_id=42)
    return OperationsSummary(
        account_id=account.id,
        bucket_count=len(_SERIES_BUCKETS),
        active_incidents=48,
        region="au-east",
    )


_V1.include_router(
    _GRAPHQL.router(),
    permissions=("graphql:read",),
)


# -- generated CRUD --------------------------------------------------------


def _open_read_session(request: Any) -> Session:
    return Session(_REGISTRY, "read")


_V1.include_router(
    crud_router(
        OperationsAccount,
        _open_read_session,
        prefix="/accounts",
        operations=("list", "retrieve"),
        fields=("id",),
        authorize=Access.permissions("crud:read"),
    )
)


# -- negotiated representations -------------------------------------------


def _operations_export(account_id: int) -> OperationsExport:
    return OperationsExport(
        account_id=account_id,
        bucket_count=len(_SERIES_BUCKETS),
        incident_ids=list(range(1, 13)),
        similarity_scores=[index / 16 for index in range(12)],
        generated_at="2026-03-23T03:15:00Z",
    )


@_V1.get("/exports/{account_id}/protobuf")
@permissions("exports:read")
async def protobuf_export(
    request: Any,
    account_id: Annotated[int, Path()],
) -> OperationsExport:
    return _operations_export(account_id)


@_V1.get("/exports/{account_id}/messagepack")
@permissions("exports:read")
async def messagepack_export(
    request: Any,
    account_id: Annotated[int, Path()],
) -> Any:
    return serialize(
        request,
        {
            "account_id": account_id,
            "bucket_count": len(_SERIES_BUCKETS),
            "incident_ids": list(range(1, 13)),
            "region": "au-east",
        },
        serializers=(MSGPACK,),
    )


# -- multipart --------------------------------------------------------------


@_V1.post("/imports")
@permissions("imports:write")
async def import_observations(
    request: Any,
    manifest: Annotated[ImportManifest, Form()],
    observations: Annotated[UploadedFile, File()],
) -> dict[str, Any]:
    checksum = sum(sum(chunk) for chunk in observations.chunks()) & 0xFFFF
    return {
        "source": manifest.source,
        "observations": manifest.observations,
        "dry_run": manifest.dry_run,
        "filename": observations.filename,
        "bytes": observations.size,
        "checksum": checksum,
    }


# -- MCP --------------------------------------------------------------------

_MCP_ROUTER = Router()
_MCP = MCP(
    name="wreath-operations",
    version="1.0.0",
    path="/mcp",
    authorizer=_AUTHORIZER,
)


@_MCP.tool(
    description="Inspect one account's operational summary.",
    action="Operations::inspect",
    resource=EntityUid("Operations", "42"),
)
async def inspect_operations(request: Any, account_id: int, window: str = "30d") -> dict:
    return {
        "account_id": account_id,
        "window": window,
        "buckets": len(_SERIES_BUCKETS),
        "incidents": 48,
    }


@_MCP.resource(
    "operations://accounts/42/status",
    description="Current operating status for account 42.",
    mime_type="application/json",
)
async def operations_status(request: Any) -> dict:
    return {"account_id": 42, "status": "nominal", "region": "au-east"}


@_MCP.prompt(description="Draft an operations handover for an account.")
async def operations_handover(
    request: Any,
    account_id: str,
    tone: str = "concise",
) -> str:
    return f"Draft a {tone} handover for operations account {account_id}."


_MCP.mount(_MCP_ROUTER)
_V1.include_router(_MCP_ROUTER, permissions=("mcp:use",))


# -- gRPC -------------------------------------------------------------------

_GRPC = GrpcService("wreath.operations.Reports")


@_GRPC.unary(
    request=OperationsQuery,
    response=OperationsExport,
    action="Operations::compile",
    resource=EntityUid("Operations", "42"),
)
@permissions("grpc:invoke")
async def CompileReport(request: Any, query: OperationsQuery) -> OperationsExport:
    export = _operations_export(query.account_id)
    if query.include_vectors:
        return export
    return OperationsExport(
        account_id=export.account_id,
        bucket_count=export.bucket_count,
        incident_ids=export.incident_ids,
        similarity_scores=[],
        generated_at=export.generated_at,
    )


_V1.include_router(_GRPC.router())


# -- finite realtime stream -------------------------------------------------


@_V1.get("/events")
@permissions("events:read")
async def operation_events(request: Any) -> SSEResponse:
    async def events():
        for sequence, status in enumerate(
            ("accepted", "validated", "projected", "complete"), start=1
        ):
            yield {
                "event": "report",
                "id": str(sequence),
                "data": f'{{"account_id":42,"status":"{status}"}}',
            }

    return SSEResponse(events())


# One access declaration covers the complete versioned surface. Individual
# routes add the narrower role, permission and Cedar controls above.
app.include_router(_API.router(), permissions=("operations:access",))
app.enable_api_docs(
    path="/v1/docs",
    spec_path="/v1/openapi.json",
    environments=None,
    permissions=("operations:access",),
    try_it_out=True,
    title="Wreath operations intelligence",
    version="1.0.0",
)


_COMMON_GET_HEADERS = {
    "Authorization": "Bearer holistic-user",
    "Host": "operations.example.com",
    "User-Agent": "Mozilla/5.0 (Linux; Android 14) Chrome/126.0 Mobile Safari/537.36",
    "X-Forwarded-For": "4.1.1.1",
}
_COMMON_POST_HEADERS = {
    **_COMMON_GET_HEADERS,
    "Content-Type": "application/json",
    "Origin": "https://example.com",
    "Sec-Fetch-Site": "same-origin",
}
_GRAPHQL_BODY = (
    b'{"query":"{ operationsSummary { account_id bucket_count active_incidents region } }"}'
)
_MCP_INITIALIZE_BODY = (
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":'
    f'{{"protocolVersion":"{PROTOCOL_VERSION}","capabilities":{{}},'
    '"clientInfo":{"name":"holistic-bench","version":"1"}}}'
).encode()
_MCP_TOOL_BODY = (
    b'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":'
    b'{"name":"inspect_operations","arguments":{"account_id":42,"window":"30d"}}}'
)
_MCP_RESOURCE_BODY = (
    b'{"jsonrpc":"2.0","id":3,"method":"resources/read","params":'
    b'{"uri":"operations://accounts/42/status"}}'
)
_MCP_PROMPT_BODY = (
    b'{"jsonrpc":"2.0","id":4,"method":"prompts/get","params":'
    b'{"name":"operations_handover","arguments":'
    b'{"account_id":"42","tone":"concise"}}}'
)
_MULTIPART_BOUNDARY = "wreath-holistic-boundary"
_MULTIPART_BODY = (
    b"--wreath-holistic-boundary\r\n"
    b'Content-Disposition: form-data; name="source"\r\n\r\nfield-unit-7\r\n'
    b"--wreath-holistic-boundary\r\n"
    b'Content-Disposition: form-data; name="observations"\r\n\r\n730\r\n'
    b"--wreath-holistic-boundary\r\n"
    b'Content-Disposition: form-data; name="dry_run"\r\n\r\ntrue\r\n'
    b"--wreath-holistic-boundary\r\n"
    b'Content-Disposition: form-data; name="observations"; filename="fixes.csv"\r\n'
    b"Content-Type: text/csv\r\n\r\n"
    b"timestamp,lat,lon\n2026-03-23T03:15:00Z,-27.47,153.03\n\r\n"
    b"--wreath-holistic-boundary--\r\n"
)
_GRPC_BODY = frame_message(protobuf_encode(OperationsQuery(account_id=42, include_vectors=True)))
_WEBHOOK_BODY = b'{"account_id":42,"station":"field-unit-7","severity":3}'
_WEBHOOK_ENVELOPE = WebhookEnvelope(
    id="field-observation-baseline",
    type="field.observation",
    version="1",
    timestamp=datetime.datetime(2026, 8, 13, 0, 0, tzinfo=datetime.UTC),
    content_type="application/json",
    body=_WEBHOOK_BODY,
)
_WEBHOOK_HEADERS = {
    **_COMMON_GET_HEADERS,
    "Content-Type": "application/json",
    **{
        name.decode("ascii"): value.decode("ascii")
        for name, value in HMACWebhookSigner(
            _WEBHOOK_KEYS,
            key_id="operations",
        ).headers(_WEBHOOK_ENVELOPE)
    },
}

ARMS: dict[str, Arm] = {
    "dashboard": Arm(REQUEST_METHOD, REQUEST_PATH, REQUEST_HEADERS, REQUEST_BODY),
    "graphql": Arm(
        "POST",
        "/v1/graphql",
        _COMMON_POST_HEADERS,
        _GRAPHQL_BODY,
    ),
    "crud-list": Arm(
        "GET",
        "/v1/accounts?page=1&size=20",
        _COMMON_GET_HEADERS,
    ),
    "protobuf": Arm(
        "GET",
        "/v1/exports/42/protobuf",
        {**_COMMON_GET_HEADERS, "Accept": "application/x-protobuf"},
    ),
    "messagepack": Arm(
        "GET",
        "/v1/exports/42/messagepack",
        {**_COMMON_GET_HEADERS, "Accept": "application/msgpack"},
    ),
    "multipart": Arm(
        "POST",
        "/v1/imports",
        {
            **_COMMON_POST_HEADERS,
            "Content-Type": f"multipart/form-data; boundary={_MULTIPART_BOUNDARY}",
        },
        _MULTIPART_BODY,
    ),
    "mcp-initialize": Arm(
        "POST",
        "/v1/mcp",
        _COMMON_POST_HEADERS,
        _MCP_INITIALIZE_BODY,
    ),
    "mcp-tool": Arm(
        "POST",
        "/v1/mcp",
        _COMMON_POST_HEADERS,
        _MCP_TOOL_BODY,
        setup="mcp",
    ),
    "mcp-resource": Arm(
        "POST",
        "/v1/mcp",
        _COMMON_POST_HEADERS,
        _MCP_RESOURCE_BODY,
        setup="mcp",
    ),
    "mcp-prompt": Arm(
        "POST",
        "/v1/mcp",
        _COMMON_POST_HEADERS,
        _MCP_PROMPT_BODY,
        setup="mcp",
    ),
    "grpc": Arm(
        "POST",
        "/v1/wreath.operations.Reports/CompileReport",
        {
            **_COMMON_POST_HEADERS,
            "Content-Type": "application/grpc",
            "TE": "trailers",
        },
        _GRPC_BODY,
        http_version="2",
    ),
    "sse": Arm("GET", "/v1/events", _COMMON_GET_HEADERS),
    "series-live": Arm(
        "GET",
        "/v1/accounts/42/activity-series",
        _COMMON_GET_HEADERS,
    ),
    "enterprise": Arm(
        "GET",
        "/v1/enterprise/accounts/42",
        {**_COMMON_GET_HEADERS, "X-Tenant": "acme"},
    ),
    "admin-detail": Arm(
        "GET",
        "/v1/admin/operations-view/42",
        _COMMON_GET_HEADERS,
    ),
    "scim-search": Arm(
        "GET",
        "/v1/scim/v2/Users?filter=userName%20eq%20%22holistic-user@example.com%22",
        {
            "Authorization": "Bearer operations-directory",
            "Accept": "application/scim+json",
            "Host": "operations.example.com",
        },
    ),
    "sync-snapshot": Arm(
        "GET",
        "/v1/sync/accounts/mine",
        _COMMON_GET_HEADERS,
    ),
    "websocket-room": Arm(
        "WEBSOCKET",
        "/v1/live/accounts/42",
        _COMMON_GET_HEADERS,
        b'{"event":"status","sequence":730}',
        driver="websocket",
    ),
    "signed-webhook": Arm(
        "POST",
        "/v1/webhooks/field-observations",
        _WEBHOOK_HEADERS,
        _WEBHOOK_BODY,
        driver="signed-webhook",
    ),
    "workflow": Arm(
        "POST",
        "/v1/workflows/reports",
        _COMMON_POST_HEADERS,
        b"{}",
    ),
    "readiness": Arm("GET", "/v1/ready", _COMMON_GET_HEADERS),
    "ai-search-opt-in": Arm(
        "GET",
        "/v1/ready",
        {**_COMMON_GET_HEADERS, "User-Agent": "OAI-SearchBot/1.0"},
    ),
    "ai-scraper-refusal": Arm(
        "GET",
        "/v1/ready",
        {**_COMMON_GET_HEADERS, "User-Agent": "GPTBot/1.0"},
    ),
    "metrics": Arm("GET", "/v1/metrics", _COMMON_GET_HEADERS),
    "openapi": Arm("GET", "/v1/openapi.json", _COMMON_GET_HEADERS),
}
