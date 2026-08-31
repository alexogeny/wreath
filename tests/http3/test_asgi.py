from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from wreath.server import ServerConfig, TLSConfig, serve

from .conftest import curl_http3, make_self_signed_cert, requires_curl_h3, requires_h3

pytestmark = [requires_h3, pytest.mark.asyncio]


# The response body is queued as immutable segments whose addresses are handed
# straight to nghttp3, and submitted at http.response.start so bytes reach the
# wire while the app is still producing. These drive a real QUIC client.


async def _serve_h3(app, **config):
    cert, key = make_self_signed_cert()
    server = await serve(
        app,
        ServerConfig(host="127.0.0.1", port=0, lifespan="off", protocols=("h3",), **config),
        tls=TLSConfig(cert, key),
    )
    return server, server.datagram_addresses[0][1]


async def _serve_h3_config(app, config):
    cert, key = make_self_signed_cert()
    server = await serve(app, config, tls=TLSConfig(cert, key))
    return server, server.datagram_addresses[0][1]


@requires_curl_h3
@pytest.mark.network
async def test_scope_reports_http3_over_https() -> None:
    async def app(scope, receive, send):
        payload = f"{scope['http_version']}|{scope['scheme']}".encode()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": payload})

    server, port = await _serve_h3(app)
    try:
        rc, output = await curl_http3(port, "/scope")
        assert rc == 0
        assert output == b"3|https"
    finally:
        await server.close()


@requires_curl_h3
@pytest.mark.network
async def test_existing_endpoint_keeps_the_default_header_sequence() -> None:
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    config = ServerConfig(
        host="127.0.0.1",
        port=0,
        lifespan="off",
        protocols=("h3",),
        server_header="before",
        date_header=False,
    )
    server, port = await _serve_h3_config(app, config)
    defaults = config._default_response_headers
    defaults.server = b"after"
    defaults.refresh(False)
    object.__setattr__(config, "_default_response_headers", object())
    try:
        rc, output = await curl_http3(port, "/", "-D", "-")
        assert rc == 0
        assert b"server: after\r\n" in output.lower()
    finally:
        await server.close()


@requires_curl_h3
@pytest.mark.network
async def test_application_response_headers_override_defaults() -> None:
    async def app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"server", b"application"), (b"date", b"custom")],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})

    server, port = await _serve_h3(app)
    try:
        rc, output = await curl_http3(port, "/", "-D", "-")
        head = output.lower().partition(b"\r\n\r\n")[0]
        assert rc == 0
        assert head.count(b"server:") == 1
        assert b"server: application" in head
        assert head.count(b"date:") == 1
        assert b"date: custom" in head
    finally:
        await server.close()


@requires_curl_h3
@pytest.mark.network
async def test_invalid_response_header_is_refused_before_start() -> None:
    observed = []

    async def app(scope, receive, send):
        with pytest.raises(RuntimeError, match="response header must be a pair") as caught:
            await send({"type": "http.response.start", "status": 200, "headers": [(b"x",)]})
        observed.append(str(caught.value))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    server, port = await _serve_h3(app)
    try:
        rc, output = await curl_http3(port, "/")
        assert rc == 0
        assert output == b"ok"
        assert observed == ["response header must be a pair"]
    finally:
        await server.close()


