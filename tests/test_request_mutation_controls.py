from __future__ import annotations

import hashlib
import tempfile
from typing import Any

import pytest

import wreath.request as request_module
from wreath.exceptions import BadRequest, ClientDisconnect, PayloadTooLarge
from wreath.request import Request, RequestLimits, StreamConsumed, _multipart_boundary
from wreath.state import BODY_CHECK_SLOT


async def _no_receive() -> dict[str, Any]:
    raise AssertionError("the receive channel must not be read")


def _messages(*messages: object) -> Any:
    pending = iter(messages)

    async def receive() -> object:
        return next(pending)

    return receive


class _App:
    def __init__(self, host: str | None = None) -> None:
        self.host = host

    def _host_for(self, _name: str, _parameters: dict[str, Any]) -> str | None:
        return self.host

    def url_path_for(self, name: str, **parameters: Any) -> str:
        assert name == "item"
        return f"/items/{parameters['item_id']}"


class _NativeContext:
    method = "PATCH"
    path = "/native"
    scheme = "https"
    client: tuple[str, int | None] = ("127.0.0.1", 4040)
    query_string = b"native=1"
    headers = [(b"host", b"native.example")]

    def __init__(self) -> None:
        self.scope_calls = 0
        self.cookie_calls = 0
        self.bearer_calls = 0
        self.single_calls = 0

    def _asgi_scope(self) -> dict[str, Any]:
        self.scope_calls += 1
        return {
            "type": "http",
            "method": self.method,
            "path": self.path,
            "scheme": self.scheme,
            "client": self.client,
            "query_string": self.query_string,
            "headers": self.headers,
        }

    def _parse_cookies(self, _limit: int, _error: type[Exception]) -> dict[str, str]:
        self.cookie_calls += 1
        return {"native": "yes"}

    def _bearer_verify(self, verifier: Any) -> Any:
        self.bearer_calls += 1
        return verifier("native-token")

    def _single_header(self, name: bytes) -> bytes | None:
        self.single_calls += 1
        return b"native-value" if name == b"x-one" else None

    def _bearer_token(self) -> str:
        return "native-token"

    def _set_client(self, client: tuple[str, int | None]) -> None:
        self.client = client

    def _set_scheme(self, scheme: str) -> None:
        self.scheme = scheme


def _multipart_request(body: bytes, *, limits: RequestLimits | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "headers": [(b"content-type", b"multipart/form-data; boundary=B")],
        },
        _messages({"type": "http.request", "body": body, "more_body": False}),
        limits=limits or RequestLimits(),
    )


def test_boundary_fragment_without_equals_does_not_hide_a_later_boundary() -> None:
    content_type = b"multipart/form-data; boundary; charset=utf-8; boundary=usable"

    assert _multipart_boundary(content_type) == b"usable"


@pytest.mark.asyncio
async def test_multipart_refuses_a_part_without_a_form_field_name() -> None:
    request = _multipart_request(b"--B\r\nContent-Disposition: form-data\r\n\r\nvalue\r\n--B--\r\n")

    with pytest.raises(ValueError, match="needs a non-empty form-data name"):
        await request.form()


@pytest.mark.asyncio
async def test_large_plain_field_is_not_misclassified_as_a_spooled_upload() -> None:
    value = b"x" * 65
    request = _multipart_request(
        b'--B\r\nContent-Disposition: form-data; name="plain"\r\n\r\n' + value + b"\r\n--B--\r\n",
        limits=RequestLimits(spool_max_bytes=64, max_form_memory_bytes=128),
    )

    form = await request.form()

    assert form["plain"] == value.decode()
    assert form.files == {}


