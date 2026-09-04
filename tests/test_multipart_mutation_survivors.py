from __future__ import annotations

from collections.abc import Iterable

import pytest

from wreath import _multipart
from wreath._native import _core


def _body(payload: bytes = b"value", *, boundary: bytes = b"B") -> bytes:
    return (
        b"--"
        + boundary
        + b'\r\nContent-Disposition: form-data; name="field"\r\n\r\n'
        + payload
        + b"\r\n--"
        + boundary
        + b"--\r\n"
    )


def _stream_events(chunks: Iterable[bytes], *, boundary: bytes = b"B") -> list[tuple[int, object]]:
    parser = _core.MultipartStreamParser(boundary, 8, 1024, 1024, ValueError)
    events = []
    for chunk in chunks:
        events.extend(parser.feed(chunk))
    parser.finish()
    return events


@pytest.mark.parametrize(
    "name", ["max_parts", "max_part_header_bytes", "max_part_bytes"]
)
@pytest.mark.parametrize("limit", [True, False])
def test_public_parser_limits_require_exact_integers(name: str, limit: bool) -> None:
    with pytest.raises(TypeError, match=rf"{name} must be an integer"):
        _multipart.parse(_body(), b"B", **{name: limit})


@pytest.mark.parametrize("boundary", [b"bad@boundary", b"bad\x00boundary", b"trailing "])
def test_public_parser_refuses_non_rfc_boundary_octets(boundary: bytes) -> None:
    with pytest.raises(ValueError, match="boundary must be 1-70 permitted bytes"):
        _multipart.parse(_body(boundary=boundary), boundary)


@pytest.mark.parametrize("suffix", [b"x", b"-", b"\x00", b" \tx"])
def test_complete_parser_refuses_junk_on_the_closing_boundary_line(suffix: bytes) -> None:
    body = _body()[:-2] + suffix + b"\r\n"

    with pytest.raises(ValueError, match="malformed multipart closing boundary"):
        _core.multipart_parse(body, b"B")


@pytest.mark.parametrize("split", range(1, 5))
def test_stream_parser_refuses_junk_on_the_closing_boundary_line(split: int) -> None:
    body = _body()[:-2] + b"evil\r\n"
    chunks = [body[: -5 + split], body[-5 + split :]]

    with pytest.raises(ValueError, match="malformed multipart closing boundary"):
        _stream_events(chunks)


def test_stream_parser_does_not_recognize_an_opening_boundary_mid_line() -> None:
    parser = _core.MultipartStreamParser(b"B", 8, 1024, 1024, ValueError)

    assert parser.feed(b"preamble--B\r\n\r\nvalue") == []


def test_boundary_shaped_part_data_is_not_a_delimiter() -> None:
    payload = b"before\r\n--B-not-a-delimiter\r\nafter"
    body = _body(payload)

    assert _core.multipart_parse(body, b"B")[0][1].tobytes() == payload
    events = _stream_events(body[index : index + 2] for index in range(0, len(body), 2))
    assert b"".join(payload for kind, payload in events if kind == 1) == payload


def test_quoted_disposition_semicolons_stay_inside_the_filename() -> None:
    headers, name, filename = _core.multipart_part_info(
        b'Content-Disposition: form-data; name="field"; filename="semi;colon.txt"'
    )

    assert headers
    assert name == "field"
    assert filename == "semi;colon.txt"


@pytest.mark.parametrize(
    "disposition",
    [b"attachment", b"attachment; name=field", b"x-form-data; name=field"],
)
def test_parts_require_the_form_data_disposition(disposition: bytes) -> None:
    with pytest.raises(ValueError, match="Content-Disposition must be form-data"):
        _core.multipart_part_info(b"Content-Disposition: " + disposition)


@pytest.mark.parametrize("name", [b"bad name", b"bad\x00name", b"bad\tname", b"bad\x7fname"])
def test_part_header_names_require_ascii_tokens(name: bytes) -> None:
    with pytest.raises(ValueError, match="multipart header name must be an ASCII token"):
        _core.multipart_part_info(name + b": value")


@pytest.mark.parametrize("value", [b"bad\x00value", b"bad\x01value", b"bad\x7fvalue"])
def test_part_header_values_refuse_control_octets(value: bytes) -> None:
    with pytest.raises(ValueError, match="multipart header value contains a control octet"):
        _core.multipart_part_info(b"X-Value: " + value)
