from __future__ import annotations

import pytest

from wreath.response import Response


def test_a_bytes_media_type_reaches_the_header_list() -> None:
    response = Response(b"{}", media_type=b"application/x-protobuf")
    assert (b"content-type", b"application/x-protobuf") in response.headers


def test_a_str_media_type_is_refused_where_it_is_passed() -> None:
    with pytest.raises(TypeError) as caught:
        Response(b"{}", media_type="application/x-protobuf")  # type: ignore[arg-type]
    message = str(caught.value)
    # The refusal has to name the byte literal, because the whole failure mode
    # was a person reading a page that showed the wrong one.
    assert "b'application/x-protobuf'" in message
    assert "bytes" in message


def test_the_refusal_does_not_fire_for_the_default() -> None:
    # `media_type=None` takes the class default and must not reach the check.
    assert (b"content-type", b"application/octet-stream") in Response(b"x").headers


def test_empty_bytes_still_emit_no_content_type() -> None:
    # Documented behaviour, and adjacent to the new branch: keep it pinned.
    assert not any(name == b"content-type" for name, _ in Response(b"x", media_type=b"").headers)
