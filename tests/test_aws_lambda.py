from __future__ import annotations

import asyncio

import pytest

from wreath import Wreath
from wreath._asgi_state import ResponseCapture
from wreath.aws_lambda import LambdaAdapter, _response, _scope, _v2_headers
from wreath.response import JSONResponse, Response


@pytest.mark.asyncio
async def test_response_capture_owns_strict_and_replay_message_semantics() -> None:
    strict = ResponseCapture()
    await strict.send({"type": "http.response.start", "status": 201})
    await strict.send({"type": "http.response.body", "body": b"one", "more_body": True})
    await strict.send({"type": "http.response.body", "body": b"two"})
    strict.require_complete()
    assert (strict.status, strict.body) == (201, b"onetwo")
    with pytest.raises(RuntimeError, match="after the response ended"):
        await strict.send({"type": "http.response.body", "body": b"late"})

    replay = ResponseCapture(strict=False)
    await replay.send({"type": "http.response.body", "body": b"captured"})
    assert replay.body == b"captured"


def test_lambda_payload_v2_runs_one_lifespan_across_warm_invocations() -> None:
    app = Wreath()
    lifecycle: list[str] = []

    @app.on_startup
    async def startup(_app):
        lifecycle.append("start")

    @app.on_shutdown
    async def shutdown(_app):
        lifecycle.append("stop")

    @app.post("/hello")
    async def hello(request):
        return JSONResponse(
            {
                "query": request.query_string.decode(),
                "body": (await request.body()).decode(),
                "cookie": request.header("cookie"),
            }
        )

    event = {
        "version": "2.0",
        "rawPath": "/hello",
        "rawQueryString": "x=1",
        "headers": {"host": "api.example", "content-type": "text/plain"},
        "cookies": ["a=1", "b=2"],
        "requestContext": {"http": {"method": "POST", "path": "/hello", "sourceIp": "203.0.113.4"}},
        "body": "payload",
        "isBase64Encoded": False,
    }
    adapter = LambdaAdapter(app)
    first = adapter(event, object())
    second = adapter(event, object())
    adapter.close()
    assert first["statusCode"] == second["statusCode"] == 200
    assert first["isBase64Encoded"] is False
    assert '"cookie":"a=1; b=2"' in first["body"]
    assert lifecycle == ["start", "stop"]


def test_lambda_payload_v2_separates_decoded_and_raw_paths() -> None:
    app = Wreath()

    @app.get("/café")
    async def cafe(request):
        assert request.scope["path"] == "/café"
        assert request.scope["raw_path"] == b"/caf%C3%A9"
        return Response(b"ok", media_type=b"text/plain")

    event = {
        "version": "2.0",
        "rawPath": "/caf%C3%A9",
        "requestContext": {
            "http": {
                "method": "GET",
                "path": "/café",
                "sourceIp": "203.0.113.4",
            }
        },
    }
    with LambdaAdapter(app) as adapter:
        response = adapter(event, None)
    assert response["statusCode"] == 200
    assert response["body"] == "ok"


@pytest.mark.parametrize(
    ("raw_path", "http_path", "message"),
    [
        (7, "/", "rawPath must be a string"),
        ("/café", "/café", "non-ASCII bytes percent-encoded"),
        ("/caf%C3%A9", 7, "http path must be a string"),
        ("/%FF", None, "not valid percent-encoded UTF-8"),
    ],
)
def test_lambda_payload_v2_refuses_malformed_path_forms(raw_path, http_path, message) -> None:
    http = {"method": "GET"}
    if http_path is not None:
        http["path"] = http_path
    event = {
        "version": "2.0",
        "rawPath": raw_path,
        "requestContext": {"http": http},
    }
    adapter = LambdaAdapter(Wreath())
    try:
        with pytest.raises(ValueError, match=message):
            adapter(event, None)
    finally:
        adapter.close()


def test_lambda_payload_v1_preserves_multivalue_inputs_and_binary_outputs() -> None:
    app = Wreath()

    @app.get("/binary")
    async def binary(request):
        assert request.query_string == b"tag=a&tag=b"
        assert request.headers.count((b"x-item", b"one")) == 1
        return Response(
            b"\x00\xff",
            headers=[
                (b"content-type", b"application/octet-stream"),
                (b"set-cookie", b"a=1"),
                (b"set-cookie", b"b=2"),
            ],
        )

    event = {
        "httpMethod": "GET",
        "path": "/binary",
        "multiValueQueryStringParameters": {"tag": ["a", "b"]},
        "multiValueHeaders": {"host": ["api.example"], "x-item": ["one"]},
        "requestContext": {"identity": {"sourceIp": "203.0.113.4"}},
    }
    with LambdaAdapter(app) as adapter:
        response = adapter(event, None)
    assert response["statusCode"] == 200
    assert response["body"] == "AP8="
    assert response["isBase64Encoded"] is True
    assert response["multiValueHeaders"]["set-cookie"] == ["a=1", "b=2"]


@pytest.mark.parametrize("name", ["host", "x-forwarded-proto"])
def test_lambda_scope_refuses_ambiguous_authority_fields(name: str) -> None:
    event = {
        "httpMethod": "GET",
        "path": "/",
        "multiValueHeaders": {name: ["trusted.example", "attacker.example"]},
        "requestContext": {},
    }

    with pytest.raises(ValueError, match=name):
        _scope(event, None)


