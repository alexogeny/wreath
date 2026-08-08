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
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime, parsedate_to_datetime
from typing import Any

import pytest

from wreath import Response, Wreath
from wreath.middleware.cors import CORSMiddleware
from wreath.middleware.tape import compile_tape
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


DATE_TOLERANCE = timedelta(seconds=2)


def _is_date(line: bytes) -> bool:
    return line.lower().startswith(b"date:")


def _blank_value(line: bytes) -> bytes:
    """Replace a header's value, keeping its name, colon and spacing verbatim."""
    value = line.partition(b":")[2].lstrip()
    return line[: len(line) - len(value)] + b"<normalized>"


def comparable(response: bytes) -> tuple[list[bytes], bytes]:
    """Split a response into header lines and body, blanking only `Date` values.

    Everything else stays byte-for-byte, and so does the `Date` header's own
    name, spelling, spacing and position among the headers -- a `Date:` on one
    side against a `date:` on the other is still a mismatch. The value itself is
    the one thing the two `serve()` calls cannot share, because each reads its
    own clock; `assert_same_wire_bytes` checks it instead of discarding it.
    """
    head, _, body = response.partition(b"\r\n\r\n")
    lines = [_blank_value(line) if _is_date(line) else line for line in head.split(b"\r\n")]
    return lines, body


def _http_dates(response: bytes) -> list[datetime]:
    """Every `Date` value in the head, parsed as an RFC 9110 IMF-fixdate."""
    head, _, _ = response.partition(b"\r\n\r\n")
    values = [line.partition(b":")[2].strip() for line in head.split(b"\r\n") if _is_date(line)]
    return [_parse_http_date(value) for value in values]


def _parse_http_date(value: bytes) -> datetime:
    text = value.decode("ascii", "replace")
    try:
        parsed = parsedate_to_datetime(text)
    except ValueError as exc:  # a malformed value is a parity failure, not an error
        raise AssertionError(f"not an HTTP-date: {text!r}") from exc
    assert parsed.tzinfo == UTC, f"HTTP-date must be GMT: {text!r}"
    assert format_datetime(parsed, usegmt=True) == text, f"not IMF-fixdate: {text!r}"
    return parsed


def assert_same_wire_bytes(python: bytes, native: bytes) -> None:
    """Assert the two modes wrote the same bytes, allowing only a clock tick.

    The `Date` values are compared as instants within `DATE_TOLERANCE` rather
    than as bytes; every other byte, including the `Date` header's name and
    position, must match exactly.
    """
    assert comparable(python) == comparable(native)
    python_dates = _http_dates(python)
    native_dates = _http_dates(native)
    assert python_dates, "expected a Date header to compare"
    for left, right in zip(python_dates, native_dates, strict=True):
        assert abs(left - right) <= DATE_TOLERANCE, f"{left} vs {right}"


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
    """Opting in changes no byte but the instant in an independent `Date` read."""
    python = await serve(build("python"), raw)
    native = await serve(build("native"), raw)
    assert_same_wire_bytes(python, native)


@pytest.mark.asyncio
async def test_a_wildcard_configuration_matches_too() -> None:
    """A wildcard origin is answered with the same bytes, `Date` value included."""
    raw = request(origin="https://anything.test", access_control_request_method="POST")
    python = await serve(build("python", allow_origins=["*"]), raw)
    native = await serve(build("native", allow_origins=["*"]), raw)
    assert_same_wire_bytes(python, native)
    assert b"access-control-allow-origin: *" in native.lower()


@pytest.mark.asyncio
async def test_credentials_and_headers_configuration_matches() -> None:
    """Credentials, allowed headers and max-age match byte for byte too."""
    config = {
        "allow_origins": ["https://a.test"],
        "allow_credentials": True,
        "allow_headers": ["x-token"],
        "max_age": 99,
    }
    raw = request(origin="https://a.test", access_control_request_method="GET")
    assert_same_wire_bytes(
        await serve(build("python", **config), raw),
        await serve(build("native", **config), raw),
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
