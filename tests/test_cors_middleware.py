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


async def test_an_opaque_origin_cannot_receive_credentials() -> None:
    with pytest.raises(ValueError, match=r"invalid CORS origin.*null"):
        CorsPolicy(allow_origins=["null"], allow_credentials=True)


async def test_an_opaque_origin_remains_available_without_credentials() -> None:
    app = _app(CorsPolicy(allow_origins=["null"]))

    async with TestClient(app) as client:
        response = await client.get("/", headers={"origin": "null"})

    assert response.header("access-control-allow-origin") == "null"
    assert response.header("access-control-allow-credentials") is None


async def test_a_get_carrying_the_preflight_header_is_not_a_preflight() -> None:
    app = _app(CorsPolicy(allow_origins=[ALLOWED]))
    async with TestClient(app) as client:
        response = await client.get(
            "/thing",
            headers={"origin": ALLOWED, "access-control-request-method": "GET"},
        )
    assert response.status == 200
    assert response.json() == {"ok": True}  # the route ran


async def test_an_options_request_missing_either_header_falls_through() -> None:
    app = _app(CorsPolicy(allow_origins=[ALLOWED]))
    async with TestClient(app) as client:
        responses = [
            await client.options("/thing", headers={"origin": ALLOWED}),
            await client.options(
                "/thing", headers={"access-control-request-method": "GET"}
            ),
            await client.options("/thing"),
        ]
    for response in responses:
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


async def test_an_exact_origin_match_avoids_normalization(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = CorsPolicy(allow_origins=[ALLOWED])

    def fail_normalization(value: str) -> str:
        pytest.fail(f"normalized exact origin {value}")

    monkeypatch.setattr("wreath.policy.cors._normalize_origin", fail_normalization)

    assert policy._origin_header(ALLOWED) == (
        b"access-control-allow-origin",
        ALLOWED.encode("ascii"),
    )


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

    async def receive() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/thing",
            "query_string": b"",
            "headers": [(b"origin", origin.encode("ascii"))],
        },
        receive,
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


async def test_method_generators_are_compiled_once() -> None:
    methods = (method for method in ("GET", "POST"))
    app = _app(CorsPolicy(allow_origins=[ALLOWED], allow_methods=methods))

    async with TestClient(app) as client:
        response = await client.options(
            "/thing",
            headers={"origin": ALLOWED, "access-control-request-method": "POST"},
        )

    assert response.status == 204
    assert _header(response, "access-control-allow-methods") == "GET, POST"


@pytest.mark.parametrize(
    ("argument", "value", "label"),
    [
        ("allow_methods", ["GET\r\nx-injected: yes"], "allow_methods"),
        ("allow_methods", ["GET, POST"], "allow_methods"),
        ("allow_headers", ["x-token\r\nx-injected: yes"], "allow_headers"),
        ("allow_headers", ["x-token, x-other"], "allow_headers"),
        ("expose_headers", ["x-result\r\nx-injected: yes"], "expose_headers"),
        ("expose_headers", [None], "expose_headers"),
    ],
)
async def test_serialized_cors_names_must_be_single_http_tokens(
    argument: str, value: list[str], label: str
) -> None:
    with pytest.raises(ValueError, match=rf"{label}.*HTTP token"):
        CorsPolicy(allow_origins=[ALLOWED], **{argument: value})


@pytest.mark.parametrize("allow_credentials", [0, 1, "yes", None])
async def test_allow_credentials_must_be_an_exact_bool(allow_credentials: Any) -> None:
    with pytest.raises(TypeError, match="allow_credentials must be bool"):
        CorsPolicy(allow_origins=[ALLOWED], allow_credentials=allow_credentials)


@pytest.mark.parametrize("max_age", [True, False, -1, 1.5, "600", None])
async def test_max_age_must_be_a_non_negative_integer(max_age: Any) -> None:
    with pytest.raises(TypeError, match="max_age must be a non-negative int"):
        CorsPolicy(allow_origins=[ALLOWED], max_age=max_age)


@pytest.mark.parametrize(
    "origin",
    [
        "https://good.example\\evil.example",
        "https://b\N{LATIN SMALL LETTER U WITH DIAERESIS}cher.example",
    ],
)
async def test_allowed_origins_refuse_non_browser_serializations(origin: str) -> None:
    with pytest.raises(ValueError, match=r"invalid CORS origin.*ASCII browser origin"):
        CorsPolicy(allow_origins=[origin])


