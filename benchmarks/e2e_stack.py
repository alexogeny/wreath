"""Equivalent pragmatic service stacks for retired-instruction accounting.

The selected application is controlled by two environment variables because a
measurement process must import exactly one framework and one cumulative arm:

``WREATH_E2E_FRAMEWORK``
    ``wreath`` or ``fastapi``.

``WREATH_E2E_ARM``
    ``route``, ``cors``, ``binding``, ``auth``, ``cedar``, ``postgres`` or
    ``complete``. ``complete-aa`` is an identical complete-stack control.

Every arm answers the same successful POST. Later arms replace fixture values
with work rather than changing the representation, so adjacent instruction
slopes price one addition. PostgreSQL and HTTP use the same deterministic
in-process wire peers as Wreath's established e2e benchmark. They are real
client-driver round trips without an external daemon, disk, DNS, clock or
randomness entering the measured request.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Annotated, Any

from .e2e_upstream import _UPSTREAM_BODY, BenchPostgres, BenchUpstreamHttp

FRAMEWORK = os.environ.get("WREATH_E2E_FRAMEWORK", "wreath")
ARM = os.environ.get("WREATH_E2E_ARM", "complete")
ARM_ORDER = ("route", "cors", "binding", "auth", "cedar", "postgres", "complete")
EFFECTIVE_ARM = "complete" if ARM == "complete-aa" else ARM

if FRAMEWORK not in {"wreath", "fastapi"}:
    raise RuntimeError(f"WREATH_E2E_FRAMEWORK must be 'wreath' or 'fastapi', got {FRAMEWORK!r}")
if EFFECTIVE_ARM not in ARM_ORDER:
    raise RuntimeError(
        "WREATH_E2E_ARM must be route, cors, binding, auth, cedar, postgres, "
        f"complete or complete-aa, got {ARM!r}"
    )

ARM_INDEX = ARM_ORDER.index(EFFECTIVE_ARM)
ALLOWED_ORIGIN = "https://console.example"
POLICY = 'permit(principal == User::"user", action == Action::"read", resource == Document::"42");'
REQUEST_PATH = "/api/reports/42?limit=3"
REQUEST_BODY = b'{"title":"Quarterly report","tags":["finance","internal"]}'
REQUEST_HEADERS = {
    "Authorization": "Bearer user",
    "Content-Type": "application/json",
    "Origin": ALLOWED_ORIGIN,
}
EXPECTED = {
    "user": "user",
    "item_id": 42,
    "limit": 3,
    "title": "Quarterly report",
    "tag_count": 2,
    "db": 42,
    "upstream_status": 200,
    "upstream_bytes": len(_UPSTREAM_BODY),
}


class _Dependencies:
    """One lazy pair of real clients over deterministic local wire peers."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.postgres_peer: BenchPostgres | None = None
        self.http_peer: BenchUpstreamHttp | None = None
        self.database: Any = None
        self.http: Any = None

    async def wreath(self) -> _Dependencies:
        if self.database is not None:
            return self
        async with self._lock:
            if self.database is not None:
                return self
            from wreath import postgres
            from wreath.http_client import ClientLimits, DestinationPolicy, HTTPClient

            self.postgres_peer = BenchPostgres()
            dsn = await self.postgres_peer.start()
            self.database = await postgres.connect(dsn)
            self.http_peer = BenchUpstreamHttp()
            port = await self.http_peer.start()
            self.http = HTTPClient(
                "stack-e2e",
                base_url=f"http://127.0.0.1:{port}",
                limits=ClientLimits(max_connections=64, max_keepalive_connections=64),
                destination=DestinationPolicy(allow_private=True, allow_loopback=True),
            )
            await self.http.start()
        return self

    async def fastapi(self) -> _Dependencies:
        if self.database is not None:
            return self
        async with self._lock:
            if self.database is not None:
                return self
            import aiohttp
            import asyncpg

            self.postgres_peer = BenchPostgres()
            dsn = await self.postgres_peer.start()

            async def no_reset(connection: Any) -> None:
                return None

            self.database = await asyncpg.create_pool(
                dsn,
                min_size=64,
                max_size=64,
                ssl=False,
                reset=no_reset,
            )
            self.http_peer = BenchUpstreamHttp()
            port = await self.http_peer.start()
            self.http = aiohttp.ClientSession(
                base_url=f"http://127.0.0.1:{port}",
                connector=aiohttp.TCPConnector(limit=64, limit_per_host=64),
                trust_env=False,
            )
        return self


_DEPENDENCIES = _Dependencies()


def _result(
    *,
    user: str = "user",
    item_id: int = 42,
    limit: int = 3,
    title: str = "Quarterly report",
    tags: list[str] | tuple[str, ...] = ("finance", "internal"),
    database: int = 42,
    upstream_status: int = 200,
    upstream_bytes: int = len(_UPSTREAM_BODY),
) -> dict[str, Any]:
    return {
        "user": user,
        "item_id": item_id,
        "limit": limit,
        "title": title,
        "tag_count": len(tags),
        "db": database,
        "upstream_status": upstream_status,
        "upstream_bytes": upstream_bytes,
    }


