from __future__ import annotations

from typing import Annotated, Any

import pytest

import wreath.http_client as http_client_module
from wreath import Request, Router, Wreath
from wreath.binding import Body
from wreath.edge.proxy import _BODYLESS, IDEMPOTENT
from wreath.http_client import (
    ClientResponse,
    DestinationPolicy,
    HTTPClient,
    RedirectPolicy,
    RetryPolicy,
)
from wreath.openapi import generate_openapi
from wreath.router import _query_media_range
from wreath.testing import TestClient
from wreath.typegen import build_api_model, render_typescript


def _local_policy() -> DestinationPolicy:
    return DestinationPolicy(allow_private=True, allow_loopback=True)


async def test_query_decorators_and_test_client_require_a_supported_content_type() -> None:
    app = Wreath()

    @app.query("/search", accept_query=("application/json", "application/sql"))
    async def search(request: Request) -> dict[str, str]:
        return {"body": (await request.body()).decode()}

    router = Router(prefix="/v1")

    @router.query("/search", accept_query=("application/json",))
    async def routed(request: Request) -> dict[str, str]:
        return {"body": (await request.body()).decode()}

    app.include_router(router)

    async with TestClient(app) as client:
        accepted = await client.query("/search", json={"q": "wreath"})
        missing = await client.query("/search", content=b"select 1")
        unsupported = await client.query(
            "/search", content=b"x", headers={"content-type": "text/plain"}
        )
        included = await client.query("/v1/search", json={"q": "router"})

    assert accepted.status == included.status == 200
    assert accepted.json() == {"body": '{"q":"wreath"}'}
    assert accepted.header("accept-query") == '"application/json", "application/sql"'
    assert missing.status == 400
    assert unsupported.status == 415
    assert missing.header("accept-query") == '"application/json", "application/sql"'
    assert unsupported.header("accept-query") == '"application/json", "application/sql"'


async def test_accept_query_uses_structured_fields_for_media_parameters() -> None:
    app = Wreath()

    @app.query(
        "/search",
        accept_query=("application/jsonpath", 'application/sql;charset="UTF-8"'),
    )
    async def search(request: Request) -> str:
        return "ok"

    async with TestClient(app) as client:
        response = await client.query(
            "/search",
            content=b"$.store.book",
            headers={"content-type": "application/jsonpath"},
        )

    assert response.status == 200
    assert response.header("accept-query") == (
        '"application/jsonpath", application/sql;charset="UTF-8"'
    )


@pytest.mark.parametrize(
    ("accepted", "content_type"),
    [
        (("application/json",), "application/json"),
        (("application/*",), "application/json"),
        (("*/*",), "text/plain"),
    ],
)
async def test_accept_query_matches_exact_type_ranges_and_the_any_range(
    accepted: tuple[str, ...], content_type: str
) -> None:
    app = Wreath()

    @app.query("/search", accept_query=accepted)
    async def search(request: Request) -> str:
        return "ok"

    async with TestClient(app) as client:
        response = await client.query(
            "/search", content=b"query", headers={"content-type": content_type}
        )

    assert response.status == 200


@pytest.mark.parametrize(
    "media_range",
    ["json", "application/foo:bar", "application/json;format=foo/bar"],
)
def test_query_declaration_refuses_an_invalid_accept_query_media_range(
    media_range: str,
) -> None:
    app = Wreath()

    with pytest.raises(ValueError, match="invalid Accept-Query"):
        app.query("/search", accept_query=(media_range,))


@pytest.mark.parametrize(
    "media_range",
    [
        "application/json;format",
        "application/json;=compact",
        "application/json;format=",
    ],
)
def test_query_declaration_requires_complete_accept_query_parameters(
    media_range: str,
) -> None:
    app = Wreath()

    with pytest.raises(ValueError, match="parameters must be name=value"):
        app.query("/search", accept_query=(media_range,))


@pytest.mark.parametrize(
    "media_range",
    [
        "/json",
        "application/",
        "application/json/extra",
        "app@/json",
        "application/json@",
        "*/json",
    ],
)
def test_accept_query_refuses_each_malformed_type_or_subtype(media_range: str) -> None:
    with pytest.raises(ValueError, match="use type/subtype"):
        _query_media_range(media_range)


def test_accept_query_refuses_non_string_ranges() -> None:
    value: Any = 7
    with pytest.raises(TypeError, match="must be str, not int"):
        _query_media_range(value)


