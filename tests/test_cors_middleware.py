from __future__ import annotations

from typing import Any

import pytest

from wreath import Wreath
from wreath.policy import CorsPolicy, HttpPolicy
from wreath.request import Request
from wreath.testing import TestClient

pytestmark = pytest.mark.asyncio

ALLOWED = "https://app.example"


def _app(middleware: CorsPolicy) -> Wreath:
    app = Wreath(http_policy=HttpPolicy(cors=middleware))

    @app.get("/thing")
    async def thing(request) -> dict:
        return {"ok": True}

    return app


def _header(response: Any, name: str) -> str | None:
    return response.header(name)


def _headers(response: Any, name: str) -> list[bytes]:
    wanted = name.encode("ascii")
    return [value for key, value in response.headers if key.lower() == wanted]


async def test_an_options_request_with_both_headers_is_answered_as_a_preflight() -> None:
    app = _app(CorsPolicy(allow_origins=[ALLOWED]))
    async with TestClient(app) as client:
        response = await client.options(
            "/thing",
            headers={"origin": ALLOWED, "access-control-request-method": "GET"},
        )
    assert response.status == 204
    assert _header(response, "access-control-allow-origin") == ALLOWED
    assert _header(response, "access-control-max-age") is not None
    assert _header(response, "access-control-allow-credentials") is None
    assert _header(response, "access-control-expose-headers") is None


async def test_a_wildcard_origin_cannot_be_combined_with_credentials() -> None:
    with pytest.raises(ValueError, match="cannot be combined"):
        CorsPolicy(allow_origins=["*"], allow_credentials=True)


async def test_a_get_carrying_the_preflight_header_is_not_a_preflight() -> None:
    app = _app(CorsPolicy(allow_origins=[ALLOWED]))
    async with TestClient(app) as client:
        response = await client.get(
            "/thing",
            headers={"origin": ALLOWED, "access-control-request-method": "GET"},
        )
    assert response.status == 200
    assert response.json() == {"ok": True}  # the route ran


@pytest.mark.parametrize(
    "headers",
    [
        {"origin": ALLOWED},  # no requested method
        {"access-control-request-method": "GET"},  # no origin
        {},  # neither
    ],
)
async def test_an_options_request_missing_either_header_falls_through(
    headers: dict[str, str],
) -> None:
    app = _app(CorsPolicy(allow_origins=[ALLOWED]))
    async with TestClient(app) as client:
        response = await client.options("/thing", headers=headers)
    assert response.status == 405  # the route has no OPTIONS
    assert _header(response, "allow") == "GET, HEAD"
    assert _header(response, "access-control-max-age") is None


async def test_a_disallowed_origin_is_refused_at_the_preflight() -> None:
    app = _app(CorsPolicy(allow_origins=[ALLOWED]))
    async with TestClient(app) as client:
        response = await client.options(
            "/thing",
            headers={"origin": "https://evil.example", "access-control-request-method": "GET"},
        )
    assert response.status == 403
    assert b"disallowed origin" in response.body
    assert _header(response, "access-control-allow-origin") is None
    # The refusal itself depends on the origin, so a shared cache must not
    # replay it to one that would have been allowed.
    assert b"origin" in b",".join(_headers(response, "vary")).lower()


async def test_a_disallowed_method_is_refused_rather_than_answered_with_the_list() -> None:
    app = _app(CorsPolicy(allow_origins=[ALLOWED], allow_methods=["GET"]))
    async with TestClient(app) as client:
        response = await client.options(
            "/thing",
            headers={"origin": ALLOWED, "access-control-request-method": "DELETE"},
        )
    assert response.status == 403
    assert b"disallowed method" in response.body
    assert b"origin" in b",".join(_headers(response, "vary")).lower()


async def test_an_origin_matches_case_insensitively_on_scheme_and_host() -> None:
    app = _app(CorsPolicy(allow_origins=[ALLOWED]))
    async with TestClient(app) as client:
        response = await client.options(
            "/thing",
            headers={"origin": "HTTPS://App.Example", "access-control-request-method": "GET"},
        )
    assert response.status == 204
    # Echoed exactly as sent -- that is the string the browser compares against.
    assert _header(response, "access-control-allow-origin") == "HTTPS://App.Example"