@pytest.mark.asyncio
async def test_duplicate_spooled_file_is_closed_while_the_first_stays_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spools: list[Any] = []

    def open_spool() -> Any:
        spool = tempfile.TemporaryFile()
        spools.append(spool)
        return spool

    monkeypatch.setattr(request_module, "TemporaryFile", open_spool)
    part = (
        b'Content-Disposition: form-data; name="upload"; filename="item.bin"'
        b"\r\n\r\n" + b"x" * 65 + b"\r\n"
    )
    request = _multipart_request(
        b"--B\r\n" + part + b"--B\r\n" + part + b"--B--\r\n",
        limits=RequestLimits(spool_max_bytes=64),
    )

    form = await request.form()

    assert len(spools) == 2
    assert spools[0].closed is False
    assert spools[1].closed is True
    assert form.files["upload"].read() == b"x" * 65
    form.close()


@pytest.mark.asyncio
async def test_unterminated_spooled_part_is_closed_on_parser_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spools: list[Any] = []

    def open_spool() -> Any:
        spool = tempfile.TemporaryFile()
        spools.append(spool)
        return spool

    monkeypatch.setattr(request_module, "TemporaryFile", open_spool)
    request = _multipart_request(
        b'--B\r\nContent-Disposition: form-data; name="upload"; filename="item.bin"'
        b"\r\n\r\n" + b"x" * 256,
        limits=RequestLimits(spool_max_bytes=32),
    )

    with pytest.raises(ValueError):
        await request.form()

    assert len(spools) == 1
    assert spools[0].closed is True


def test_native_scope_is_materialized_once_and_cached_on_the_request() -> None:
    context = _NativeContext()
    request = Request(context, _no_receive)

    first = request.scope

    assert first["path"] == "/native"
    assert request.scope is first
    assert context.scope_calls == 1


def test_route_outcome_moves_into_state_on_its_first_read() -> None:
    request = Request({"type": "http"}, _no_receive)
    request._set_route_outcome("miss")

    assert request._state is None
    assert request.state.route_outcome == "miss"


def test_state_without_a_route_outcome_does_not_invent_one() -> None:
    request = Request({"type": "http"}, _no_receive)

    state = request.state

    with pytest.raises(RuntimeError, match="route_outcome"):
        state.require("route_outcome")


def test_native_bearer_verifier_uses_the_context_boundary() -> None:
    context = _NativeContext()
    request = Request(context, _no_receive)

    result = request._bearer_verify(lambda token: f"verified:{token}")

    assert result == "verified:native-token"
    assert context.bearer_calls == 1


def test_native_bearer_token_uses_the_context_boundary() -> None:
    context = _NativeContext()
    request = Request(context, _no_receive)

    assert request._bearer_token() == "native-token"
    assert request._scope is None


def test_portable_bearer_verifier_is_not_called_without_a_token() -> None:
    request = Request({"type": "http", "headers": []}, _no_receive)

    def verifier(_token: str) -> None:
        raise AssertionError("a missing bearer token has nothing to verify")

    assert request._bearer_verify(verifier) is None


def test_native_cookie_parser_is_used_once_without_materializing_headers() -> None:
    context = _NativeContext()
    request = Request(context, _no_receive)

    first = request.cookies

    assert first == {"native": "yes"}
    assert request.cookies is first
    assert context.cookie_calls == 1
    assert request._scope is None


def test_native_single_header_lookup_uses_the_duplicate_aware_context_method() -> None:
    context = _NativeContext()
    request = Request(context, _no_receive)

    assert request._single_header(b"x-one") == b"native-value"
    assert request._single_header(b"missing") is None
    assert context.single_calls == 2
    assert request._scope is None


def test_native_scalar_properties_do_not_materialize_an_asgi_scope() -> None:
    context = _NativeContext()
    request = Request(context, _no_receive)

    assert request.method == "PATCH"
    assert request.path == "/native"
    assert request.client == ("127.0.0.1", 4040)
    assert request.scheme == "https"
    assert request.query_string == b"native=1"
    assert request.headers == [(b"host", b"native.example")]
    assert context.scope_calls == 0


def test_native_proxy_rewrites_update_context_without_materializing_scope() -> None:
    context = _NativeContext()
    request = Request(context, _no_receive)

    request._set_client(("203.0.113.7", None), source="forwarded")
    request._set_scheme("https")

    assert request.client == ("203.0.113.7", None)
    assert request.client_source == "forwarded"
    assert request.scheme == "https"
    assert context.scope_calls == 0


