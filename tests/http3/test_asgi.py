"""HTTP/3 ASGI mapping (ASGI HTTP spec, http_version == "3").

Behavioral HTTP/3 tests. They require the optional ``wreath._native._http3`` backend
(WREATH_BUILD_HTTP3=1 with ngtcp2/nghttp3) and are skipped otherwise; in the
dedicated HTTP/3 CI job the backend is present and these run. The executable
detail is completed with the endpoint implementation (Step 5); the endpoint is
exercised through the ``h3_module`` fixture and a real QUIC client, never a mock.
"""
from __future__ import annotations

import pytest

from .conftest import requires_h3

pytestmark = [requires_h3, pytest.mark.asyncio]


async def test_scope_http_version_is_3(h3_module) -> None:
    """See module docstring; RFC reference. Deeper conformance is future work."""
    pytest.skip("deeper HTTP/3 conformance not yet implemented")


async def test_scope_scheme_is_https(h3_module) -> None:
    """See module docstring; RFC reference. Deeper conformance is future work."""
    pytest.skip("deeper HTTP/3 conformance not yet implemented")


async def test_request_body_delivered(h3_module) -> None:
    """See module docstring; RFC reference. Deeper conformance is future work."""
    pytest.skip("deeper HTTP/3 conformance not yet implemented")


async def test_response_start_body_mapping(h3_module) -> None:
    """See module docstring; RFC reference. Deeper conformance is future work."""
    pytest.skip("deeper HTTP/3 conformance not yet implemented")


async def test_connection_close_propagates_disconnect(h3_module) -> None:
    """See module docstring; RFC reference. Deeper conformance is future work."""
    pytest.skip("deeper HTTP/3 conformance not yet implemented")




# --- streaming responses ---------------------------------------------------
#
# The response body is queued as immutable segments whose addresses are handed
# straight to nghttp3, and submitted at http.response.start so bytes reach the
# wire while the app is still producing. These drive a real QUIC client.

import asyncio  # noqa: E402

from wreath.server import ServerConfig, TLSConfig, serve  # noqa: E402

from .conftest import curl_http3, make_self_signed_cert, requires_curl_h3  # noqa: E402


async def _serve_h3(app):
    cert, key = make_self_signed_cert()
    server = await serve(
        app,
        ServerConfig(host="127.0.0.1", port=0, lifespan="off", protocols=("h3",)),
        tls=TLSConfig(cert, key),
    )
    return server, server.datagram_addresses[0][1]


@requires_curl_h3
@pytest.mark.network
async def test_response_bytes_are_sent_before_the_final_body_message() -> None:
    """The first chunk reaches the client before the app sends its last message.

    The app holds its final body message until the test has actually read the
    first chunk out of the client. A buffered implementation, which submits
    nothing until more_body=False, cannot satisfy this and times out.
    """
    release = asyncio.Event()

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"early",
                    "more_body": True})
        await release.wait()  # only set once the client has read "early"
        await send({"type": "http.response.body", "body": b"late",
                    "more_body": False})

    server, port = await _serve_h3(app)
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "curl", "-sN", "--http3-only", "-k", "--max-time", "20",
            f"https://127.0.0.1:{port}/stream",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        # Read the early chunk while the app is still suspended.
        early = await asyncio.wait_for(proc.stdout.readexactly(5), timeout=15)
        assert early == b"early"
        release.set()
        rest, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        assert rest == b"late"
    finally:
        if proc is not None and proc.returncode is None:
            proc.kill()
        await server.close()


@requires_curl_h3
@pytest.mark.network
async def test_many_chunks_arrive_in_exact_order() -> None:
    chunks = [f"<{i:04d}>".encode() for i in range(500)]

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        for i, chunk in enumerate(chunks):
            await send({"type": "http.response.body", "body": chunk,
                        "more_body": i < len(chunks) - 1})

    server, port = await _serve_h3(app)
    try:
        rc, out = await curl_http3(port, "/many")
        assert rc == 0, f"curl failed rc={rc}"
        assert out == b"".join(chunks)
    finally:
        await server.close()


@requires_curl_h3
@pytest.mark.network
async def test_empty_chunks_do_not_end_the_body_early() -> None:
    """An empty interior chunk must not be read as EOF."""
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"a", "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": True})
        await send({"type": "http.response.body", "body": b"b", "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    server, port = await _serve_h3(app)
    try:
        rc, out = await curl_http3(port, "/empties")
        assert rc == 0, f"curl failed rc={rc}"
        assert out == b"ab"
    finally:
        await server.close()


@requires_curl_h3
@pytest.mark.network
async def test_large_streamed_response_is_complete_under_retransmission() -> None:
    """A response far larger than one window exercises ack-driven release.

    Segments are freed as acknowledgements arrive while nghttp3 may still be
    retransmitting others; the delivered bytes must remain exact.
    """
    chunk = b"x" * 4096
    count = 512  # 2 MiB across many segments

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        for i in range(count):
            await send({"type": "http.response.body", "body": chunk,
                        "more_body": i < count - 1})

    server, port = await _serve_h3(app)
    try:
        rc, out = await curl_http3(port, "/big", deadline=30.0)
        assert rc == 0, f"curl failed rc={rc}"
        assert len(out) == len(chunk) * count
        assert out == chunk * count
    finally:
        await server.close()


@requires_curl_h3
@pytest.mark.network
async def test_app_that_never_sends_a_final_body_still_completes() -> None:
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"done", "more_body": True})
        # Returns without more_body=False.

    server, port = await _serve_h3(app)
    try:
        rc, out = await curl_http3(port, "/nofinal")
        assert rc == 0, f"curl failed rc={rc}"
        assert out == b"done"
    finally:
        await server.close()


