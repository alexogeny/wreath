from __future__ import annotations

import os
from typing import Any, cast

import pytest

from wreath._headers import validate_response_headers
from wreath.response import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PreparedResponse,
    ProblemDetail,
    ProblemResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
    TextResponse,
    _disposition,
    _HeaderResponse,
)


@pytest.mark.asyncio
async def test_file_response_prefers_native_descriptor_path(tmp_path) -> None:
    path = tmp_path / "asset.bin"
    path.write_bytes(b"native-file")

    class Protocol:
        def __init__(self) -> None:
            self.started = False
            self.finished = False
            self.payload = b""

        async def _asgi_send(self, message) -> None:
            raise AssertionError("native file response used generic ASGI send")

        async def _wreath_file_start(self, status, headers, file, size) -> None:
            self.started = status == 200 and (b"content-length", b"11") in headers
            assert size == 11
            self.payload = file.read()

        async def _wreath_file_finish(self) -> None:
            self.finished = True

    protocol = Protocol()
    await FileResponse(path)(protocol._asgi_send)

    assert protocol.started and protocol.finished
    assert protocol.payload == b"native-file"


@pytest.mark.asyncio
async def test_file_reader_errors_are_relayed_to_the_sender(monkeypatch, tmp_path) -> None:
    import wreath.response as response_module

    path = tmp_path / "asset.bin"
    path.write_bytes(b"broken")
    descriptor = os.open(path, os.O_RDONLY)

    def fail_read(fd: int, size: int) -> bytes:
        raise OSError("reader failed")

    async def send(message) -> None:
        if message["type"] == "http.response.body":
            assert isinstance(message["body"], bytes)

    monkeypatch.setattr(response_module.os, "read", fail_read)
    with pytest.raises(OSError, match="reader failed"):
        await response_module._send_from_descriptor(
            descriptor,
            path.stat().st_size,
            200,
            [],
            send,
        )


def test_default_headers() -> None:
    response = Response(b"abc")
    assert response.status == 200
    assert response.headers == [
        (b"content-type", b"application/octet-stream"),
        (b"content-length", b"3"),
    ]


@pytest.mark.parametrize("status", [True, False, 99, 600, "200"])
def test_response_refuses_invalid_status_at_construction(status: Any) -> None:
    with pytest.raises((TypeError, ValueError), match="status"):
        Response(status=status)


def test_response_refuses_non_bytes_body_at_construction() -> None:
    with pytest.raises(TypeError, match="body must be bytes"):
        Response(cast(Any, bytearray(b"mutable")))


@pytest.mark.parametrize(
    "headers",
    [
        [(b"x evil", b"value")],
        [(b"x:evil", b"value")],
        [(b"", b"value")],
        [(b"x-test", b"safe\r\nx-evil: injected")],
        [(b"x-test", b"safe\x00evil")],
        [("x-test", b"value")],
        [(b"x-test", "value")],
    ],
)
def test_response_refuses_invalid_wire_headers_at_construction(headers: Any) -> None:
    with pytest.raises((TypeError, ValueError), match="header"):
        Response(headers=headers)


@pytest.mark.parametrize("name", [b"content-length", b"content-type"])
def test_response_refuses_duplicate_singleton_headers(name: bytes) -> None:
    with pytest.raises(ValueError, match=name.decode("ascii")):
        Response(headers=[(name, b"0"), (name.upper(), b"0")])


@pytest.mark.parametrize("value", [b"", b"+3", b"3, 3", b"three", b"4"])
def test_response_refuses_ambiguous_content_length(value: bytes) -> None:
    with pytest.raises(ValueError, match="content-length"):
        Response(b"abc", headers=[(b"content-length", value)])


def test_response_refuses_invalid_media_type_with_caller_headers() -> None:
    with pytest.raises(TypeError, match="media_type must be bytes"):
        Response(headers=[(b"x-test", b"value")], media_type=cast(Any, "text/plain"))


