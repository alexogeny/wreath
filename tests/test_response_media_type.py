"""`media_type=` is bytes, and a `str` is refused where it is passed.

The defect this pins was found by the tracking example, which followed the
protobuf recipe verbatim: the recipe showed `media_type="application/x-protobuf"`
and `Response` accepted it, so the application emitted

    (b"content-type", "application/x-protobuf")

onto the wire -- a bytes header name beside a `str` value, which is not a valid
ASGI header. Nothing raised at the call site. What surfaced was a `TypeError`
from whatever read the header afterwards, arbitrarily far from the `media_type=`
that caused it, which is the shape `AGENTS.md` calls a guard worth having.

The parameter has always been annotated `bytes | None`; this enforces that
rather than widening it.
"""

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
