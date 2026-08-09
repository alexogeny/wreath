from __future__ import annotations

import os
import random
import subprocess
import sys

import pytest

from wreath import _client_codec
from wreath._client_codec import serialize_request as selected_serialize_request
from wreath._native import _client, _core
from wreath._pure.http_client import (
    parse_response_head,
    response_framing,
    response_keeps_alive,
    serialize_request,
)


def test_serialize_fixed_request() -> None:
    request = serialize_request(
        "POST",
        b"/events?source=wreath",
        b"partner.example",
        headers=((b"content-type", b"application/json"), (b"x-event-id", b"evt-1")),
        body=b'{}',
    )

    assert request == (
        b"POST /events?source=wreath HTTP/1.1\r\n"
        b"host: partner.example\r\n"
        b"content-type: application/json\r\n"
        b"x-event-id: evt-1\r\n"
        b"content-length: 2\r\n"
        b"\r\n"
        b"{}"
    )


@pytest.mark.parametrize(
    ("method", "target", "host", "headers", "match"),
    [
        ("PO ST", b"/", b"example.com", (), "method"),
        ("GET", b"relative", b"example.com", (), "target"),
        ("GET", b"/", b"example.com\r\nx-bad: yes", (), "host"),
        ("GET", b"/", b"example.com", ((b"x-bad\n", b"value"),), "header name"),
        ("GET", b"/", b"example.com", ((b"x-bad", b"a\r\nb: c"),), "header value"),
        ("GET", b"/", b"example.com", ((b"host", b"other.example"),), "host"),
        ("GET", b"/", b"example.com", ((b"content-length", b"0"),), "content-length"),
        (
            "GET",
            b"/",
            b"example.com",
            ((b"transfer-encoding", b"chunked"),),
            "transfer-encoding",
        ),
    ],
)
def test_serialize_request_rejects_ambiguous_or_injected_input(
    method: str,
    target: bytes,
    host: bytes,
    headers: tuple[tuple[bytes, bytes], ...],
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        serialize_request(method, target, host, headers=headers)


def test_parse_response_head_waits_for_complete_head() -> None:
    assert parse_response_head(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n") is None


def test_parse_response_head_returns_consumed_offset() -> None:
    data = (
        b"HTTP/1.1 202 Accepted\r\n"
        b"Content-Type: application/json\r\n"
        b"X-Event-Id:\t evt-1 \t\r\n"
        b"\r\n"
        b"{}extra"
    )

    parsed = parse_response_head(data)

    assert parsed == (
        1,
        202,
        b"Accepted",
        [(b"content-type", b"application/json"), (b"x-event-id", b"evt-1")],
        data.index(b"\r\n\r\n") + 4,
    )


def test_parse_response_head_accepts_every_fragment_boundary() -> None:
    data = b"HTTP/1.1 204 No Content\r\nx-test: yes\r\n\r\n"
    for split in range(len(data)):
        assert parse_response_head(data[:split]) is None
    assert parse_response_head(data) == (
        1,
        204,
        b"No Content",
        [(b"x-test", b"yes")],
        len(data),
    )


@pytest.mark.parametrize(
    ("data", "match"),
    [
        (b"HTTP/2 200 OK\r\n\r\n", "version"),
        (b"HTTP/1.1 two OK\r\n\r\n", "status"),
        (b"HTTP/1.1 99 Nope\r\n\r\n", "status"),
        (b"HTTP/1.1 200 O\x01K\r\n\r\n", "reason"),
        (b"HTTP/1.1 200 OK\r\n folded: no\r\n\r\n", "folding"),
        (b"HTTP/1.1 200 OK\r\nbad name: no\r\n\r\n", "header name"),
        (b"HTTP/1.1 200 OK\r\nx-test: a\x01b\r\n\r\n", "header value"),
    ],
)
def test_parse_response_head_rejects_malformed_input(data: bytes, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        parse_response_head(data)


@pytest.mark.skipif(
    _core is None or not hasattr(_core, "http_parse_response"),
    reason="native response parser is not built",
)
def test_native_response_head_parser_matches_pure_parser() -> None:
    samples = (
        b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\n{}",
        b"HTTP/1.0 204 No Content\r\nx-test: yes\r\n\r\n",
        b"HTTP/1.1 304\r\n\r\n",
    )
    for sample in samples:
        assert _core.http_parse_response(sample) == parse_response_head(sample)


@pytest.mark.skipif(
    _core is None or not hasattr(_core, "http_serialize_request"),
    reason="native request serializer is not built",
)
def test_native_request_serializer_matches_pure_serializer() -> None:
    cases = (
        ("GET", b"/", b"example.com", (), b""),
        (
            "POST",
            b"/events?source=wreath",
            b"partner.example:8443",
            ((b"Content-Type", b"application/json"), (b"x-id", b"one")),
            b"{}",
        ),
    )
    for method, target, host, headers, body in cases:
        expected = serialize_request(
            method,
            target,
            host,
            headers=headers,
            body=body,
        )
        assert selected_serialize_request(
            method,
            target,
            host,
            headers=headers,
            body=body,
        ) == expected


def test_independent_native_client_module_is_selected_when_available() -> None:
    if _client is None:
        pytest.skip("native client extension is not built")
    assert _client_codec._implementation == "native-client"
    assert callable(_client.serialize_request)
    assert callable(_client.parse_response_head)


def test_wreath_pure_forces_outbound_codec_reference_implementation() -> None:
    environment = os.environ.copy()
    environment["WREATH_PURE"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from wreath._client_codec import _implementation; print(_implementation)",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert completed.stdout.strip() == "pure"


@pytest.mark.skipif(_client is None, reason="native client extension is not built")
def test_native_client_codec_randomized_parity() -> None:
    rng = random.Random(20260716)
    for index in range(500):
        status = rng.randint(100, 599)
        reason = f"Status-{index}".encode()
        headers = tuple(
            (f"x-random-{item}".encode(), f"value-{rng.randrange(10_000)}".encode())
            for item in range(rng.randrange(6))
        )
        response = (
            b"HTTP/1.1 "
            + str(status).encode()
            + b" "
            + reason
            + b"\r\n"
            + b"".join(name + b": " + value + b"\r\n" for name, value in headers)
            + b"\r\n"
        )
        assert _client.parse_response_head(response) == parse_response_head(response)
        split = rng.randrange(len(response))
        assert _client.parse_response_head(response[:split]) is None
        method = rng.choice(("GET", "POST", "PUT", "DELETE"))
        body = bytes(rng.randrange(256) for _ in range(rng.randrange(32)))
        expected = serialize_request(
            method,
            b"/random",
            b"example.com",
            headers=headers,
            body=body,
        )
        assert _client.serialize_request(
            method,
            b"/random",
            b"example.com",
            headers,
            body,
        ) == expected
        framing_headers = rng.choice(
            (
                [],
                [(b"content-length", str(rng.randrange(10_000)).encode())],
                [(b"transfer-encoding", b"gzip"), (b"transfer-encoding", b"chunked")],
            )
        )
        assert _client.response_framing(method, status, framing_headers) == (
            response_framing(method, status, framing_headers)
        )
        connection_headers = rng.choice(
            (
                [],
                [(b"connection", b"close")],
                [(b"connection", b"upgrade, Keep-Alive")],
            )
        )
        minor = rng.randrange(2)
        framed = bool(rng.randrange(2))
        assert _client.response_keeps_alive(minor, connection_headers, framed) == (
            response_keeps_alive(minor, connection_headers, framed)
        )


@pytest.mark.skipif(_client is None, reason="native client extension is not built")
def test_native_client_codec_malformed_input_parity() -> None:
    malformed_responses = (
        b"HTTP/2 200 OK\r\n\r\n",
        b"HTTP/1.1 20 OK\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nbad name: value\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nx-test: a\x01b\r\n\r\n",
    )
    for response in malformed_responses:
        with pytest.raises(ValueError):
            parse_response_head(response)
        with pytest.raises(ValueError):
            _client.parse_response_head(response)

    malformed_framing = (
        [(b"content-length", b"5"), (b"content-length", b"7")],
        [(b"content-length", b"five")],
        [(b"transfer-encoding", b"chunked, gzip")],
        [(b"transfer-encoding", b"chunked, chunked")],
        [(b"content-length", b"5"), (b"transfer-encoding", b"chunked")],
    )
    for headers in malformed_framing:
        with pytest.raises(ValueError) as pure_error:
            response_framing("GET", 200, headers)
        with pytest.raises(ValueError) as native_error:
            _client.response_framing("GET", 200, headers)
        assert str(native_error.value) == str(pure_error.value)
