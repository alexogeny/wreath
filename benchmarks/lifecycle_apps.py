"""DB-backed full-lifecycle benchmark applications selected through WREATH_BENCH_FRAMEWORK.

Every framework implements the same endpoint with identical per-request work:

    POST /api/v2/organizations/{organization_id}/admin-console/admin/users/{user_id}

The target sits beside 24 domain branches with 16 decoy leaves each. Wreath's
permission-aware decision router can prune the 23 ineligible sibling subtrees.
The route table's literals come from ``benchmarks.lifecycle_routes``, which
explains why they are words rather than ``domain-7``/``resource-13``.

1. Match the deep route in the 385-route tree.
2. Extract the bearer token from the Authorization header.
3. Authenticate it against the ``bench_users`` table (one point SELECT).
4. Require the ``admin`` role; unauthenticated/unauthorized requests get 401/403
   and never reach the mutation.
5. Decode the JSON request body.
6. Mutate the target user row (one UPDATE .. RETURNING that bumps ``version``).
7. Return the mutated row with the same CSP and four related security headers.

Fairness rules shared by every application:

- The same two SQL statements (``AUTH_SQL``/``MUTATE_SQL``) with the same
  argument shapes; both driver layers auto-prepare statements.
- One bounded connection pool sized by ``WREATH_BENCH_DB_POOL``; every query is an
  acquire -> run -> release cycle on that pool (``wreath.postgres.Statement`` and
  ``asyncpg.Pool.fetchrow`` have the same per-query lease semantics).
- The pool is created lazily on the first request behind an asyncio lock, so no
  framework needs lifespan support and warmup absorbs the cost identically.

Wreath runs on its own driver facade (``wreath.postgres``); Sanic and BlackSheep use
asyncpg, their ecosystem-standard PostgreSQL driver.
"""

from __future__ import annotations

import asyncio
import importlib
import os
from typing import Any, cast

from benchmarks.lifecycle_routes import (
    API_PREFIX_TEMPLATE,
    BRANCH_WORDS,
    ROUTE_BRANCHES,
    ROUTES_PER_BRANCH,
    TARGET_BRANCH,
    TARGET_BRANCH_WORD,
    leaf_suffix,
)

FRAMEWORK = os.environ.get("WREATH_BENCH_FRAMEWORK", "wreath")
DSN = os.environ.get("WREATH_BENCH_DSN", "postgresql://wreath:secret@127.0.0.1:5432/wreath")
POOL_SIZE = int(os.environ.get("WREATH_BENCH_DB_POOL", "4"))
API_PREFIX = API_PREFIX_TEMPLATE

CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'self'"
)
SECURITY_HEADERS = (
    ("content-security-policy", CONTENT_SECURITY_POLICY),
    ("x-frame-options", "DENY"),
    ("x-content-type-options", "nosniff"),
    ("referrer-policy", "strict-origin-when-cross-origin"),
    ("permissions-policy", "camera=(), microphone=(), geolocation=()"),
    # Deliberately no HSTS -- the scenario is cleartext. See the note on
    # EXPECTED_SECURITY_HEADERS in benchmarks/lifecycle.py.
)

AUTH_SQL = "select id, role from bench_users where token = $1"
MUTATE_SQL = (
    "update bench_users set full_name = $1, version = version + 1 "
    "where id = $2 returning id, full_name, version"
)


def _bearer_token(value: str | None) -> str | None:
    if not value:
        return None
    scheme, separator, token = value.partition(" ")
    if not separator or not token or scheme.lower() != "bearer":
        return None
    return token


def _mutation_payload(row: Any) -> dict[str, Any]:
    return {"id": row[0], "name": row[1], "version": row[2]}


class _AsyncpgDatabase:
    """Lazily started asyncpg pool with per-query lease semantics."""

    def __init__(self) -> None:
        self._pool: Any = None
        self._lock = asyncio.Lock()

    async def pool(self) -> Any:
        if self._pool is None:
            async with self._lock:
                if self._pool is None:
                    asyncpg: Any = importlib.import_module("asyncpg")
                    self._pool = await asyncpg.create_pool(
                        DSN, min_size=POOL_SIZE, max_size=POOL_SIZE
                    )
        return self._pool

    async def authenticate(self, authorization: str | None) -> Any:
        token = _bearer_token(authorization)
        if token is None:
            return None
        pool = await self.pool()
        return await pool.fetchrow(AUTH_SQL, token)

    async def mutate(self, name: str, user_id: int) -> Any:
        pool = await self.pool()
        return await pool.fetchrow(MUTATE_SQL, name, user_id)


