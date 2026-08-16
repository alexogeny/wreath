"""Applications for `wreath-request-trace` to measure.

A bare route with no middleware crosses the boundary a handful of times and
proves nothing. The question `wreath-request-trace` exists to answer -- how much
Python runs before a route is activated -- only has a meaningful answer against
an app shaped like one someone would deploy: proxy headers, security headers,
CORS, CSRF, rate limiting, request IDs, timing, bearer authentication,
role/permission authorization, a policy check, a non-trivial route table, and a
handler that reaches the database through the ORM.

The database is scripted rather than live, in the same spirit as
`tests/orm/conftest.py`: it speaks the `wreath.postgres.Connection` surface, so the
real compiler, session, and hydrator run, while the trace stays reproducible on
a machine with no PostgreSQL. Driver-internal crossings (protocol decode) are
therefore out of scope here -- `benchmarks/postgres/` covers those.
"""

from __future__ import annotations

import datetime
from typing import Any

from wreath import Wreath
from wreath._auth.backends import BearerTokenBackend
from wreath._auth.decorators import authorize, permissions, roles
from wreath._auth.models import AuthorizationDecision, Identity
from wreath._auth.requirements import PolicyRequirement
from wreath.orm import Mapped, Model, column, relationship
from wreath.orm.registry import Registry
from wreath.orm.session import Session
from wreath.orm.types import Int64, Text, Timestamp
from wreath.policy import AIScrapingPolicy, HttpPolicy
from wreath.policy.cors import CorsPolicy
from wreath.policy.csrf import CsrfPolicy
from wreath.policy.proxy import ProxyPolicy
from wreath.policy.ratelimit import RateLimitPolicy
from wreath.policy.request_id import RequestIdPolicy
from wreath.policy.security import SecurityHeadersPolicy
from wreath.policy.timing import ServerTimingPolicy
from wreath.request import Request

CSRF_SECRET = "x" * 32


class TracedUser(Model, table="users"):
    id: Mapped[int] = column(Int64, primary_key=True)
    email: Mapped[str] = column(Text, unique=True)
    name: Mapped[str] = column(Text)
    created_at: Mapped[object] = column(Timestamp, nullable=True)
    posts = relationship("TracedPost", foreign_key="author_id", load="raise")


class TracedPost(Model, table="posts"):
    id: Mapped[int] = column(Int64, primary_key=True)
    author_id: Mapped[int] = column(Int64, references=TracedUser.id)
    title: Mapped[str] = column(Text)
    author = relationship(TracedUser, foreign_key=author_id, load="raise")