def test_unbacked_client_has_no_portable_fallback() -> None:
    request = Request(None, _no_receive)

    with pytest.raises(RuntimeError, match="scope is unavailable"):
        _ = request.client


def test_portable_client_and_proxy_rewrites_share_the_asgi_scope() -> None:
    scope = {
        "type": "http",
        "client": ("127.0.0.1", 4040),
        "scheme": "http",
    }
    request = Request(scope, _no_receive)

    assert request.client == ("127.0.0.1", 4040)
    request._set_client(("203.0.113.9", None), source="forwarded")
    request._set_scheme("https")

    assert request.client == ("203.0.113.9", None)
    assert request.client_source == "forwarded"
    assert request.scheme == "https"
    assert scope["client"] == ("203.0.113.9", None)
    assert scope["scheme"] == "https"


def test_url_path_for_applies_the_asgi_root_path() -> None:
    request = Request(
        {"type": "http", "root_path": "/mounted/", "headers": []},
        _no_receive,
        app=_App(),
    )

    assert request.url_path_for("item", item_id=7) == "/mounted/items/7"


def test_host_specific_reverse_url_wins_over_the_request_host() -> None:
    request = Request(
        {
            "type": "http",
            "scheme": "https",
            "headers": [(b"host", b"request.example")],
        },
        _no_receive,
        app=_App("route.example"),
    )

    assert request.url_for("item", item_id=7) == "https://route.example/items/7"


def test_reverse_url_uses_the_request_host_when_route_has_no_host() -> None:
    request = Request(
        {
            "type": "http",
            "scheme": "http",
            "headers": [(b"host", b"request.example:8080")],
            "server": ("ignored", 80),
        },
        _no_receive,
        app=_App(),
    )

    assert request.url_for("item", item_id=3) == "http://request.example:8080/items/3"


def test_reverse_url_omits_each_schemes_default_server_port() -> None:
    http = Request(
        {"type": "http", "scheme": "http", "headers": [], "server": ("host", 80)},
        _no_receive,
        app=_App(),
    )
    https = Request(
        {
            "type": "http",
            "scheme": "https",
            "headers": [],
            "server": ("secure", 443),
        },
        _no_receive,
        app=_App(),
    )

    assert http.url_for("item", item_id=1) == "http://host/items/1"
    assert https.url_for("item", item_id=2) == "https://secure/items/2"


def test_reverse_url_keeps_a_nondefault_server_port() -> None:
    request = Request(
        {
            "type": "http",
            "scheme": "https",
            "headers": [],
            "server": ("secure", 8443),
        },
        _no_receive,
        app=_App(),
    )

    assert request.url_for("item", item_id=2) == "https://secure:8443/items/2"


def test_absent_body_check_does_not_require_request_state() -> None:
    request = Request({"type": "http"}, _no_receive)

    assert request._state is None
    assert request._take_body_check() is None
    assert request._state is None


def test_absent_body_check_does_not_write_a_placeholder_into_existing_state() -> None:
    request = Request({"type": "http"}, _no_receive)
    state = request.state

    assert request._take_body_check() is None
    assert BODY_CHECK_SLOT not in state._values


def test_body_check_is_spent_once_and_refuses_different_bytes() -> None:
    request = Request({"type": "http"}, _no_receive)
    request.state.__setattr__(BODY_CHECK_SLOT, ("sha-256", hashlib.sha256(b"honest").digest()))

    with pytest.raises(BadRequest, match="does not match"):
        request._check_body(b"forged")

    assert request.state.get(BODY_CHECK_SLOT) is None
    request._check_body(b"forged")


def test_body_check_accepts_matching_bytes() -> None:
    request = Request({"type": "http"}, _no_receive)
    request.state.__setattr__(BODY_CHECK_SLOT, ("sha-256", hashlib.sha256(b"honest").digest()))

    request._check_body(b"honest")

    assert request.state.get(BODY_CHECK_SLOT) is None


