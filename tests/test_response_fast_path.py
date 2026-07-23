"""The response coercion fast paths must be byte-for-byte equivalent to the
Response/TextResponse/JSONResponse constructors they replace.

_coerce_response builds str/bytes/dict handler returns in a single frame via
coerce_text/coerce_json/coerce_bytes. These guard that the shortcut produces
identical status, headers, and body to the full constructors, so the speedup
never changes what a client sees.
"""
from __future__ import annotations

import pytest

from wreath.response import (
    JSONResponse,
    Response,
    TextResponse,
    coerce_bytes,
    coerce_json,
    coerce_text,
)


def _shape(response: Response) -> tuple[int, list, bytes]:
    return (response.status, list(response.headers), response.body)


@pytest.mark.parametrize("body", ["", "ok", "a" * 2000, "unicode: café ☕", "x" * 1023])
def test_coerce_text_matches_text_response(body: str) -> None:
    assert _shape(coerce_text(body)) == _shape(TextResponse(body))


@pytest.mark.parametrize("data", [{}, {"a": 1}, {"nested": {"list": [1, 2, 3]}},
                                  {"k": "v" * 500}])
def test_coerce_json_matches_json_response(data: dict) -> None:
    assert _shape(coerce_json(data)) == _shape(JSONResponse(data))


@pytest.mark.parametrize("body", [b"", b"bytes", b"\x00\x01\x02", b"z" * 5000])
def test_coerce_bytes_matches_response(body: bytes) -> None:
    assert _shape(coerce_bytes(body)) == _shape(Response(body))


def test_fast_paths_produce_a_plain_response_that_emits_natively() -> None:
    # The native metal write path keys on `type(response).__call__ is
    # Response.__call__`; the fast path must satisfy that (it returns a bare
    # Response, and the subclasses do not override __call__).
    for response in (coerce_text("x"), coerce_json({"a": 1}), coerce_bytes(b"x")):
        assert type(response).__call__ is Response.__call__


def test_response_slots_are_fully_populated_by_the_fast_path() -> None:
    # The fast path sets slots by hand; if Response gains a slot this trips so
    # the shortcut is updated rather than silently leaving it unset.
    assert Response.__slots__ == ("background", "body", "headers", "status")
    response = coerce_text("ok")
    for slot in Response.__slots__:
        assert hasattr(response, slot)


@pytest.mark.asyncio
async def test_end_to_end_handler_returns_are_coerced_correctly() -> None:
    from wreath import Wreath

    app = Wreath()

    @app.get("/text")
    async def text_endpoint(request):
        return "hello"

    @app.get("/json")
    async def json_endpoint(request):
        return {"ok": True}

    @app.get("/bytes")
    async def bytes_endpoint(request):
        return b"raw"

    async def call(path: str) -> list[dict]:
        messages = iter([{"type": "http.request", "body": b"", "more_body": False}])
        sent: list[dict] = []

        async def receive():
            return next(messages)

        async def send(message):
            sent.append(message)

        await app({"type": "http", "method": "GET", "path": path,
                   "headers": [], "query_string": b""}, receive, send)
        return sent

    text = await call("/text")
    assert text[0]["status"] == 200
    assert (b"content-type", b"text/plain; charset=utf-8") in text[0]["headers"]
    assert (b"content-length", b"5") in text[0]["headers"]
    assert text[1]["body"] == b"hello"

    js = await call("/json")
    assert (b"content-type", b"application/json") in js[0]["headers"]
    assert js[1]["body"] == b'{"ok":true}'

    raw = await call("/bytes")
    assert (b"content-type", b"application/octet-stream") in raw[0]["headers"]
    assert raw[1]["body"] == b"raw"