def test_lambda_refuses_unknown_payload_versions_and_malformed_events() -> None:
    app = Wreath()
    adapter = LambdaAdapter(app)
    with pytest.raises(ValueError, match="expected '1.0' or '2.0'"):
        adapter({"version": "3.0", "requestContext": {}}, None)
    with pytest.raises(ValueError, match="requestContext"):
        adapter({"version": "2.0"}, None)
    adapter.close()


def test_lambda_scope_refuses_each_malformed_v2_container() -> None:
    with pytest.raises(ValueError, match="http object"):
        _scope({"version": "2.0", "requestContext": {"http": None}}, None)
    with pytest.raises(ValueError, match="headers must be an object"):
        _v2_headers({"headers": []})
    for cookies in ({}, ["valid", 7]):
        with pytest.raises(ValueError, match="cookies must be a list of strings"):
            _v2_headers({"cookies": cookies})


def test_lambda_scope_preserves_body_client_and_platform_variants() -> None:
    base = {"httpMethod": "POST", "path": "/", "requestContext": {}}
    _version, anonymous, empty = _scope(base, "context")
    assert empty == b""
    assert anonymous["client"] is None
    assert anonymous["extensions"] == {
        "wreath.lambda": {"event": base, "context": "context"}
    }

    _version, malformed_identity, _body = _scope(
        {**base, "requestContext": {"identity": 7}}, None
    )
    assert malformed_identity["client"] is None

    event = {
        **base,
        "requestContext": {"identity": {"sourceIp": "203.0.113.9"}},
        "body": "aGVsbG8=",
        "isBase64Encoded": True,
        "_wreath_google": {"project": "demo"},
    }
    _version, populated, body = _scope(event, None)
    assert populated["client"] == ("203.0.113.9", None)
    assert populated["extensions"] == {"wreath.google": {"project": "demo"}}
    assert body == b"hello"

    with pytest.raises(ValueError, match="body must be a string or null"):
        _scope({**base, "body": 7}, None)
    with pytest.raises(ValueError, match="body is not valid base64"):
        _scope({**base, "body": "not-base64!", "isBase64Encoded": True}, None)


def test_lambda_v2_cookies_are_joined_only_when_present() -> None:
    assert _v2_headers({"cookies": []}) == []
    assert _v2_headers({"cookies": ["a=1", "b=2"]}) == [
        (b"cookie", b"a=1; b=2")
    ]


def test_lambda_response_classifies_content_and_cookie_headers_exactly() -> None:
    binary = _response("2.0", 200, ((b"x-format", b"text/plain"),), b"hello")
    assert binary["body"] == "aGVsbG8="
    assert binary["isBase64Encoded"] is True
    assert "cookies" not in binary

    cookies = _response(
        "2.0",
        200,
        (
            (b"content-type", b"text/plain"),
            (b"set-cookie", b"a=1"),
            (b"x-test", b"value"),
        ),
        b"hello",
    )
    assert cookies["body"] == "hello"
    assert cookies["cookies"] == ["a=1"]
    assert cookies["headers"] == {"content-type": "text/plain", "x-test": "value"}


def test_lambda_startup_failure_closes_the_warm_driver() -> None:
    app = Wreath()

    @app.on_startup
    async def fail(_app):
        raise RuntimeError("configuration failed")

    event = {
        "version": "2.0",
        "rawPath": "/",
        "requestContext": {"http": {"method": "GET"}},
    }
    adapter = LambdaAdapter(app)
    with pytest.raises(RuntimeError, match="lifespan startup failed"):
        adapter(event, None)
    with pytest.raises(RuntimeError, match="closed"):
        adapter(event, None)


def test_lambda_runs_an_asgi_app_that_does_not_support_lifespan() -> None:
    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            raise RuntimeError("lifespan unsupported")
        request = await receive()
        assert request["type"] == "http.request"
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    event = {
        "version": "2.0",
        "rawPath": "/",
        "requestContext": {"http": {"method": "GET"}},
    }
    with LambdaAdapter(app) as adapter:
        response = adapter(event, None)
    assert response["statusCode"] == 200
    assert response["body"] == "b2s="


def test_lambda_reports_disconnect_only_after_the_response_finishes() -> None:
    observations: list[object] = []

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                phase = message["type"].rsplit(".", 1)[1]
                await send({"type": f"lifespan.{phase}.complete"})
                if phase == "shutdown":
                    return
        first = await receive()
        pending_disconnect = asyncio.create_task(receive())
        await asyncio.sleep(0)
        observations.extend((first["type"], pending_disconnect.done()))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
        observations.append((await pending_disconnect)["type"])

    event = {
        "version": "2.0",
        "rawPath": "/",
        "requestContext": {"http": {"method": "GET"}},
    }
    with LambdaAdapter(app) as adapter:
        adapter(event, None)
    assert observations == ["http.request", False, "http.disconnect"]


def test_lambda_payload_v2_folds_header_names_case_insensitively() -> None:
    app = Wreath()

    @app.get("/")
    async def mixed_case_headers(_request):
        return Response(
            b"ok",
            headers=[
                (b"content-type", b"text/plain"),
                (b"X-Test", b"one"),
                (b"x-test", b"two"),
            ],
        )

    event = {
        "version": "2.0",
        "rawPath": "/",
        "requestContext": {"http": {"method": "GET"}},
    }
    with LambdaAdapter(app) as adapter:
        response = adapter(event, None)
    assert response["headers"]["X-Test"] == "one,two"
    assert "x-test" not in response["headers"]
