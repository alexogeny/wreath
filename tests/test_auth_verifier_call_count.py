"""The bearer verifier (and its backing DB query) must run exactly once per request.

Thesis under test: profiling the lifecycle benchmark suggested the auth backend
authenticates twice per request (decision-router probe + route guard), which
would mean a duplicated database SELECT on every authenticated request.
"""

from wreath import Wreath
from wreath.auth import BearerTokenBackend, Identity
from wreath.authorization import roles
from wreath.response import TextResponse
from wreath.testing import TestClient


def _counting_app() -> tuple[Wreath, list[str]]:
    calls: list[str] = []
    app = Wreath()

    async def verify(token: str) -> Identity | None:
        calls.append(token)
        if token == "admin":
            return Identity("admin", roles=frozenset({"admin"}))
        return None

    app.configure_auth(BearerTokenBackend(verify))

    @app.post("/admin/users/{user_id}")
    @roles("admin")
    async def mutate(request):
        return TextResponse(request.path_params["user_id"])

    return app, calls


async def test_verifier_runs_exactly_once_per_authorized_request():
    app, calls = _counting_app()
    client = TestClient(app)
    response = await client.post(
        "/admin/users/42", headers={"Authorization": "Bearer admin"}
    )
    assert response.status == 200
    assert len(calls) == 1, f"verifier ran {len(calls)}x for one request: {calls}"


async def test_verifier_runs_exactly_once_per_denied_request():
    app, calls = _counting_app()
    client = TestClient(app)
    response = await client.post(
        "/admin/users/42", headers={"Authorization": "Bearer wrong"}
    )
    assert response.status in (401, 403)
    assert len(calls) == 1, f"verifier ran {len(calls)}x for one request: {calls}"