@pytest.mark.asyncio
async def test_completed_stream_cannot_be_replayed_a_second_time() -> None:
    request = Request(
        {"type": "http"},
        _messages({"type": "http.request", "body": b"once", "more_body": False}),
    )

    assert [chunk async for chunk in request.stream()] == [b"once"]
    with pytest.raises(StreamConsumed):
        _ = [chunk async for chunk in request.stream()]


@pytest.mark.asyncio
async def test_cached_empty_body_replays_as_no_stream_chunks() -> None:
    request = Request(
        {"type": "http"},
        _messages({"type": "http.request", "body": b"", "more_body": False}),
    )

    assert await request.body() == b""
    assert [chunk async for chunk in request.stream()] == []


@pytest.mark.asyncio
async def test_tuple_stream_checks_disconnect_before_yielding_body() -> None:
    request = Request({"type": "http"}, _messages((b"ignored", False, True)))

    with pytest.raises(ClientDisconnect):
        _ = [chunk async for chunk in request.stream()]

    assert await request.body() == b""


@pytest.mark.asyncio
async def test_tuple_stream_does_not_yield_an_empty_transport_chunk() -> None:
    request = Request({"type": "http"}, _messages((b"", False, False)))

    assert [chunk async for chunk in request.stream()] == []


@pytest.mark.asyncio
async def test_tuple_stream_enforces_the_body_limit_across_chunks() -> None:
    request = Request(
        {"type": "http"},
        _messages((b"1234", True, False), (b"5678", False, False)),
        limits=RequestLimits(max_body_bytes=7),
    )

    with pytest.raises(PayloadTooLarge, match="exceeds 7 bytes"):
        _ = [chunk async for chunk in request.stream()]


@pytest.mark.asyncio
async def test_tuple_stream_checks_the_deferred_signed_digest() -> None:
    request = Request(
        {"type": "http"},
        _messages((b"forged", False, False)),
    )
    request.state.__setattr__(BODY_CHECK_SLOT, ("sha-256", hashlib.sha256(b"honest").digest()))

    with pytest.raises(BadRequest, match="does not match"):
        _ = [chunk async for chunk in request.stream()]


@pytest.mark.asyncio
async def test_tuple_stream_accepts_a_matching_deferred_digest() -> None:
    request = Request(
        {"type": "http"},
        _messages((b"hon", True, False), (b"est", False, False)),
    )
    request.state.__setattr__(BODY_CHECK_SLOT, ("sha-256", hashlib.sha256(b"honest").digest()))

    assert [chunk async for chunk in request.stream()] == [b"hon", b"est"]


@pytest.mark.asyncio
async def test_mapping_stream_checks_the_deferred_signed_digest() -> None:
    request = Request(
        {"type": "http"},
        _messages({"type": "http.request", "body": b"forged", "more_body": False}),
    )
    request.state.__setattr__(BODY_CHECK_SLOT, ("sha-256", hashlib.sha256(b"honest").digest()))

    with pytest.raises(BadRequest, match="does not match"):
        _ = [chunk async for chunk in request.stream()]


@pytest.mark.asyncio
async def test_mapping_stream_accepts_a_matching_deferred_digest() -> None:
    request = Request(
        {"type": "http"},
        _messages(
            {"type": "http.request", "body": b"hon", "more_body": True},
            {"type": "http.request", "body": b"est", "more_body": False},
        ),
    )
    request.state.__setattr__(BODY_CHECK_SLOT, ("sha-256", hashlib.sha256(b"honest").digest()))

    assert [chunk async for chunk in request.stream()] == [b"hon", b"est"]


@pytest.mark.asyncio
async def test_form_without_content_type_is_refused() -> None:
    request = Request(
        {"type": "http", "headers": []},
        _messages({"type": "http.request", "body": b"name=wreath", "more_body": False}),
    )

    with pytest.raises(ValueError, match=r"form\(\) requires"):
        await request.form()
