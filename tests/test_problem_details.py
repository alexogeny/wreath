from __future__ import annotations

from typing import Any

import pytest

from wreath import Wreath
from wreath.exceptions import Forbidden, MethodNotAllowed, Unauthorized
from wreath.response import ProblemDetail, ProblemResponse
from wreath.testing import TestClient


def test_problem_response_serializes_rfc_9457_members() -> None:
    response = ProblemResponse(
        ProblemDetail(
            409,
            detail="Version conflict",
            type="https://wreath.example/problems/conflict",
            instance="/items/7",
            extensions={"revision": 3},
        )
    )
    assert response.status == 409
    assert (b"content-type", b"application/problem+json") in response.headers


def test_unauthorized_challenge_header_is_explicitly_optional() -> None:
    assert Unauthorized(challenge='Basic realm="admin"').headers == (
        (b"www-authenticate", b'Basic realm="admin"'),
    )
    assert Unauthorized(challenge=None).headers == ()


def test_method_not_allowed_only_emits_allow_for_declared_methods() -> None:
    assert MethodNotAllowed(allow=("GET", "HEAD")).headers == ((b"allow", b"GET, HEAD"),)
    assert MethodNotAllowed().headers == ()


@pytest.mark.asyncio
async def test_builtin_errors_use_problem_details() -> None:
    app = Wreath()

    @app.get("/forbidden")
    async def forbidden(request: Any) -> None:
        raise Forbidden("Access denied")

    async with TestClient(app) as client:
        missing = await client.get("/missing")
        denied = await client.get("/forbidden")

    assert missing.status == 404
    assert missing.header("content-type") == "application/problem+json"
    assert missing.json() == {
        "type": "about:blank",
        "title": "Not Found",
        "status": 404,
        "detail": "Not Found",
    }
    assert denied.status == 403
    assert denied.json()["detail"] == "Access denied"