@requires_curl_h3
@pytest.mark.network
async def test_response_bytes_are_sent_before_the_final_body_message() -> None:
    release = asyncio.Event()

    async def app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"early", "more_body": True})
        await release.wait()  # only set once the client has read "early"
        await send({"type": "http.response.body", "body": b"late", "more_body": False})

    server, port = await _serve_h3(app)
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "curl",
            "-sN",
            "--http3-only",
            "-k",
            "--max-time",
            "20",
            f"https://127.0.0.1:{port}/stream",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
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
            await send(
                {"type": "http.response.body", "body": chunk, "more_body": i < len(chunks) - 1}
            )

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
    chunk = b"x" * 4096
    count = 512  # 2 MiB across many segments

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        for i in range(count):
            await send({"type": "http.response.body", "body": chunk, "more_body": i < count - 1})

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
@pytest.mark.parametrize(
    ("config", "chunks"),
    [
        (
            {
                "response_high_water": 7,
                "response_low_water": 3,
                "response_high_water_segments": 100,
                "response_low_water_segments": 50,
            },
            (b"aaaa", b"bbbb"),
        ),
        (
            {
                "response_high_water": 1 << 20,
                "response_low_water": 1 << 19,
                "response_high_water_segments": 1,
                "response_low_water_segments": 0,
            },
            (b"a", b"b"),
        ),
    ],
    ids=("bytes", "segments"),
)
async def test_response_retention_watermarks_suspend_asgi_send(
    config: dict[str, int], chunks: tuple[bytes, ...]
) -> None:
    observed: list[bool] = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        for index, chunk in enumerate(chunks):
            waiter = send({"type": "http.response.body", "body": chunk, "more_body": True})
            if index == len(chunks) - 1:
                observed.append(not waiter.done())
            await waiter
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    server, port = await _serve_h3(app, **config)
    try:
        rc, out = await curl_http3(port, "/pressure")
        assert rc == 0, f"curl failed rc={rc}"
        assert out == b"".join(chunks)
        assert observed == [True]
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
            await send({"type": "http.response.body", "body": body, "more_body": True})
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


async def _serve_h3_telemetry(app, telemetry):
    cert, key = make_self_signed_cert()
    server = await serve(
        app,
        ServerConfig(
            host="127.0.0.1", port=0, lifespan="off", protocols=("h3",), telemetry=telemetry
        ),
        tls=TLSConfig(cert, key),
    )
    return server, server.datagram_addresses[0][1]


@requires_curl_h3
@pytest.mark.network
async def test_h3_pulse_records_a_completion_with_bytes() -> None:
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
        assert cell.bytes_in == 4  # "ping"
        assert cell.bytes_out == 14  # "h3-flight-body"
    finally:
        await server.close()


@requires_curl_h3
@pytest.mark.network
async def test_h3_native_ai_refusal_is_a_structured_completion() -> None:
    pytest.importorskip("wreath._native._flight")
    from wreath import Wreath
    from wreath import _flight_schema as fs
    from wreath.metrics import collect
    from wreath.telemetry import Mode, TelemetryConfig

    app = Wreath()
    reached = False

    @app.get("/")
    async def handler(request):
        nonlocal reached
        reached = True
        return "not reached"

    telemetry = TelemetryConfig(mode=Mode.PULSE, ring_records=64, active_requests=16)
    server, port = await _serve_h3_telemetry(app, telemetry)
    try:
        rc, out = await curl_http3(port, "/", "-A", "GPTBot/1.0")
        assert rc == 0, f"curl failed rc={rc}"
        assert b'"status":403' in out
        assert reached is False
        recorder = server.recorder
        assert recorder is not None
        assert recorder.requests == 1
        assert recorder.completions == 1
        cell = fs.CompletionCell.decode(recorder.drain())
        assert cell.protocol is fs.Protocol.HTTP3
        assert cell.status == 403
        assert cell.terminal is fs.TerminalStatus.OK
        assert cell.flags & fs.FLAG_POLICY_REFUSED
        assert cell.flags & fs.FLAG_AI_SCRAPING_REFUSED
        readings = {(row.subsystem, row.instance): row.values for row in collect(app)}
        assert readings[("ai_scraping_policy", "default")] == {"refused": 1}
    finally:
        await server.close()


@requires_curl_h3
@pytest.mark.network
async def test_large_request_body_uploads_past_the_flow_control_window() -> None:

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

        payload = b"a" * (512 * 1024 + 7)  # past flow window, within body limit
        path = tempfile.mktemp()
        await asyncio.to_thread(Path(path).write_bytes, payload)
        rc, out = await curl_http3(port, "/upload", "-X", "POST", "--data-binary", f"@{path}")
        assert rc == 0, f"curl failed rc={rc} (upload stalled?)"
        assert out == str(len(payload)).encode()
    finally:
        await server.close()