async def test_allowed_origin_entries_must_be_strings() -> None:
    with pytest.raises(TypeError, match="allow_origins entries must be str"):
        CorsPolicy(allow_origins=[None])


@pytest.mark.parametrize(
    "requested",
    ["x-other", "x-token, x-other", "x-token,,x-other"],
)
async def test_preflight_refuses_disallowed_or_malformed_requested_headers(
    requested: str,
) -> None:
    app = _app(CorsPolicy(allow_origins=[ALLOWED], allow_headers=["x-token"]))

    async with TestClient(app) as client:
        response = await client.options(
            "/thing",
            headers={
                "origin": ALLOWED,
                "access-control-request-method": "GET",
                "access-control-request-headers": requested,
            },
        )

    assert response.status == 403
    assert response.body == b"disallowed header"
    assert b"origin" in b",".join(_headers(response, "vary")).lower()


async def test_preflight_requested_headers_match_case_insensitively() -> None:
    app = _app(
        CorsPolicy(allow_origins=[ALLOWED], allow_headers=["X-Token", "x-other"])
    )

    async with TestClient(app) as client:
        response = await client.options(
            "/thing",
            headers={
                "origin": ALLOWED,
                "access-control-request-method": "GET",
                "access-control-request-headers": "x-token, X-Other",
            },
        )

    assert response.status == 204


async def test_wildcard_allow_headers_still_requires_header_name_grammar() -> None:
    app = _app(CorsPolicy(allow_origins=[ALLOWED], allow_headers=["*"]))

    async with TestClient(app) as client:
        malformed = await client.options(
            "/thing",
            headers={
                "origin": ALLOWED,
                "access-control-request-method": "GET",
                "access-control-request-headers": "x-token\tx-other",
            },
        )
        allowed = await client.options(
            "/thing",
            headers={
                "origin": ALLOWED,
                "access-control-request-method": "GET",
                "access-control-request-headers": "x-arbitrary",
            },
        )

    assert malformed.status == 403
    assert allowed.status == 204


@pytest.mark.parametrize(
    "duplicated",
    ["origin", "access-control-request-method", "access-control-request-headers"],
)
async def test_preflight_refuses_duplicate_singleton_cors_headers(duplicated: str) -> None:
    headers = [
        ("origin", ALLOWED),
        ("access-control-request-method", "GET"),
        ("access-control-request-headers", "x-token"),
        (
            duplicated,
            ALLOWED
            if duplicated == "origin"
            else "x-token"
            if duplicated == "access-control-request-headers"
            else "GET",
        ),
    ]
    app = _app(CorsPolicy(allow_origins=[ALLOWED], allow_headers=["x-token"]))

    async with TestClient(app) as client:
        response = await client.options("/thing", headers=headers)

    assert response.status == 400
    assert response.body == b"duplicate CORS header"
    assert _header(response, "access-control-allow-origin") is None


async def test_simple_request_with_duplicate_origin_is_never_authorized() -> None:
    app = _app(CorsPolicy(allow_origins=[ALLOWED], allow_credentials=True))

    async with TestClient(app) as client:
        response = await client.get(
            "/thing",
            headers=[("origin", ALLOWED), ("origin", "https://evil.example")],
        )

    assert response.status == 200
    assert _header(response, "access-control-allow-origin") is None
    assert _header(response, "access-control-allow-credentials") is None
    assert b"origin" in b",".join(_headers(response, "vary")).lower()


async def test_duplicate_origin_on_headerless_response_is_safe() -> None:
    policy = CorsPolicy(allow_origins=[ALLOWED], allow_credentials=True)

    class HeaderlessRequest:
        method = "GET"

        def header(self, name: str) -> str | None:
            return ALLOWED if name == "origin" else None

        def _single_header(self, name: bytes) -> bytes | None:
            raise ValueError("duplicate")

    policy._egress_inplace(HeaderlessRequest(), object())


async def test_direct_preflight_stub_needs_no_request_state() -> None:
    policy = CorsPolicy(allow_origins=[ALLOWED], allow_methods=["GET"])

    class BareRequest:
        method = "OPTIONS"

        def header(self, name: str) -> str | None:
            return {
                "origin": ALLOWED,
                "access-control-request-method": "DELETE",
            }.get(name)

    request: Any = BareRequest()
    response = policy._ingress_sync(request)

    assert response is not None
    assert response.status == 403
