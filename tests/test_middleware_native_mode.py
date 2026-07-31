"""`Wreath(middleware="native")`: CORS preflights answered before Python.

The table of answers is recorded from `CORSMiddleware` itself at boot, so the
test that matters is differential: for every request shape, the two modes must
put the same bytes on the wire. Anything the table does not cover must fall
through to the Python tape rather than be guessed at -- an origin outside the
recorded set still has `CORSMiddleware`'s normalized compare to face, and
answering it here would refuse a request the framework allows.
"""

from __future__ import annotations

import asyncio
import importlib
from typing import Any

import pytest

from wreath import Response, Wreath
from wreath._middleware_tape import compile_tape
from wreath.middleware.cors import CORSMiddleware
from wreath.server import ServerConfig

_server = importlib.import_module("wreath._native._server")


class Recorder(asyncio.Transport):
    def __init__(self) -> None:
        super().__init__()
        self.seen: list[bytes] = []

    def write(self, data: Any) -> None:
        self.seen.append(bytes(data))

    def writelines(self, list_of_data: Any) -> None:
        for chunk in list_of_data:
            self.seen.append(bytes(chunk))

    def close(self) -> None:
        pass

    def abort(self) -> None:
        pass

    def is_closing(self) -> bool:
        return False

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        if name == "sockname":
            return ("127.0.0.1", 8000)
        if name == "peername":
            return ("127.0.0.1", 5000)
        return default


def build(mode: str, **cors: Any) -> Wreath:
    app = Wreath(middleware=mode)
    app.add_middleware(CORSMiddleware(**(cors or {"allow_origins": ["https://a.test"]})))

    @app.get("/x")
    async def handler(request: Any) -> Response:
        return Response(b"ok")

    app._compile_routes()
    return app


def request(method: str = "OPTIONS", **headers: str) -> bytes:
    lines = [f"{method} /x HTTP/1.1", "host: h"]
    lines += [f"{name.replace('_', '-')}: {value}" for name, value in headers.items()]
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


async def serve(app: Wreath, raw: bytes) -> bytes:
    loop = asyncio.get_running_loop()
    protocol = _server.HttpProtocol(app, ServerConfig(), loop, set())
    transport = Recorder()
    protocol.connection_made(transport)
    protocol.data_received(raw)
    for _ in range(200):
        await asyncio.sleep(0)
        if transport.seen:
            break
    return b"".join(transport.seen)


def preflight(origin: str, method: str) -> bytes:
    return request(origin=origin, access_control_request_method=method)


SHAPES = [
    pytest.param(preflight("https://a.test", "POST"), id="allowed"),
    pytest.param(preflight("https://evil.test", "POST"), id="denied-origin"),
    pytest.param(preflight("https://a.test", "TRACE"), id="denied-method"),
    pytest.param(request(origin="https://a.test"), id="options-without-acrm"),
    pytest.param(request(access_control_request_method="POST"), id="options-no-origin"),
    pytest.param(request(), id="bare-options"),
    pytest.param(request("GET", origin="https://a.test"), id="not-options"),
    # An origin `CORSMiddleware` accepts only after normalizing. The table does
    # not carry it, so the native path must fall through rather than refuse.
    pytest.param(preflight("HTTPS://A.test", "POST"), id="mixed-case-origin"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", SHAPES)
async def test_native_mode_puts_the_same_bytes_on_the_wire(raw: bytes) -> None:
    """The whole contract: opting in must not change a single response byte."""
    python = await serve(build("python"), raw)
    native = await serve(build("native"), raw)
    assert python == native


@pytest.mark.asyncio
async def test_a_wildcard_configuration_matches_too() -> None:
    raw = request(origin="https://anything.test", access_control_request_method="POST")
    python = await serve(build("python", allow_origins=["*"]), raw)
    native = await serve(build("native", allow_origins=["*"]), raw)
    assert python == native
    assert b"access-control-allow-origin: *" in native.lower()


@pytest.mark.asyncio
async def test_credentials_and_headers_configuration_matches() -> None:
    config = {
        "allow_origins": ["https://a.test"],
        "allow_credentials": True,
        "allow_headers": ["x-token"],
        "max_age": 99,
    }
    raw = request(origin="https://a.test", access_control_request_method="GET")
    assert await serve(build("python", **config), raw) == await serve(
        build("native", **config), raw
    )


def test_python_is_the_default_and_compiles_no_table() -> None:
    assert build("python")._native_preflight is None
    assert build("native")._native_preflight is not None


def test_an_unknown_mode_is_refused_rather_than_treated_as_python() -> None:
    """A typo must not silently mean 'no acceleration'."""
    with pytest.raises(ValueError, match="not a valid mode"):
        compile_tape("natve", ())


def test_an_application_without_cors_compiles_no_table() -> None:
    app = Wreath(middleware="native")

    @app.get("/x")
    async def handler(request: Any) -> Response:
        return Response(b"ok")

    app._compile_routes()
    assert app._native_preflight is None


def test_an_unrecognised_middleware_is_declined_not_guessed() -> None:
    class NotCors:
        global_scope = True

        def before_sync(self, request: Any) -> None:
            return None

    assert compile_tape("native", (NotCors(),)) is None


def test_a_middleware_whose_answer_is_not_stable_is_declined() -> None:
    """A recorded answer is only safe if it is the same answer every time."""

    class Drifting(CORSMiddleware):
        def __init__(self) -> None:
            super().__init__(allow_origins=["https://a.test"])
            self._calls = 0

        def before_sync(self, request: Any) -> Any:
            self._calls += 1
            response = super().before_sync(request)
            if response is not None and self._calls % 2 == 0:
                response.headers.append((b"x-drift", str(self._calls).encode()))
            return response

    assert compile_tape("native", (Drifting(),)) is None


@pytest.mark.asyncio
async def test_recompiling_after_removing_cors_drops_the_table() -> None:
    app = build("native")
    assert app._native_preflight is not None
    app._global_middleware = []
    app._dirty = True
    app._compile_routes()
    assert app._native_preflight is None