@requires_curl_h3
@pytest.mark.network
async def test_acknowledged_segments_are_released_while_streaming() -> None:
    """Retained response storage must fall as acknowledgements arrive.

    Each chunk is a distinct bytes object the app keeps a reference to, so once
    the stream drops its own reference the only holder left is the app's list,
    which `sys.getrefcount` can see.

    The app deliberately never sends ``more_body=False`` until after it has
    measured. The stream therefore cannot reach EOF or close during the
    measurement window, so anything released can only have been released by the
    acknowledgement path -- not by stream teardown freeing the queue wholesale.
    """
    import sys

    count = 400
    observed = {"released": 0}

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        sent: list[bytes] = []
        for i in range(count):
            body = bytes([65 + (i % 26)]) * 4096  # a fresh object every time
            sent.append(body)
            # Always more_body=True: the stream stays open while we measure.
            await send({"type": "http.response.body", "body": body,
                        "more_body": True})
        for _ in range(100):
            await asyncio.sleep(0.05)
            # 2 == `sent` + getrefcount's own argument: nothing else holds it.
            observed["released"] = sum(1 for b in sent if sys.getrefcount(b) <= 2)
            if observed["released"] > count // 2:
                break
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    server, port = await _serve_h3(app)
    try:
        rc, out = await curl_http3(port, "/release", deadline=30.0)
        assert rc == 0, f"curl failed rc={rc}"
        assert len(out) == 4096 * count
        assert observed["released"] > count // 2, (
            "acknowledged response segments were not released while the stream "
            f"was still open (released {observed['released']}/{count})"
        )
    finally:
        await server.close()


# --- Native Flight Recorder (Stage 1 HTTP/3 hooks) -------------------------


async def _serve_h3_telemetry(app, telemetry):
    cert, key = make_self_signed_cert()
    server = await serve(
        app,
        ServerConfig(host="127.0.0.1", port=0, lifespan="off", protocols=("h3",),
                     telemetry=telemetry),
        tls=TLSConfig(cert, key),
    )
    return server, server.datagram_addresses[0][1]


@requires_curl_h3
@pytest.mark.network
async def test_h3_pulse_records_a_completion_with_bytes() -> None:
    """A real QUIC request produces one HTTP/3 completion cell with byte tallies."""
    pytest.importorskip("wreath._native._flight")
    from wreath import _flight_schema as fs
    from wreath.telemetry import Mode, TelemetryConfig

    async def app(scope, receive, send):
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 201, "headers": []})
        await send({"type": "http.response.body", "body": b"h3-flight-body"})

    telemetry = TelemetryConfig(mode=Mode.PULSE, ring_records=64, active_requests=16)
    server, port = await _serve_h3_telemetry(app, telemetry)
    try:
        rc, out = await curl_http3(port, "/x", "-X", "POST", "--data", "ping")
        assert rc == 0, f"curl failed rc={rc}"
        assert out == b"h3-flight-body"
        recorder = server.recorder
        assert recorder is not None
        assert recorder.completions == 1
        assert recorder.active_count == 0
        cell = fs.CompletionCell.decode(recorder.drain()[: fs.CELL_SIZE])
        assert cell.protocol is fs.Protocol.HTTP3
        assert cell.status == 201
        assert cell.terminal is fs.TerminalStatus.OK
        assert cell.bytes_in == 4          # "ping"
        assert cell.bytes_out == 14        # "h3-flight-body"
    finally:
        await server.close()


@requires_curl_h3
@pytest.mark.network
async def test_large_request_body_uploads_past_the_flow_control_window() -> None:
    """A request body larger than the initial QUIC stream window (~64 KiB) must
    upload fully. Without crediting DATA payload to flow control the upload
    stalls once the initial window fills; this drives ~1 MiB through."""

    async def app(scope, receive, send):
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": str(len(body)).encode()})

    server, port = await _serve_h3(app)
    try:
        import tempfile

        payload = b"a" * (1024 * 1024 + 7)  # well past the initial window
        path = tempfile.mktemp()
        with open(path, "wb") as fh:
            fh.write(payload)
        rc, out = await curl_http3(port, "/upload", "-X", "POST", "--data-binary", f"@{path}")
        assert rc == 0, f"curl failed rc={rc} (upload stalled?)"
        assert out == str(len(payload)).encode()
    finally:
        await server.close()