@pytest.mark.parametrize("media_type", [bytearray(b"text/plain"), 1, b"text/plain\r\nx: y"])
def test_response_refuses_invalid_generated_media_type(media_type: Any) -> None:
    with pytest.raises((TypeError, ValueError), match="media_type"):
        Response(media_type=media_type)


@pytest.mark.parametrize("status", [200, 299, 400, True])
def test_redirect_refuses_non_redirect_status(status: Any) -> None:
    error = TypeError if type(status) is not int else ValueError
    with pytest.raises(error, match="redirect status"):
        RedirectResponse("/next", status=status)


def test_redirect_accepts_the_full_redirect_status_range() -> None:
    assert RedirectResponse("/next", status=300).status == 300
    assert RedirectResponse("/next", status=399).status == 399


def test_response_header_validator_preserves_valid_field_octets() -> None:
    assert validate_response_headers([(b"x-test", b"\tvalue\x80")]) == (False, None)


@pytest.mark.parametrize("name", [b"", b"x evil", b"x:evil"])
def test_response_header_validator_refuses_each_non_token_name(name: bytes) -> None:
    with pytest.raises(ValueError, match="HTTP token"):
        validate_response_headers([(name, b"value")])


def test_response_header_validator_refuses_non_bytes_fields_by_type() -> None:
    with pytest.raises(TypeError, match="header name"):
        validate_response_headers([(cast(Any, "x-test"), b"value")])
    with pytest.raises(TypeError, match="header value"):
        validate_response_headers([(b"x-test", cast(Any, "value"))])


@pytest.mark.parametrize("value", [b"value\x00", b"value\x1f", b"value\x7f"])
def test_response_header_validator_refuses_each_forbidden_value_octet(value: bytes) -> None:
    with pytest.raises(ValueError, match="control character"):
        validate_response_headers([(b"x-test", value)])


@pytest.mark.parametrize("value", [b"", b"+3", b"3, 3", b"three"])
def test_response_header_validator_refuses_non_decimal_content_length(value: bytes) -> None:
    with pytest.raises(ValueError, match="decimal digits"):
        validate_response_headers([(b"content-length", value)])


def test_problem_detail_refuses_reserved_extensions() -> None:
    with pytest.raises(ValueError, match="reserved.*status"):
        ProblemDetail(400, extensions={"status": 200})


def test_problem_detail_snapshots_extension_mapping() -> None:
    extensions = {"request_id": "original"}
    problem = ProblemDetail(400, extensions=extensions)

    extensions["request_id"] = "attacker"

    assert ProblemResponse(problem).body == (
        b'{"type":"about:blank","title":"Bad Request","status":400,'
        b'"request_id":"original"}'
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"status": True},
        {"status": 99},
        {"status": 400, "title": 1},
        {"status": 400, "detail": b"bad"},
        {"status": 400, "type": 1},
        {"status": 400, "instance": 1},
        {"status": 400, "extensions": {1: "bad"}},
    ],
)
def test_problem_detail_refuses_invalid_document_shape(kwargs: dict[str, Any]) -> None:
    with pytest.raises((TypeError, ValueError)):
        ProblemDetail(**kwargs)


def test_content_digest_uses_explicit_content_instead_of_the_response_body() -> None:
    response = Response(b"response body")
    expected = Response(b"transmitted content")

    response.set_content_digest("sha-256", content=b"transmitted content")
    expected.set_content_digest("sha-256")

    assert response.headers[-1] == expected.headers[-1]


def test_attachment_disposition_uses_ascii_and_extended_filename_forms() -> None:
    assert _disposition("asset.txt") == b'attachment; filename="asset.txt"'
    assert _disposition("caf\N{LATIN SMALL LETTER E WITH ACUTE}.txt") == (
        b'attachment; filename="caf?.txt"; filename*=UTF-8\'\'caf%C3%A9.txt'
    )


def test_exact_html_headers_materialize_only_when_observed() -> None:
    response = HTMLResponse(b"hello")

    assert response._headers is None
    first = response.headers
    assert first == [
        (b"content-type", b"text/html; charset=utf-8"),
        (b"content-length", b"5"),
    ]
    assert response.headers is first