if FRAMEWORK == "wreath":
    from wreath import Wreath
    from wreath.auth import BearerTokenBackend, Identity, authenticated
    from wreath.authorization import CedarAuthorizer, CedarPolicies, authorize
    from wreath.binding import Body, Field, Path, Query
    from wreath.policy import CorsPolicy, HttpPolicy

    @dataclass(frozen=True, slots=True)
    class ReportPayload:
        title: Annotated[str, Field(min_length=4, max_length=80)]
        tags: Annotated[list[str], Field(min_length=1, max_length=8)]

    policy = (
        HttpPolicy(cors=CorsPolicy(allow_origins=[ALLOWED_ORIGIN]))
        if ARM_INDEX >= ARM_ORDER.index("cors")
        else None
    )
    app = Wreath(http_policy=policy)

    if ARM_INDEX >= ARM_ORDER.index("auth"):
        identity = Identity("user")

        def verify(token: str) -> Identity | None:
            return identity if token == "user" else None

        authorizer = (
            CedarAuthorizer(engine=CedarPolicies(POLICY))
            if ARM_INDEX >= ARM_ORDER.index("cedar")
            else None
        )
        app.configure_auth(BearerTokenBackend(verify), authorizer)

    if ARM_INDEX < ARM_ORDER.index("binding"):

        @app.post("/api/reports/42")
        async def report(request: Any) -> dict[str, Any]:
            return _result()

    else:

        async def bound_report(
            request: Any,
            item_id: Annotated[int, Path()],
            limit: Annotated[int, Query(minimum=1, maximum=8)],
            payload: Annotated[ReportPayload, Body()],
        ) -> dict[str, Any]:
            user = request.identity.id if ARM_INDEX >= ARM_ORDER.index("auth") else "user"
            database = 42
            upstream_status = 200
            upstream_bytes = len(_UPSTREAM_BODY)
            if ARM_INDEX >= ARM_ORDER.index("postgres"):
                state = await _DEPENDENCIES.wreath()
                if ARM_INDEX >= ARM_ORDER.index("complete"):
                    fetch = asyncio.create_task(state.http.get("/data"))
                    try:
                        database = await state.database.fetchval("select $1::int4", 42)
                    except BaseException:
                        fetch.cancel()
                        raise
                    response = await fetch
                    upstream_status = response.status
                    upstream_bytes = len(response.body)
                else:
                    database = await state.database.fetchval("select $1::int4", 42)
            return _result(
                user=user,
                item_id=item_id,
                limit=limit,
                title=payload.title,
                tags=payload.tags,
                database=database,
                upstream_status=upstream_status,
                upstream_bytes=upstream_bytes,
            )

        endpoint: Any = bound_report
        if ARM_INDEX >= ARM_ORDER.index("cedar"):
            endpoint = authorize(action="read", resource='Document::"42"')(endpoint)
        if ARM_INDEX >= ARM_ORDER.index("auth"):
            endpoint = authenticated()(endpoint)
        app.post("/api/reports/{item_id}")(endpoint)

else:
    from cedarpy import Decision, is_authorized
    from fastapi import Depends, FastAPI, HTTPException, Security
    from fastapi import Query as FastAPIQuery
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
    from pydantic import BaseModel, Field

    class ReportPayload(BaseModel):
        title: Annotated[str, Field(min_length=4, max_length=80)]
        tags: Annotated[list[str], Field(min_length=1, max_length=8)]

    app = FastAPI()
    if ARM_INDEX >= ARM_ORDER.index("cors"):
        app.add_middleware(CORSMiddleware, allow_origins=[ALLOWED_ORIGIN])

    bearer = HTTPBearer(auto_error=False)
    cedar_entities = json.dumps([])
    cedar_request = {
        "principal": 'User::"user"',
        "action": 'Action::"read"',
        "resource": 'Document::"42"',
        "context": {},
    }

    async def authenticate(
        credential: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
    ) -> str:
        if (
            credential is None
            or credential.scheme.lower() != "bearer"
            or credential.credentials != "user"
        ):
            raise HTTPException(status_code=401, detail="invalid bearer token")
        return "user"

    async def authorize_user(user: Annotated[str, Depends(authenticate)]) -> str:
        decision = is_authorized(cedar_request, POLICY, cedar_entities)
        if decision.decision != Decision.Allow:
            raise HTTPException(status_code=403, detail="forbidden")
        return user

    if ARM_INDEX < ARM_ORDER.index("binding"):

        @app.post("/api/reports/42")
        async def report() -> dict[str, Any]:
            return _result()

    else:
        dependency = authorize_user if ARM_INDEX >= ARM_ORDER.index("cedar") else authenticate

        if ARM_INDEX < ARM_ORDER.index("auth"):

            @app.post("/api/reports/{item_id}")
            async def report_bound(
                item_id: int,
                payload: ReportPayload,
                limit: Annotated[int, FastAPIQuery(ge=1, le=8)],
            ) -> dict[str, Any]:
                return _result(
                    item_id=item_id,
                    limit=limit,
                    title=payload.title,
                    tags=payload.tags,
                )

        else:

            @app.post("/api/reports/{item_id}")
            async def report_full(
                item_id: int,
                payload: ReportPayload,
                limit: Annotated[int, FastAPIQuery(ge=1, le=8)],
                user: Annotated[str, Depends(dependency)],
            ) -> dict[str, Any]:
                database = 42
                upstream_status = 200
                upstream_bytes = len(_UPSTREAM_BODY)
                if ARM_INDEX >= ARM_ORDER.index("postgres"):
                    state = await _DEPENDENCIES.fastapi()
                    if ARM_INDEX >= ARM_ORDER.index("complete"):
                        fetch = asyncio.create_task(state.http.get("/data"))
                        try:
                            database = await state.database.fetchval("select $1::int4", 42)
                        except BaseException:
                            fetch.cancel()
                            raise
                        response = await fetch
                        body = await response.read()
                        upstream_status = response.status
                        upstream_bytes = len(body)
                        response.release()
                    else:
                        database = await state.database.fetchval("select $1::int4", 42)
                return _result(
                    user=user,
                    item_id=item_id,
                    limit=limit,
                    title=payload.title,
                    tags=payload.tags,
                    database=database,
                    upstream_status=upstream_status,
                    upstream_bytes=upstream_bytes,
                )