if FRAMEWORK in {"wreath", "wreath-native", "wreath-metal"}:
    from wreath import JSONResponse, Router, Wreath
    from wreath.auth import BearerTokenBackend, Identity
    from wreath.middleware import Middleware, SecurityHeadersMiddleware
    from wreath.postgres import Database, PoolConfig

    app = Wreath(routing=os.environ.get("WREATH_BENCH_ROUTING", "decision"))
    app.add_middleware(
        cast(
            Middleware,
            SecurityHeadersMiddleware(
                content_security_policy=CONTENT_SECURITY_POLICY,
                permissions_policy="camera=(), microphone=(), geolocation=()",
            ),
        )
    )

    _wreath_database = Database(
        "bench",
        DSN,
        pools={"write": PoolConfig(min_size=POOL_SIZE, max_size=POOL_SIZE)},
    )
    _auth_statement = _wreath_database.statement("auth", AUTH_SQL, workload="write")
    _mutate_statement = _wreath_database.statement("mutate", MUTATE_SQL, workload="write")
    _start_lock = asyncio.Lock()

    async def _ensure_started() -> None:
        if not _wreath_database.started:
            async with _start_lock:
                if not _wreath_database.started:
                    await _wreath_database.start()

    async def verify_lifecycle_token(token: str) -> Identity | None:
        await _ensure_started()
        row = await _auth_statement.fetchrow(token)
        if row is None:
            return None
        return Identity(
            str(row[0]),
            roles=frozenset({row[1]}),
            permissions=frozenset(
                {"organizations:access", f"domain:{TARGET_BRANCH}:write"}
            ),
        )

    app.configure_auth(BearerTokenBackend(verify_lifecycle_token))

    async def decoy_endpoint(request):
        return {"resource": request.path_params["item_id"]}

    organization = Router(
        prefix=API_PREFIX, permissions=("organizations:access",)
    )
    for branch_number in range(ROUTE_BRANCHES):
        branch = Router(
            prefix=f"/{BRANCH_WORDS[branch_number]}",
            permissions=(f"domain:{branch_number}:write",),
        )
        for leaf in range(ROUTES_PER_BRANCH):
            branch.get(leaf_suffix(leaf, "{item_id}"))(decoy_endpoint)
        organization.include_router(branch)

    target = Router(
        prefix=f"/{TARGET_BRANCH_WORD}",
        permissions=(f"domain:{TARGET_BRANCH}:write",),
    )

    @target.post("/admin/users/{user_id}")
    async def mutate_user(request):
        if "admin" not in request.identity.roles:
            return JSONResponse({"error": "forbidden"}, status=403)
        payload = await request.json()
        row = await _mutate_statement.fetchrow(
            payload["name"], int(request.path_params["user_id"])
        )
        return _mutation_payload(row)

    organization.include_router(target)
    app.include_router(organization)

elif FRAMEWORK == "sanic":
    from sanic import Sanic
    from sanic.response import json as sanic_json

    app = Sanic("wreath_lifecycle_benchmark")
    _database = _AsyncpgDatabase()

    @app.middleware("response")
    async def add_security_headers(request, response):
        for name, value in SECURITY_HEADERS:
            response.headers[name] = value

    async def decoy_endpoint(request, organization_id: int, item_id: int):
        return sanic_json({"resource": item_id})

    for branch_number in range(ROUTE_BRANCHES):
        for leaf in range(ROUTES_PER_BRANCH):
            path = (
                f"/api/v2/organizations/<organization_id:int>"
                f"/{BRANCH_WORDS[branch_number]}"
                f"{leaf_suffix(leaf, '<item_id:int>')}"
            )
            app.add_route(
                decoy_endpoint,
                path,
                methods=("GET",),
                name=f"decoy_{branch_number}_{leaf}",
            )

    @app.post(
        f"/api/v2/organizations/<organization_id:int>/{TARGET_BRANCH_WORD}"
        "/admin/users/<user_id:int>"
    )
    async def mutate_user(request, organization_id: int, user_id: int):
        identity = await _database.authenticate(request.headers.get("authorization"))
        if identity is None:
            return sanic_json({"error": "unauthorized"}, status=401)
        if identity[1] != "admin":
            return sanic_json({"error": "forbidden"}, status=403)
        row = await _database.mutate(request.json["name"], user_id)
        return sanic_json(_mutation_payload(row))

elif FRAMEWORK == "blacksheep":
    from blacksheep import Request
    from blacksheep.server.application import Application
    from blacksheep.server.responses import json as blacksheep_json

    app = Application()
    _database = _AsyncpgDatabase()

    async def add_security_headers(request: Request, handler):
        response = await handler(request)
        for name, value in SECURITY_HEADERS:
            response.add_header(name.encode("latin-1"), value.encode("latin-1"))
        return response

    app.middlewares.append(add_security_headers)

    async def decoy_endpoint(request: Request, organization_id: int, item_id: int):
        return blacksheep_json({"resource": item_id})

    for branch_number in range(ROUTE_BRANCHES):
        for leaf in range(ROUTES_PER_BRANCH):
            path = (
                f"/api/v2/organizations/{{organization_id}}"
                f"/{BRANCH_WORDS[branch_number]}"
                f"{leaf_suffix(leaf, '{item_id}')}"
            )
            app.router.get(path)(decoy_endpoint)

    @app.router.post(
        f"/api/v2/organizations/{{organization_id}}/{TARGET_BRANCH_WORD}"
        "/admin/users/{user_id}"
    )
    async def mutate_user(request: Request, organization_id: int, user_id: int):
        header = request.get_first_header(b"authorization")
        identity = await _database.authenticate(
            header.decode("latin-1") if header is not None else None
        )
        if identity is None:
            return blacksheep_json({"error": "unauthorized"}, status=401)
        if identity[1] != "admin":
            return blacksheep_json({"error": "forbidden"}, status=403)
        payload = await request.json()
        row = await _database.mutate(payload["name"], user_id)
        return blacksheep_json(_mutation_payload(row))

else:
    raise RuntimeError(f"unsupported lifecycle benchmark framework: {FRAMEWORK}")