def test_subclasses_carry_their_media_type() -> None:
    assert TextResponse("hi").headers == [
        (b"content-type", b"text/plain; charset=utf-8"),
        (b"content-length", b"2"),
    ]
    assert JSONResponse({"a": 1}).headers == [
        (b"content-type", b"application/json"),
        (b"content-length", b"7"),
    ]


def test_content_length_beyond_cache() -> None:
    # The small-value cache covers < 2048; larger bodies format on demand.
    for size in (0, 1, 2047, 2048, 2049, 70000):
        response = Response(b"x" * size)
        assert (b"content-length", str(size).encode()) in response.headers


def test_media_type_override_and_empty() -> None:
    response = Response(b"x", media_type=b"text/csv")
    assert (b"content-type", b"text/csv") in response.headers
    response = Response(b"x", media_type=b"")
    assert not any(key == b"content-type" for key, _ in response.headers)


def test_status_without_body_omits_content_length() -> None:
    for status in (101, 199, 204, 205, 304):
        response = Response(b"ignored", status=status)
        assert response.body == b""
        assert not any(key == b"content-length" for key, _ in response.headers)


@pytest.mark.parametrize("status", [101, 204, 205, 304])
def test_streaming_response_refuses_status_that_forbids_content(status: int) -> None:
    async def chunks():
        yield b"content"

    with pytest.raises(ValueError, match="must not carry a streaming body"):
        StreamingResponse(chunks(), status=status)


def test_streaming_response_validates_status_and_headers_at_construction() -> None:
    async def chunks():
        yield b"content"

    with pytest.raises(TypeError, match="status"):
        StreamingResponse(chunks(), status=True)
    with pytest.raises(ValueError, match="header"):
        StreamingResponse(chunks(), headers=[(b"x-test", b"ok\r\nx-evil: injected")])


def test_file_response_validates_status_and_headers_at_construction(tmp_path) -> None:
    target = tmp_path / "asset.bin"
    with pytest.raises(TypeError, match="status"):
        FileResponse(target, status=True)
    with pytest.raises(ValueError, match="header"):
        FileResponse(target, headers=[(b"x-test", b"ok\r\nx-evil: injected")])
    with pytest.raises(ValueError, match="content-type"):
        FileResponse(target, headers=[(b"content-type", b"text/plain")])
    with pytest.raises(ValueError, match="content-length"):
        FileResponse(target, headers=[(b"content-length", b"1")])


def test_encoded_response_fast_shapes_preserve_non_default_status_semantics() -> None:
    for response in (TextResponse("created", status=201), JSONResponse({"ok": True}, status=201)):
        assert response.status == 201
        assert response.body
        assert (b"content-length", str(len(response.body)).encode()) in response.headers
    bodyless = TextResponse("ignored", status=204), JSONResponse({"ignored": True}, status=304)
    for response in bodyless:
        assert response.body == b""
        assert not any(key == b"content-length" for key, _ in response.headers)


def test_html_response_fast_shape_preserves_general_status_semantics() -> None:
    response = HTMLResponse("ignored", status=204)
    assert response.body == b""
    assert response.status == 204
    assert not any(key == b"content-length" for key, _ in response.headers)


def test_html_response_fast_shape_uses_a_subclass_media_type() -> None:
    class FragmentResponse(HTMLResponse):
        media_type = b"application/xhtml+xml"

    response = FragmentResponse("<p>ok</p>")
    assert response.headers == [
        (b"content-type", b"application/xhtml+xml"),
        (b"content-length", b"9"),
    ]


def test_caller_headers_are_preserved_and_deduplicated() -> None:
    response = Response(
        b"abc",
        headers=[(b"x-custom", b"1"), (b"Content-Type", b"text/html")],
    )
    # Existing content-type (any case) wins; content-length is appended.
    assert response.headers[:2] == [(b"x-custom", b"1"), (b"Content-Type", b"text/html")]
    assert response.headers[2:] == [(b"content-length", b"3")]

    response = Response(b"abc", headers=[(b"CONTENT-LENGTH", b"3")])
    assert (b"content-type", b"application/octet-stream") in response.headers
    assert sum(key.lower() == b"content-length" for key, _ in response.headers) == 1