@pytest.mark.parametrize(
    "media_range",
    [
        "application/json;bad@=value",
        'application/json;format="',
        'application/json;format="unterminated',
        "application/json;format=hello world",
    ],
)
def test_accept_query_refuses_malformed_parameter_names_and_values(media_range: str) -> None:
    with pytest.raises(ValueError, match="invalid Accept-Query"):
        _query_media_range(media_range)


def test_accept_query_accepts_both_wildcard_shapes_and_parameter_forms() -> None:
    assert _query_media_range("*/*")[0] == "*/*"
    assert _query_media_range("application/*")[0] == "application/*"
    assert _query_media_range("application/json;format=compact")[0] == "application/json"
    assert _query_media_range('application/json;format="pretty print"')[0] == "application/json"


def test_openapi_uses_an_extension_for_query_and_keeps_its_request_body() -> None:
    app = Wreath()

    @app.query("/search")
    async def search(request: Request, body: Annotated[dict[str, str], Body()]) -> dict[str, str]:
        return body

    path = generate_openapi(app)["paths"]["/search"]

    assert "query" not in path
    operation = path["x-wreath-query"]
    assert operation["x-wreath-http-method"] == "QUERY"
    assert operation["requestBody"]["content"]["application/json"]
    assert (
        operation["responses"]["200"]["headers"]["Accept-Query"]["schema"]["const"]
        == '"application/json"'
    )
    assert {"400", "415"} <= operation["responses"].keys()


def test_typescript_treats_query_as_a_react_query_despite_its_request_body() -> None:
    app = Wreath()

    @app.query("/search", operation_id="search")
    async def search(request: Request, body: Annotated[dict[str, str], Body()]) -> dict[str, str]:
        return body

    source = render_typescript(build_api_model(app), react_query=True)["react-query.ts"]

    hook = source[source.index("export function useSearch") :]
    assert "return useQuery({" in hook
    assert "useMutation" not in hook
    assert 'queryKey: ["search", body] as const' in hook
    assert "queryFn: () => client.search(body)" in hook


@pytest.mark.asyncio
async def test_http_client_query_helper_sends_content_type_and_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, tuple[tuple[bytes, bytes], ...], bytes]] = []

    async def request(
        self: HTTPClient,
        method: str,
        target: str,
        *,
        headers: tuple[tuple[bytes, bytes], ...] = (),
        body: bytes | bytearray | memoryview = b"",
        idempotency_key: str | None = None,
    ) -> ClientResponse:
        calls.append((method, target, headers, bytes(body)))
        return ClientResponse(200, (), b"ok", "1.1")

    monkeypatch.setattr(HTTPClient, "request", request)
    client = HTTPClient("query", base_url="http://127.0.0.1", destination=_local_policy())

    response = await client.query("/search", body=b"select 1", content_type="application/sql")

    assert response.body == b"ok"
    assert calls == [("QUERY", "/search", ((b"content-type", b"application/sql"),), b"select 1")]


def test_http_client_query_refuses_a_non_media_content_type() -> None:
    client = HTTPClient("query", base_url="http://127.0.0.1", destination=_local_policy())

    with pytest.raises(ValueError, match="type/subtype"):
        client.query("/search", content_type="json")


@pytest.mark.asyncio
async def test_query_retries_and_preserves_method_and_body_across_301(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[str, bytes]] = []
    replies = iter(
        (
            ClientResponse(301, ((b"location", b"/moved"),), b"", "1.1"),
            ClientResponse(200, (), b"ok", "1.1"),
        )
    )

    async def send(
        self: HTTPClient,
        method: str,
        request: bytes,
        *,
        idempotency_key: str | None,
    ) -> ClientResponse:
        sent.append((method, request))
        return next(replies)

    monkeypatch.setattr(HTTPClient, "_send_with_retries", send)
    client = HTTPClient(
        "query-redirect",
        base_url="http://127.0.0.1",
        destination=_local_policy(),
        retry=RetryPolicy(attempts=2),
        redirect=RedirectPolicy(enabled=True, max_hops=4),
    )
    client._started = True

    response = await client._request_flow(
        "QUERY",
        "/search",
        headers=((b"content-type", b"application/sql"),),
        body=b"select 1",
        idempotency_key=None,
    )

    assert response.body == b"ok"
    assert [method for method, _request in sent] == ["QUERY", "QUERY"]
    assert sent[0][1].endswith(b"select 1") and sent[1][1].endswith(b"select 1")
    assert sent[1][1].startswith(b"QUERY /moved HTTP/1.1\r\n")
    assert "QUERY" in http_client_module._IDEMPOTENT


def test_edge_reads_query_content_and_can_retry_it() -> None:
    assert "QUERY" in IDEMPOTENT
    assert "QUERY" not in _BODYLESS
