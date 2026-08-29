from __future__ import annotations

import pytest

from wreath._native import _core
from wreath.response import ServerSentEvent, _encode_sse


def frame(
    comment: str | None = None,
    name: str | None = None,
    ident: str | None = None,
    retry: int | None = None,
    data: str | None = None,
) -> bytes:
    return _core.sse_frame(comment, name, ident, retry, data)


def test_empty_event_is_a_bare_keepalive() -> None:
    assert frame() == b":\n\n"


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ("", b"data:\n\n"),
        ("x", b"data: x\n\n"),
        ("hello world", b"data: hello world\n\n"),
        ("line one\nline two", b"data: line one\ndata: line two\n\n"),
        ("crlf\r\nsecond", b"data: crlf\ndata: second\n\n"),
        ("bare-cr\rsecond", b"data: bare-cr\ndata: second\n\n"),
        ("mixed\r\na\rb\nc", b"data: mixed\ndata: a\ndata: b\ndata: c\n\n"),
        ("\n", b"data:\ndata:\n\n"),
        ("\r\n", b"data:\ndata:\n\n"),
        ("trailing\n", b"data: trailing\ndata:\n\n"),
        ("\nleading", b"data:\ndata: leading\n\n"),
        ("a\n\nb", b"data: a\ndata:\ndata: b\n\n"),
        ("unicode: héllo 日本語 🎄", "data: unicode: héllo 日本語 🎄\n\n".encode()),
        ("é\né", "data: é\ndata: é\n\n".encode()),
        ("x" * 5000, b"data: " + b"x" * 5000 + b"\n\n"),
    ],
)
def test_data_line_framing(data: str, expected: bytes) -> None:
    assert frame(data=data) == expected


@pytest.mark.parametrize(
    ("comment", "expected"),
    [
        ("", b":\n\n"),
        ("ka", b": ka\n\n"),
        ("two\nlines", b": two\n: lines\n\n"),
        ("cr\rhere", b": cr\n: here\n\n"),
    ],
)
def test_comment_framing(comment: str, expected: bytes) -> None:
    assert frame(comment=comment) == expected


def test_all_fields_together() -> None:
    assert frame("c", "progress", "42", 3000, "payload") == (
        b": c\nevent: progress\nid: 42\nretry: 3000\ndata: payload\n\n"
    )


@pytest.mark.parametrize(
    ("retry", "wire"),
    [(0, b"0"), (1, b"1"), (3000, b"3000"), (2**31, b"2147483648")],
)
def test_retry_is_rendered_as_an_integer(retry: int, wire: bytes) -> None:
    assert frame(retry=retry) == b"retry: " + wire + b"\n\n"


@pytest.mark.parametrize(("name", "ident"), [("a\nb", None), (None, "a\rb")])
def test_single_line_fields_refuse_newlines(name: str | None, ident: str | None) -> None:
    with pytest.raises(ValueError, match="must not contain a newline"):
        frame(name=name, ident=ident)


def test_public_entry_frames_each_input_shape() -> None:
    event = ServerSentEvent(data="payload", event="progress", id="7", retry=100)
    assert _encode_sse(event) == b"event: progress\nid: 7\nretry: 100\ndata: payload\n\n"
    assert _encode_sse("bare") == b"data: bare\n\n"
    assert _encode_sse(b"bytes") == b"data: bytes\n\n"
    assert _encode_sse({"data": "m", "event": "e", "id": "1"}) == (b"event: e\nid: 1\ndata: m\n\n")


def test_bytes_data_is_decoded_as_utf8() -> None:
    assert _encode_sse(ServerSentEvent(data="é".encode())) == "data: é\n\n".encode()
