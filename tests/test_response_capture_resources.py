import tracemalloc

import pytest

from wreath._asgi_state import ResponseCapture


async def test_response_capture_reuses_completed_body_without_retaining_chunks():
    capture = ResponseCapture()
    await capture.send({"type": "http.response.start", "status": 200})
    for index in range(8):
        await capture.send(
            {
                "type": "http.response.body",
                "body": bytes([index]) * 1024 * 1024,
                "more_body": index < 7,
            }
        )
    body = capture.body
    tracemalloc.start()
    try:
        again = capture.body
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert again is body
    assert peak < 1024
    assert capture._chunks == [body]
    assert body == b"".join(bytes([index]) * 1024 * 1024 for index in range(8))


async def test_partial_body_read_then_more_chunks():
    capture = ResponseCapture()
    assert capture.body == b""
    await capture.send({"type": "http.response.start", "status": 201})
    for value in (b"a", b"b"):
        await capture.send({"type": "http.response.body", "body": value, "more_body": True})
    first = capture.body
    assert first == b"ab"
    await capture.send({"type": "http.response.body", "body": b"c"})
    assert capture.body == b"abc"
    assert first == b"ab"
    capture.require_complete()


async def test_nonstrict_writes_after_completed_materialization():
    capture = ResponseCapture(strict=False)
    for value in (b"a", b"b"):
        await capture.send({"type": "http.response.body", "body": value})
    assert capture.body == b"ab"
    await capture.send({"type": "wreath.response", "status": 202, "body": b"c"})
    assert capture.body == b"abc"
    assert capture.status == 202


@pytest.mark.parametrize("kind", ["wreath.response", "http.response.body"])
async def test_materialization_preserves_input_freezing_and_integer_coercion(kind):
    capture = ResponseCapture(strict=False)
    value = bytearray(b"original")
    await capture.send({"type": kind, "status": 200, "body": value})
    value[:] = b"modified"
    await capture.send({"type": kind, "status": 200, "body": 2})
    assert capture.body == b"original\x00\x00"


async def test_materialization_does_not_change_strict_refusals():
    capture = ResponseCapture()
    with pytest.raises(RuntimeError, match="before response start"):
        await capture.send({"type": "http.response.body"})
    await capture.send({"type": "http.response.start", "status": 200})
    with pytest.raises(RuntimeError, match="two response starts"):
        await capture.send({"type": "http.response.start", "status": 200})
    await capture.send({"type": "http.response.body", "body": b"body"})
    assert capture.body == b"body"
    with pytest.raises(RuntimeError, match="after the response ended"):
        await capture.send({"type": "http.response.body", "body": b"extra"})
