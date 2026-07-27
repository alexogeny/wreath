"""The native SSE framer is byte-for-byte the pure framer.

`_sse_frame_fields` in `src/wreath/response.py` stays the reference
implementation and the parity contract; `src/wreath/_native/sse.c` is a faster
twin that walks the payload once instead of copying it through two `replace()`
calls and a `split()`.

Line-ending normalisation is where the two can most easily diverge, so CR, LF,
CRLF, lone trailing breaks, and empty segments are all pinned here -- a
divergence in any of them would put an extra or missing blank line into a live
stream, which ends the *event* rather than the line.
"""

from __future__ import annotations

import pytest

from wreath.response import ServerSentEvent, _encode_sse, _sse_frame_fields

native_frame = pytest.importorskip("wreath._native._core").sse_frame


def _same(
    comment: str | None = None,
    name: str | None = None,
    ident: str | None = None,
    retry: int | None = None,
    data: str | None = None,
) -> bytes:
    expected = _sse_frame_fields(comment, name, ident, retry, data)
    assert native_frame(comment, name, ident, retry, data) == expected
    return expected


def test_empty_event_is_a_bare_keepalive() -> None:
    assert _same() == b":\n\n"


@pytest.mark.parametrize(
    "data",
    [
        "",
        "x",
        "hello world",
        "line one\nline two",
        "crlf\r\nsecond",
        "bare-cr\rsecond",
        "mixed\r\na\rb\nc",
        "\n",                  # a lone break: two empty segments
        "\r\n",
        "trailing\n",
        "\nleading",
        "a\n\nb",              # an empty line between two segments
        "unicode: héllo 日本語 🎄",
        "é\né",                # multibyte either side of a break
        "x" * 5000,
    ],
)
def test_data_line_framing(data: str) -> None:
    _same(data=data)


@pytest.mark.parametrize("comment", ["", "ka", "two\nlines", "cr\rhere"])
def test_comment_framing(comment: str) -> None:
    _same(comment=comment)


def test_all_fields_together() -> None:
    _same(comment="c", name="progress", ident="42", retry=3000, data="payload")


@pytest.mark.parametrize("retry", [0, 1, 3000, 2**31])
def test_retry_is_rendered_as_an_integer(retry: int) -> None:
    _same(retry=retry)


@pytest.mark.parametrize("field,value", [("name", "a\nb"), ("ident", "a\rb")])
def test_single_line_fields_refuse_newlines_in_both(field: str, value: str) -> None:
    name = value if field == "name" else None
    ident = value if field == "ident" else None
    with pytest.raises(ValueError, match="must not contain a newline"):
        _sse_frame_fields(None, name, ident, None, None)
    with pytest.raises(ValueError, match="must not contain a newline"):
        native_frame(None, name, ident, None, None)


def test_encode_sse_uses_the_selected_framer_for_every_shape() -> None:
    """The public entry point still frames all three input shapes identically."""
    event = ServerSentEvent(data="payload", event="progress", id="7", retry=100)
    assert _encode_sse(event) == _sse_frame_fields(None, "progress", "7", 100, "payload")
    assert _encode_sse("bare") == _sse_frame_fields(None, None, None, None, "bare")
    assert _encode_sse(b"bytes") == _sse_frame_fields(None, None, None, None, "bytes")
    assert _encode_sse({"data": "m", "event": "e", "id": "1"}) == _sse_frame_fields(
        None, "e", "1", None, "m"
    )


def test_bytes_data_is_decoded_as_utf8() -> None:
    assert _encode_sse(ServerSentEvent(data="é".encode())) == _sse_frame_fields(
        None, None, None, None, "é"
    )