async def test_a_disallowed_origin_gets_vary_and_nothing_else_on_a_simple_request() -> None:
    app = _app(CorsPolicy(allow_origins=[ALLOWED]))
    async with TestClient(app) as client:
        response = await client.get("/thing", headers={"origin": "https://evil.example"})
    assert response.status == 200  # the route still ran
    assert _header(response, "access-control-allow-origin") is None
    assert b"origin" in b",".join(_headers(response, "vary")).lower()


async def test_a_request_with_no_origin_is_left_exactly_as_it_was() -> None:
    app = _app(CorsPolicy(allow_origins=[ALLOWED]))
    async with TestClient(app) as client:
        response = await client.get("/thing")
    assert _header(response, "access-control-allow-origin") is None
    assert _headers(response, "vary") == []


@pytest.mark.parametrize("origin", [ALLOWED, "https://evil.example"])
async def test_a_headerless_response_is_left_alone_for_any_origin(origin: str) -> None:
    middleware = CorsPolicy(allow_origins=[ALLOWED])
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/thing",
            "query_string": b"",
            "headers": [(b"origin", origin.encode("ascii"))],
        },
        None,
        None,
    )

    middleware._egress_inplace(request, object())


async def test_a_named_origin_response_varies_on_origin() -> None:
    app = _app(CorsPolicy(allow_origins=[ALLOWED, "https://other.example"]))
    async with TestClient(app) as client:
        preflight = await client.options(
            "/thing",
            headers={"origin": ALLOWED, "access-control-request-method": "GET"},
        )
        simple = await client.get("/thing", headers={"origin": ALLOWED})
    for response in (preflight, simple):
        assert b"origin" in b",".join(_headers(response, "vary")).lower()
    assert _header(simple, "access-control-expose-headers") is None
    assert _header(simple, "access-control-allow-credentials") is None


async def test_a_pure_wildcard_response_does_not_need_to_vary() -> None:
    app = _app(CorsPolicy(allow_origins=["*"]))
    async with TestClient(app) as client:
        preflight = await client.options(
            "/thing",
            headers={"origin": ALLOWED, "access-control-request-method": "GET"},
        )
        simple = await client.get("/thing", headers={"origin": ALLOWED})
    assert _header(preflight, "access-control-allow-origin") == "*"
    assert _header(simple, "access-control-allow-origin") == "*"
    for response in (preflight, simple):
        assert b"origin" not in b",".join(_headers(response, "vary")).lower()


async def test_a_handlers_own_cors_header_is_honoured_rather_than_duplicated() -> None:
    app = Wreath(http_policy=HttpPolicy(cors=CorsPolicy(allow_origins=[ALLOWED])))

    @app.get("/own")
    async def own(request) -> Any:
        from wreath.response import JSONResponse

        response = JSONResponse({"ok": True})
        response.headers.append((b"access-control-allow-origin", b"https://custom.example"))
        return response

    async with TestClient(app) as client:
        response = await client.get("/own", headers={"origin": ALLOWED})

    values = _headers(response, "access-control-allow-origin")
    assert values == [b"https://custom.example"]


async def test_expose_headers_and_credentials_reach_a_simple_response() -> None:
    app = _app(
        CorsPolicy(
            allow_origins=[ALLOWED],
            allow_credentials=True,
            expose_headers=["x-total"],
        )
    )
    async with TestClient(app) as client:
        response = await client.get("/thing", headers={"origin": ALLOWED})
    assert _header(response, "access-control-allow-credentials") == "true"
    assert _header(response, "access-control-expose-headers") == "x-total"


async def test_allow_headers_reach_a_preflight_and_are_absent_when_unset() -> None:
    app = _app(CorsPolicy(allow_origins=[ALLOWED], allow_headers=["x-token"]))
    async with TestClient(app) as client:
        with_headers = await client.options(
            "/thing",
            headers={"origin": ALLOWED, "access-control-request-method": "GET"},
        )
    assert _header(with_headers, "access-control-allow-headers") == "x-token"

    bare = _app(CorsPolicy(allow_origins=[ALLOWED]))
    async with TestClient(bare) as client:
        without = await client.options(
            "/thing",
            headers={"origin": ALLOWED, "access-control-request-method": "GET"},
        )
    assert _header(without, "access-control-allow-headers") is None
