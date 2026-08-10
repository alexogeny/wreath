from __future__ import annotations

from typing import Any

import pytest
from _pgfidelity import check_for

from wreath import Wreath
from wreath.auth import BearerTokenBackend, Identity, authenticated
from wreath.postgres import PoolConfig
from wreath.testing import TestClient


class FakeConnection:
    def __init__(self) -> None:
        self.closed = False
        self.lookups = 0

    async def execute(self, sql: str, *args: object) -> str:
        check_for(self, sql, args)
        return "OK"

    async def prepare(self, sql: str) -> None:
        return None

    async def fetchrow(self, sql: str, *args: object) -> dict[str, object] | None:
        check_for(self, sql, args)
        self.lookups += 1
        if args == ("valid",):
            return {"id": "7", "roles": frozenset({"member"})}
        return None

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_database_backed_auth_composes_through_identity_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()

    async def connect(dsn: str) -> FakeConnection:
        return connection

    monkeypatch.setattr("wreath.postgres.connect", connect)
    app = Wreath()
    db = app.postgres(
        "main",
        dsn="postgresql://primary/app",
        pools={"security_read": PoolConfig(min_size=1, max_size=1)},
    )
    resolve_session = db.statement(
        "security.resolve_session",
        "select id, roles from sessions where token = $1",
        workload="security_read",
    )

    async def verify(token: str) -> Identity | None:
        row = await resolve_session.fetchrow(token)
        if row is None:
            return None
        return Identity(id=str(row["id"]), roles=row["roles"])  # type: ignore[arg-type]

    app.configure_auth(BearerTokenBackend(verify))

    @app.get("/public")
    async def public(request: Any) -> dict[str, bool]:
        return {"public": True}

    @app.get("/account")
    @authenticated()
    async def account(request: Any) -> dict[str, str]:
        return {"id": request.identity.id}

    async with TestClient(app) as client:
        public_response = await client.request("GET", "/public")
        assert public_response.status == 200
        assert connection.lookups == 0

        unauthorized = await client.request("GET", "/account")
        assert unauthorized.status == 401
        assert connection.lookups == 0

        authorized = await client.request(
            "GET", "/account", headers={"authorization": "Bearer valid"}
        )
        assert authorized.status == 200
        assert authorized.json() == {"id": "7"}
        assert connection.lookups == 1
        assert db.pool("security_read").borrowed == 0