class _ScriptedConnection:
    """The `wreath.postgres.Connection` surface, replaying canned rows."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.closed = False
        self._plans: dict[str, Any] = {}
        self.responses: list[tuple[str, Any]] = []

    def script(self, fragment: str, rows: Any) -> None:
        self.responses.append((fragment, rows))

    def _result(self, sql: str) -> Any:
        for fragment, rows in self.responses:
            if fragment in sql:
                return rows
        return []

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append((sql, args))
        return "OK"

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        self.calls.append((sql, args))
        return list(self._result(sql))

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.calls.append((sql, args))
        rows = self._result(sql)
        return rows[0] if rows else None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        row = await self.fetchrow(sql, *args)
        return row[0] if row else None

    async def close(self) -> None:
        self.closed = True


class _ScriptedPool:
    def __init__(self, workload: str) -> None:
        self.workload = workload


class _ScriptedDatabase:
    def __init__(self) -> None:
        self.name = "main"
        self.connection = _ScriptedConnection()
        self.acquired = 0

    def pool(self, workload: str) -> _ScriptedPool:
        return _ScriptedPool(workload)

    async def acquire(self, workload: str = "read") -> _ScriptedConnection:
        self.acquired += 1
        return self.connection

    async def release(self, workload: str, connection: _ScriptedConnection) -> None:
        return None

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


class _Authorizer:
    """A policy provider, so `authorize(...)` routes exercise the policy stage."""

    async def authorize(
        self, request: Request, requirement: PolicyRequirement
    ) -> AuthorizationDecision:
        identity = request.identity
        if identity is None:
            return AuthorizationDecision(allowed=False, reason="anonymous")
        return AuthorizationDecision(allowed=requirement.action == "read")


# Ordered as a deployment would be: trust the proxy before anything reads a
# forwarded Host, and rate-limit before doing real work for the caller.
#
# Factories, not instances. Policy carries mutable operational state -- a token
# bucket above all -- and sharing one instance across benchmark arms drains it,
# after which the "measurement" is of 429 responses. Callers that need several
# independent policies (`wreath-policy-decomp`) build a fresh one per arm.
POLICY_FACTORIES: tuple[Any, ...] = (
    lambda: ProxyPolicy(trusted=["127.0.0.1"]),
    AIScrapingPolicy,
    # Deliberately unreachable: a benchmark drives millions of requests through
    # one bucket, and a limit it can exhaust turns the arm into a 429 path.
    lambda: RateLimitPolicy(limit=1_000_000_000),
    lambda: CorsPolicy(allow_origins=["https://example.com"]),
    lambda: CsrfPolicy(CSRF_SECRET, secure=False),
    lambda: SecurityHeadersPolicy(
        content_security_policy="default-src 'self'; frame-ancestors 'none'",
        permissions_policy="geolocation=()",
    ),
    lambda: RequestIdPolicy(),
    lambda: ServerTimingPolicy(),
)


def policy_from_components(components: list[Any] | tuple[Any, ...]) -> HttpPolicy:
    """Build the fixed first-class policy from independently measured parts."""
    fields: dict[str, Any] = {}
    for component in components:
        if type(component) is ProxyPolicy:
            fields["proxy"] = component
        elif type(component) is AIScrapingPolicy:
            fields["ai_scraping"] = component
        elif type(component) is RateLimitPolicy:
            fields["rate_limit"] = component
        elif type(component) is CorsPolicy:
            fields["cors"] = component
        elif type(component) is CsrfPolicy:
            fields["csrf"] = component
        elif type(component) is SecurityHeadersPolicy:
            fields["security_headers"] = component
        elif type(component) is RequestIdPolicy:
            fields["request_id"] = component
        elif type(component) is ServerTimingPolicy:
            fields["server_timing"] = component
        else:
            raise TypeError(f"not a built-in HTTP policy component: {component!r}")
    return HttpPolicy(**fields)


def build_realistic_app() -> tuple[Wreath, dict[str, str], str, str]:
    """The full stack. Returns (app, headers, default_method, default_path)."""
    database = _ScriptedDatabase()
    database.connection.script(
        "users", [[1, "a@b.c", "A", datetime.datetime(2024, 1, 1)]]
    )
    registry = Registry(
        database, [TracedUser, TracedPost], validate_schema="off"
    )

    async def verify(token: str) -> Identity | None:
        if token != "tok":
            return None
        return Identity(
            id="u1",
            roles=frozenset({"admin", "staff"}),
            permissions=frozenset({"users:read", "users:write"}),
        )

    app = Wreath(
        http_policy=policy_from_components(
            [factory() for factory in POLICY_FACTORIES]
        )
    )
    app.configure_auth(BearerTokenBackend(verify), _Authorizer())

    # A route table with enough shape that classification is a real decision:
    # static and parametrized siblings, several methods, varying depth.
    @app.get("/health")
    async def health(request: Request) -> Any:
        return {"ok": True}

    @app.get("/users")
    @roles("staff")
    async def list_users(request: Request) -> Any:
        session = Session(registry, "read")
        try:
            return {"users": [u.id for u in await session.fetch(TracedUser.select())]}
        finally:
            await session.close()

    @app.get("/users/{user_id}")
    @roles("admin")
    @authorize(action="read", resource=TracedUser)
    async def get_user(request: Request) -> Any:
        session = Session(registry, "read")
        try:
            user = await session.fetch_one(
                TracedUser.select().where(TracedUser.id == int(request.path_params["user_id"]))
            )
            return {"id": user.id, "email": user.email, "name": user.name}
        finally:
            await session.close()

    @app.post("/users")
    @permissions("users:write")
    async def create_user(request: Request) -> Any:
        payload = await request.json()
        return {"created": payload.get("email")}

    @app.get("/orgs/{org_id}/members/{user_id}")
    @roles("admin")
    async def member(request: Request) -> Any:
        return dict(request.path_params)

    @app.get("/posts/{post_id}")
    async def get_post(request: Request) -> Any:
        return {"id": request.path_params["post_id"]}

    headers = {
        "host": "example.com",
        "origin": "https://example.com",
        "authorization": "Bearer tok",
        "user-agent": "wreath-request-trace",
        "accept": "*/*",
        "accept-encoding": "gzip, br",
        "x-forwarded-for": "203.0.113.7",
        "x-forwarded-proto": "https",
    }
    return app, headers, "GET", "/users/1"


def build_minimal_app() -> tuple[Wreath, dict[str, str], str, str]:
    """One route, nothing else: the floor the realistic app is measured against."""
    app = Wreath()

    @app.get("/health")
    async def health(request: Request) -> Any:
        return {"ok": True}

    return app, {"host": "example.com"}, "GET", "/health"


SCENARIOS = {
    "realistic": build_realistic_app,
    "minimal": build_minimal_app,
}
