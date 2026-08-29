from __future__ import annotations

from wreath.response import ServerSentEvent, SSEResponse, _encode_sse


def test_plain_str_is_data() -> None:
    assert _encode_sse("hello") == b"data: hello\n\n"


def test_multiline_data_is_repeated_field() -> None:
    assert _encode_sse("a\nb") == b"data: a\ndata: b\n\n"
    # CRLF/CR normalised to LF
    assert _encode_sse("a\r\nb\rc") == b"data: a\ndata: b\ndata: c\n\n"


def test_all_fields() -> None:
    event = ServerSentEvent("payload", event="tick", id="7", retry=3000)
    assert _encode_sse(event) == b"event: tick\nid: 7\nretry: 3000\ndata: payload\n\n"


def test_comment_keepalive() -> None:
    assert _encode_sse(ServerSentEvent(comment="ping")) == b": ping\n\n"
    # An entirely empty event still frames as a bare keep-alive comment.
    assert _encode_sse(ServerSentEvent()) == b":\n\n"


def test_mapping_event() -> None:
    assert _encode_sse({"event": "e", "data": "d"}) == b"event: e\ndata: d\n\n"


def test_bytes_data() -> None:
    assert _encode_sse(b"raw") == b"data: raw\n\n"


def test_sse_response_headers() -> None:
    async def events():
        yield "x"

    response = SSEResponse(events())
    headers = dict(response.headers)
    assert headers[b"content-type"] == b"text/event-stream"
    assert headers[b"cache-control"] == b"no-cache"
    assert headers[b"x-accel-buffering"] == b"no"
    assert response.status == 200


async def test_sse_response_frames_iterator() -> None:
    async def events():
        yield "one"
        yield ServerSentEvent("two", event="e")

    response = SSEResponse(events())
    chunks = [chunk async for chunk in response.body]
    assert chunks == [b"data: one\n\n", b"event: e\ndata: two\n\n"]