def test_default_header_lists_are_not_shared_between_instances() -> None:
    first = Response(b"a")
    second = Response(b"bb")
    first.headers.append((b"x-marker", b"1"))
    assert (b"x-marker", b"1") not in second.headers


@pytest.mark.asyncio
async def test_streaming_response_signals_incremental_forwarding() -> None:
    async def chunks():
        yield b"a"
        yield b"b"

    sent = []

    async def send(message):
        sent.append(message)

    await StreamingResponse(chunks(), headers=[(b"content-type", b"text/plain")])(send)
    assert sent[0]["headers"] == [
        (b"content-type", b"text/plain"),
        (b"incremental", b"?1"),
    ]
    assert [m.get("body") for m in sent[1:]] == [b"a", b"b", b""]


def test_streaming_response_can_explicitly_allow_buffering() -> None:
    async def chunks():
        yield b"a"

    response = StreamingResponse(chunks(), incremental=False)
    assert response.headers == [(b"incremental", b"?0")]


def test_streaming_response_refuses_ambiguous_incremental_declarations() -> None:
    async def chunks():
        yield b"a"

    with pytest.raises(ValueError, match=r"incremental=False"):
        StreamingResponse(chunks(), headers=[(b"Incremental", b"?0")])
    with pytest.raises(TypeError, match=r"incremental must be bool"):
        StreamingResponse(chunks(), incremental=1)


@pytest.mark.asyncio
async def test_header_response_decorates_without_mutating_a_reusable_response() -> None:
    response = PreparedResponse.text("ok", headers=[(b"x-base", b"1")])
    additions = [(b"deprecation", b"@1688169599")]
    decorated = _HeaderResponse(response, additions)
    additions.append((b"sunset", b"Sat, 30 Jun 2023 23:59:59 GMT"))
    first: list[dict[str, Any]] = []
    second: list[dict[str, Any]] = []

    async def send_first(message: dict[str, Any]) -> None:
        first.append(message)

    async def send_second(message: dict[str, Any]) -> None:
        second.append(message)

    await decorated(send_first)
    await decorated(send_second)

    assert decorated.status == response.status
    assert decorated.background is response.background
    assert decorated.body == response.body
    assert decorated.headers == [*response.headers, (b"deprecation", b"@1688169599")]
    assert (b"deprecation", b"@1688169599") not in response.headers
    assert first[0]["headers"] == [
        *response.headers,
        (b"deprecation", b"@1688169599"),
    ]
    assert second[0]["headers"] == first[0]["headers"]
    assert second[0]["headers"] is not first[0]["headers"]


@pytest.mark.asyncio
async def test_header_response_decorates_every_response_shape(tmp_path) -> None:
    path = tmp_path / "asset.bin"
    path.write_bytes(b"file")

    async def chunks():
        yield b"stream"

    def recorder(messages: list[dict[str, Any]]):
        async def send(message: dict[str, Any]) -> None:
            messages.append(message)

        return send

    responses = [
        Response(b"plain"),
        PreparedResponse(b"prepared"),
        StreamingResponse(chunks()),
        FileResponse(path),
    ]
    for response in responses:
        sent: list[dict[str, Any]] = []
        await _HeaderResponse(response, ((b"sunset", b"date"),))(recorder(sent))
        assert sent[0]["headers"][-1] == (b"sunset", b"date")


@pytest.mark.asyncio
async def test_header_response_preserves_file_head_without_reading_a_body(tmp_path) -> None:
    path = tmp_path / "asset.bin"
    path.write_bytes(b"file")
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await _HeaderResponse(FileResponse(path), ((b"sunset", b"date"),))._head(send)

    assert (b"content-length", b"4") in sent[0]["headers"]
    assert (b"sunset", b"date") in sent[0]["headers"]
    assert sent[1] == {"type": "http.response.body", "body": b""}


