from __future__ import annotations

import asyncio

import pytest

from wreath.server import ServerConfig, TLSConfig, serve

from .conftest import curl_http3, make_self_signed_cert, requires_curl_h3, requires_h3

pytestmark = [requires_h3, pytest.mark.asyncio]

LIMIT = 16 * 1024


def _echo_app(seen: dict):
    """Count every request-body byte the app is handed, then report the total."""

    async def app(scope, receive, send) -> None:
        total = 0
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                seen["disconnected"] = True
                seen["bytes"] = total
                return
            total += len(msg.get("body", b""))
            if not msg.get("more_body", False):
                break
        seen["bytes"] = total
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": str(total).encode()})

    return app


async def _serve(app, **config):
    cert, key = make_self_signed_cert()
    server = await serve(
        app,
        ServerConfig(host="127.0.0.1", port=0, lifespan="off", protocols=("h3",), **config),
        tls=TLSConfig(cert, key),
    )
    return server, server.datagram_addresses[0][1]


def _body_file(tmp_path, size: int, name: str = "body.bin") -> str:
    path = tmp_path / name
    path.write_bytes(b"x" * size)
    return str(path)


@requires_curl_h3
@pytest.mark.network
async def test_body_exactly_at_limit_is_accepted(tmp_path) -> None:
    seen: dict = {}
    server, port = await _serve(_echo_app(seen), max_body_bytes=LIMIT)
    try:
        rc, out = await curl_http3(port, "/", "--data-binary", f"@{_body_file(tmp_path, LIMIT)}")
        assert rc == 0, f"curl failed rc={rc}"
        assert out == str(LIMIT).encode()
        assert seen["bytes"] == LIMIT
    finally:
        await server.close()


@requires_curl_h3
@pytest.mark.network
async def test_body_one_byte_over_limit_terminates_stream(tmp_path) -> None:
    seen: dict = {}
    server, port = await _serve(_echo_app(seen), max_body_bytes=LIMIT)
    try:
        rc, out = await curl_http3(
            port, "/", "--data-binary", f"@{_body_file(tmp_path, LIMIT + 1)}"
        )
        # The stream is reset, so curl cannot report a successful response.
        assert rc != 0, f"expected the stream to be terminated, got rc=0 out={out!r}"
        # Not one byte past the limit is ever handed to the application.
        assert seen.get("bytes", 0) <= LIMIT
    finally:
        await server.close()


@requires_curl_h3
@pytest.mark.network
async def test_body_limit_counts_bytes_across_many_chunks(tmp_path) -> None:
    seen: dict = {}
    server, port = await _serve(_echo_app(seen), max_body_bytes=LIMIT)
    try:
        rc, _ = await curl_http3(port, "/", "--data-binary", f"@{_body_file(tmp_path, 48 * 1024)}")
        assert rc != 0
        assert seen.get("bytes", 0) <= LIMIT
    finally:
        await server.close()


@requires_curl_h3
@pytest.mark.network
async def test_over_limit_stream_does_not_kill_an_unrelated_stream(tmp_path) -> None:
    seen: dict = {}
    server, port = await _serve(_echo_app(seen), max_body_bytes=LIMIT)
    try:
        proc = await asyncio.create_subprocess_exec(
            "curl",
            "-s",
            "--http3-only",
            "-k",
            "--max-time",
            "12",
            "--data-binary",
            f"@{_body_file(tmp_path, LIMIT + 1, 'big.bin')}",
            f"https://127.0.0.1:{port}/rejected",
            "--next",
            "-s",
            "--http3-only",
            "-k",
            "--max-time",
            "12",
            "--data-binary",
            f"@{_body_file(tmp_path, 128, 'small.bin')}",
            f"https://127.0.0.1:{port}/accepted",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=25)
        # The rejected request contributes no successful body; the accepted one
        # must still report its own 128 bytes.
        assert b"128" in out, f"unrelated stream did not survive: {out!r}"
    finally:
        await server.close()


@requires_curl_h3
@pytest.mark.network
async def test_waiting_receiver_is_released_on_rejection(tmp_path) -> None:
    released = asyncio.Event()
    seen: dict = {}

    async def app(scope, receive, send) -> None:
        total = 0
        try:
            while True:
                msg = await receive()
                if msg["type"] == "http.disconnect":
                    seen["disconnected"] = True
                    return
                total += len(msg.get("body", b""))
                if not msg.get("more_body", False):
                    break
            seen["bytes"] = total
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})
        finally:
            released.set()

    server, port = await _serve(app, max_body_bytes=LIMIT)
    try:
        await curl_http3(port, "/", "--data-binary", f"@{_body_file(tmp_path, 48 * 1024)}")
        # The application task must reach its finally clause rather than hang.
        await asyncio.wait_for(released.wait(), timeout=10)
        assert seen.get("disconnected") is True
    finally:
        await server.close()


@requires_curl_h3
@pytest.mark.network
async def test_rejected_bytes_are_not_retained(tmp_path) -> None:
    seen: dict = {}
    gate = asyncio.Event()

    async def app(scope, receive, send) -> None:
        await asyncio.sleep(0.25)  # let the body arrive while nothing is reading
        total = 0
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                break
            total += len(msg.get("body", b""))
            if not msg.get("more_body", False):
                break
        seen["bytes"] = total
        gate.set()

    server, port = await _serve(app, max_body_bytes=LIMIT)
    try:
        await curl_http3(port, "/", "--data-binary", f"@{_body_file(tmp_path, 48 * 1024)}")
        await asyncio.wait_for(gate.wait(), timeout=10)
        assert seen["bytes"] <= LIMIT
    finally:
        await server.close()