def test_prepared_text_headers_and_body() -> None:
    response = PreparedResponse.text("service healthy")
    assert response.status == 200
    assert response.body == b"service healthy"
    assert (b"content-type", b"text/plain; charset=utf-8") in response.headers
    assert (b"content-length", b"15") in response.headers
    assert response.background is None


def test_prepared_json_and_html() -> None:
    j = PreparedResponse.json({"ok": True})
    assert j.body == b'{"ok":true}'
    assert (b"content-type", b"application/json") in j.headers
    h = PreparedResponse.html("<b>hi</b>")
    assert h.body == b"<b>hi</b>"
    assert (b"content-type", b"text/html; charset=utf-8") in h.headers


def test_prepared_no_body_status_drops_length() -> None:
    for status in (101, 199, 204, 205, 304):
        response = PreparedResponse(b"ignored", status=status)
        assert response.body == b""
        assert all(name != b"content-length" for name, _ in response.headers)


def test_prepared_response_refuses_ambiguous_content_length() -> None:
    with pytest.raises(ValueError, match="content-length"):
        PreparedResponse(b"abc", headers=[(b"content-length", b"4")])


@pytest.mark.asyncio
async def test_prepared_emits_two_messages() -> None:
    response = PreparedResponse.text("hi")
    sent = []

    async def send(message):
        sent.append(message)

    await response(send)
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 200
    assert sent[1] == {"type": "http.response.body", "body": b"hi"}


@pytest.mark.asyncio
async def test_prepared_is_reusable_and_immutable() -> None:
    response = PreparedResponse.text("shared")
    first_headers = response.headers

    async def collect():
        sent = []

        async def send(message):
            sent.append(message)

        await response(send)
        return sent

    a = await collect()
    b = await collect()
    # Same bytes each time and no per-call mutation of the shared instance.
    assert a[1]["body"] == b[1]["body"] == b"shared"
    assert response.headers is first_headers


def test_prepared_rejects_non_bytes() -> None:
    with pytest.raises(TypeError):
        PreparedResponse(cast(Any, "not bytes"))


def _fd_is_open(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


def test_from_descriptor_closes_the_file_when_the_response_is_never_sent(tmp_path) -> None:
    path = tmp_path / "asset.bin"
    path.write_bytes(b"payload")
    fd = os.open(path, os.O_RDONLY)

    response = FileResponse.from_descriptor(fd, os.stat(fd), "asset.bin")
    assert _fd_is_open(fd)

    del response

    assert not _fd_is_open(fd)


def test_from_descriptor_close_is_explicit_and_idempotent(tmp_path) -> None:
    path = tmp_path / "asset.bin"
    path.write_bytes(b"payload")
    fd = os.open(path, os.O_RDONLY)

    response = FileResponse.from_descriptor(fd, os.stat(fd), "asset.bin")
    response.close()
    assert not _fd_is_open(fd)
    response.close()  # a second close must not touch a now-reused descriptor
    del response


def test_a_partially_initialized_file_response_is_safe_to_finalize() -> None:
    response = object.__new__(FileResponse)
    response.__del__()


@pytest.mark.asyncio
async def test_a_sent_descriptor_response_is_not_closed_twice(tmp_path) -> None:
    path = tmp_path / "asset.bin"
    path.write_bytes(b"payload")
    fd = os.open(path, os.O_RDONLY)

    sent: list[dict] = []

    async def send(message) -> None:
        sent.append(message)

    response = FileResponse.from_descriptor(fd, os.stat(fd), "asset.bin")
    await response(send)

    assert response._fd is None
    assert b"".join(m.get("body", b"") for m in sent[1:]) == b"payload"
    del response


def test_html_response_takes_the_bytes_a_template_already_produced() -> None:
    document = "<p>café &amp; ☃</p>"

    from_str = HTMLResponse(document)
    from_bytes = HTMLResponse(document.encode("utf-8"))

    assert from_bytes.body == from_str.body
    assert from_bytes.headers == from_str.headers
    assert from_bytes.status == from_str.status
