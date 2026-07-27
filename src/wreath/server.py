"""Wreath's optional native HTTP/1.1 server facade.

Wreath remains a normal ASGI framework: it runs behind Uvicorn or any other
conforming server without importing this module. `wreath.server` is an
*additional* way to serve an ASGI application, moving the HTTP hot path into a
Wreath-owned protocol implementation that runs on top of an asyncio (or uvloop)
transport.

The protocol implementation is selected at import time:

1. `WREATH_PURE` set -> the pure-Python reference (`wreath._pure.server`).
2. otherwise the native extension (`wreath._native._server`) when built.
3. falling back to the pure reference if the extension is absent.

Server availability is independent of the framework accelerator `_core`: a
missing `_server` extension never disables JSON, routing, codec, or parser
acceleration.

This server is **experimental** until fuzzing, sanitizer, soak, and security
review work is complete.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import os
import signal
import warnings
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from email.utils import formatdate
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, cast

from .config import read_osenv
from .inspector import InspectorConfig, InspectorServer, serve_inspector
from .telemetry import Mode, TelemetryConfig

if TYPE_CHECKING:
    from ssl import SSLContext


async def _resume_started_coroutine(coroutine: Any, awaited: Any) -> Any:
    """Resume a coroutine whose first step was run by the native HTTP driver."""
    while True:
        try:
            if awaited is None:
                await asyncio.sleep(0)
                result = None
            else:
                if getattr(awaited, "_asyncio_future_blocking", False):
                    awaited._asyncio_future_blocking = False
                result = await awaited
        except BaseException as error:  # noqa: BLE001 -- forwarded, never swallowed
            # This drives a coroutine the native HTTP layer already started, so
            # it must behave like the event loop it stands in for: *every*
            # exception goes back into the coroutine via `throw`, including
            # `CancelledError`. Narrowing to `Exception` would silently strip
            # cancellation from a request mid-flight.
            try:
                awaited = coroutine.throw(error)
            except StopIteration as completed:
                return completed.value
        else:
            try:
                awaited = coroutine.send(result)
            except StopIteration as completed:
                return completed.value


def _create_recorder(config: ServerConfig) -> Any:
    """Build one native Flight Recorder for a server run, or None.

    Returns None when telemetry is unset, Off, or the native _flight extension is
    not built -- in which case the protocols run with every recorder hook a
    not-taken branch, exactly as before.
    """
    telemetry = config.telemetry
    if telemetry is None or telemetry.mode is Mode.OFF:
        return None
    try:
        flight = importlib.import_module("wreath._native._flight")
    except ImportError:
        return None
    # A Forensic recorder preallocates the capture-slab pool; every other mode
    # passes capture_slabs=0 and reserves nothing (deny-by-default all the way
    # down to the allocation).
    capture_slabs = telemetry.capture_slabs if telemetry.mode is Mode.FORENSIC else 0
    return flight.Recorder(
        int(telemetry.mode),
        ring_records=telemetry.ring_records,
        active_requests=telemetry.active_requests,
        histogram_count=1,
        completion_summaries=telemetry.completion_summaries,
        detailed_sample_rate=telemetry.detailed.rate,
        phase_slots=telemetry.phase_slots,
        detailed_slow_us=telemetry.detailed_slow_us,
        capture_slabs=capture_slabs,
        slab_bytes=telemetry.slab_bytes,
    )


def _create_projector(recorder: Any, config: ServerConfig, app: Any) -> tuple[Any, Any]:
    """Build the off-path projector for a run (and its OTLP export pipeline).

    Returns `(projector, export)` where either may be None. The projector is
    the ring's only consumer: without it a running recorder's completion cells
    accumulate and drop, so one is created whenever a recorder exists. The export
    pipeline is added only when OTLP is enabled and given an endpoint; otherwise
    the projector just feeds the Inspector's projection-backed commands.
    """
    if recorder is None:
        return None, None
    from ._projector import Projector

    telemetry = config.telemetry
    export = None
    on_trace = None
    if (
        telemetry is not None
        and telemetry.otlp.enabled
        and telemetry.otlp.endpoint
    ):
        from ._export import ExportPipeline, OtlpHttpExporter
        from ._flight_metadata import build_metadata_image

        transport = OtlpHttpExporter(
            telemetry.otlp.endpoint, timeout=telemetry.otlp.timeout_seconds
        )
        export = ExportPipeline(
            transport,
            image=build_metadata_image(app),
            queue_capacity=telemetry.otlp.export_queue or 4096,
            batch_size=telemetry.otlp.batch_size or 512,
        )
        on_trace = export.on_trace
    projector = Projector(recorder, on_trace=on_trace)
    if export is not None:
        export.set_snapshot_provider(projector.snapshot)
    return projector, export


def _create_recording(recorder: Any, config: ServerConfig, app: Any) -> tuple[Any, Any]:
    """Build the forensic recording sink and runtime arm registry for a run.

    Returns `(sink, arm_registry)`, either of which may be None. Both are
    Forensic-only: a `RecordingPolicy` is the redaction/memory ceiling the runtime
    arm registry cannot exceed, and (when a path is configured) the async `WFR1`
    sink drains committed capture slabs to an owner-only file off the loop.
    """
    telemetry = config.telemetry
    if (
        recorder is None
        or telemetry is None
        or telemetry.mode is not Mode.FORENSIC
        or config.recording is None
    ):
        return None, None
    from ._flight_metadata import build_metadata_image
    from ._recording_format import RecordingSink
    from .recording import ArmRegistry

    arm_registry = ArmRegistry(config.recording)
    sink = None
    if config.recording_path is not None:
        sink = RecordingSink(
            recorder, build_metadata_image(app), config.recording_path
        )
    return sink, arm_registry

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApplication = Callable[[Scope, Receive, Send], Awaitable[None]]

HttpProtocolName = Literal["http/1.1", "h2", "h3"]

_VALID_PROTOCOLS: frozenset[str] = frozenset({"http/1.1", "h2", "h3"})


@dataclass(frozen=True, slots=True)
class TLSConfig:
    """TLS material for TCP (HTTP/1.1, HTTP/2) and the QUIC backend (HTTP/3).

    `TLSConfig` builds the TCP `ssl.SSLContext` and also supplies the
    certificate/key paths to the QUIC backend. Private-key material is never
    extracted from a Python `SSLContext`.

    That is why `h3` requires this rather than an `ssl=` context: the QUIC
    backend needs the files, and a built `SSLContext` will not give them up.
    Paths are read by the backend at bind time, so they must remain readable for
    the life of the process.

    Args:
        certfile: PEM certificate chain, leaf first.
        keyfile: PEM private key for `certfile`.
        password: Passphrase for an encrypted key; None when the key is unencrypted.
    """

    certfile: str | os.PathLike[str]
    keyfile: str | os.PathLike[str]
    password: str | None = None

    def build_ssl_context(self, protocols: tuple[HttpProtocolName, ...]) -> SSLContext:
        """Build a server `SSLContext` advertising ALPN for the TCP protocols.

        Only `http/1.1` and `h2` reach ALPN; `h3` is negotiated by QUIC and
        is filtered out here. A protocol set with neither TCP protocol produces a
        context that advertises no ALPN at all rather than an empty list.

        Args:
            protocols: The configured protocol set, in preference order.

        Returns:
            A `PROTOCOL_TLS_SERVER` context with the certificate chain loaded.

        Raises:
            OSError: A certificate or key file could not be read.
            ssl.SSLError: The material is malformed, or `password` is wrong.
        """
        import ssl as _ssl

        context = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(
            os.fspath(self.certfile), os.fspath(self.keyfile), self.password
        )
        alpn = [p for p in protocols if p in ("http/1.1", "h2")]
        if alpn:
            context.set_alpn_protocols(alpn)
        return context


class _DefaultResponseHeaders:
    """Mutable date cache owned by one server configuration."""

    __slots__ = ("date", "headers", "server")

    def __init__(self, server: str | None, include_date: bool) -> None:
        self.server = None if server is None else server.encode("ascii")
        self.date = b""
        self.headers: list[tuple[bytes, bytes]] = []
        self.refresh(include_date)

    def refresh(self, include_date: bool) -> None:
        headers: list[tuple[bytes, bytes]] = []
        if self.server is not None:
            headers.append((b"server", self.server))
        if include_date:
            self.date = formatdate(usegmt=True).encode("ascii")
            headers.append((b"date", self.date))
        self.headers[:] = headers


class EnvConfigWarning(UserWarning):
    """Warns that a variable declared critical for boot is unset or empty.

    A warning rather than an error on purpose: only the application knows
    whether it can run degraded, so this reports and lets it decide. Filter it
    to `error` to make a missing variable fatal.
    """


def _env_bool(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"expected a boolean, got {value!r}")


def _env_lifespan(value: str) -> str:
    lowered = value.strip().lower()
    if lowered not in ("auto", "on", "off"):
        raise ValueError(f"expected 'auto', 'on', or 'off', got {value!r}")
    return lowered


def _env_protocols(value: str) -> tuple[str, ...]:
    names = tuple(item.strip() for item in value.split(",") if item.strip())
    if not names:
        raise ValueError("expected a comma-separated protocol list")
    return names


class _EnvSpec(NamedTuple):
    var: str
    field: str
    coerce: Callable[[str], object]


# The single source of truth binding WREATH_* variables to ServerConfig fields.
# Keep this the only place env names are declared; from_env iterates it and the
# .env.example / docs mirror it. HTTP/2+/3 window tuning stays code-only for now
# (rarely operator-set); add specs here when that changes.
_SERVER_ENV_REGISTRY: tuple[_EnvSpec, ...] = (
    _EnvSpec("WREATH_HOST", "host", str),
    _EnvSpec("WREATH_PORT", "port", int),
    _EnvSpec("WREATH_BACKLOG", "backlog", int),
    _EnvSpec("WREATH_KEEP_ALIVE_TIMEOUT", "keep_alive_timeout", float),
    _EnvSpec("WREATH_REQUEST_TIMEOUT", "request_timeout", float),
    _EnvSpec("WREATH_SHUTDOWN_TIMEOUT", "shutdown_timeout", float),
    _EnvSpec("WREATH_SERVER_HEADER", "server_header", str),
    _EnvSpec("WREATH_DATE_HEADER", "date_header", _env_bool),
    _EnvSpec("WREATH_MAX_REQUEST_LINE", "max_request_line", int),
    _EnvSpec("WREATH_MAX_HEADER_COUNT", "max_header_count", int),
    _EnvSpec("WREATH_MAX_HEADER_BYTES", "max_header_bytes", int),
    _EnvSpec("WREATH_MAX_BODY_BYTES", "max_body_bytes", int),
    _EnvSpec("WREATH_LIFESPAN", "lifespan", _env_lifespan),
    _EnvSpec("WREATH_PROTOCOLS", "protocols", _env_protocols),
)


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """Everything one server run is configured with. Frozen; validated at construction.

    Constructing it is the whole validation: a bad port, a non-printable server
    header, a low watermark above its high one, an unknown or duplicated
    protocol name -- each raises here, so a misconfigured deployment fails at
    boot rather than on a request. `from_env` and
    `configure_from_env` build one from `WREATH_*` variables.

    The limits are a request budget, not tuning knobs: each one bounds work a
    single untrusted peer can cause. The pairs exist because a byte count alone
    does not bound work -- an empty WebSocket continuation frame and an empty
    ASGI message both cost dispatch while adding no bytes, so the message and
    fragment counters bound what the byte watermarks cannot.

    Defaults bind `127.0.0.1`, not `0.0.0.0`: reaching the network is a
    decision, not something that happens by omission.

    Args:
        host: Bind address for both the TCP listener and the HTTP/3 UDP socket.
        port: 0 asks the OS for a port; HTTP/3 then binds the one TCP received.
        backlog: Listen backlog. Must be positive.
        keep_alive_timeout: Seconds an idle connection is held between requests.
        request_timeout: Seconds one request may take before the connection is closed.
        shutdown_timeout: Seconds in-flight responses get to drain during a graceful close.
        server_header: Value for the `Server` header. Printable ASCII, or None to send none.
        date_header: Send a `Date` header, refreshed once a second by the running server.
        max_request_line: Bytes in the request line before 414.
        max_header_count: Header fields before 431.
        max_header_bytes: Bytes in the whole head before 431.
        max_body_bytes: Request-body bytes before 413; also caps one WebSocket message.
        read_high_water: Queued request-body bytes before reading is paused.
        read_high_water_messages: Queued ASGI messages before reading is paused.
        response_high_water: Unacknowledged response bytes before ASGI `send` waits.
        response_low_water: Where a waiting `send` resumes. Must be below the high mark.
        response_high_water_segments: Unacknowledged response segments before `send` waits.
        response_low_water_segments: Where a waiting `send` resumes. Must be below the high mark.
        max_ws_fragments: Fragments in one WebSocket message; empty ones count.
        lifespan: `"on"` requires it, `"off"` skips it, `"auto"` runs it if supported.
        protocols: Non-empty, no duplicates, drawn from `http/1.1`, `h2`, `h3`.
        max_concurrent_streams: Concurrent HTTP/2 and HTTP/3 streams per connection.
        initial_stream_window: Initial per-stream flow-control window, in bytes.
        initial_connection_window: Initial per-connection flow-control window, in bytes.
        max_header_list_bytes: Decoded header-list ceiling for HTTP/2 and HTTP/3.
        hpack_table_bytes: HPACK dynamic-table size. 0 disables the dynamic table.
        qpack_table_bytes: QPACK dynamic-table size. 0 disables the dynamic table.
        qpack_blocked_streams: Streams that may block on QPACK state. 0 forbids blocking.

    Raises:
        ValueError: Any bound is out of range, or the protocol set is malformed.
    """

    host: str = "127.0.0.1"
    port: int = 8000
    backlog: int = 2048
    keep_alive_timeout: float = 5.0
    request_timeout: float = 30.0
    shutdown_timeout: float = 10.0
    server_header: str | None = "wreath"
    date_header: bool = True
    max_request_line: int = 8 * 1024
    max_header_count: int = 100
    max_header_bytes: int = 32 * 1024
    max_body_bytes: int = 1 * 1024 * 1024
    read_high_water: int = 256 * 1024
    # Queued ASGI messages may each carry zero payload bytes (an empty
    # WebSocket message, an empty chunk), so `read_high_water` alone cannot
    # bound the queue. This bounds it by count as well; both watermarks apply.
    read_high_water_messages: int = 1024
    # HTTP/3 must retain immutable response segments until the peer acknowledges
    # them. Bound both payload bytes and segment metadata; ASGI send waits above
    # the high watermarks and resumes only after both fall to their low marks.
    response_high_water: int = 1 * 1024 * 1024
    response_low_water: int = 512 * 1024
    response_high_water_segments: int = 1024
    response_low_water_segments: int = 512
    # `max_body_bytes` bounds what a fragmented WebSocket message may hold, but
    # an empty continuation costs parser dispatch and unmasking while adding no
    # bytes, so the byte limit alone cannot bound the work one message causes.
    # This bounds fragments per message; empty fragments count. The default
    # admits a max-size message fragmented at 4 KiB, which is far below what
    # real clients use, and is a pre-1.0 default that may tighten.
    max_ws_fragments: int = 4096
    lifespan: Literal["auto", "on", "off"] = "auto"
    protocols: tuple[HttpProtocolName, ...] = ("http/1.1",)
    max_concurrent_streams: int = 64
    initial_stream_window: int = 65_535
    initial_connection_window: int = 1_048_576
    max_header_list_bytes: int = 32 * 1024
    hpack_table_bytes: int = 4 * 1024
    qpack_table_bytes: int = 4 * 1024
    qpack_blocked_streams: int = 16
    #: Optional Native Flight Recorder configuration. When set to a non-Off
    #: TelemetryConfig (and the native _flight extension is built), the server
    #: creates one recorder and the native protocols emit a completion cell per
    #: request. None (the default) keeps every recorder hook a not-taken branch.
    telemetry: TelemetryConfig | None = None
    #: Optional read-only Inspector socket. Only honored when telemetry created
    #: a recorder; None (the default) binds nothing.
    inspector: InspectorConfig | None = None
    #: Optional forensic recording policy: the startup redaction/memory ceiling.
    #: Only honored under a Forensic recorder; it starts the async WFR1 recording
    #: sink (when ``recording_path`` is set) and the runtime capture-arm registry
    #: the Inspector's capture-control commands install into.
    recording: Any = None
    #: Where the async recording sink writes the owner-only WFR1 file. None keeps
    #: capture in memory only (drained slabs are still recycled, just not stored).
    recording_path: str | None = None
    _default_response_headers: _DefaultResponseHeaders = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        server_header = self.server_header
        if server_header is not None and (
            not server_header
            or any(ord(char) < 0x20 or ord(char) > 0x7E for char in server_header)
        ):
            raise ValueError("server_header must contain printable ASCII")
        if not isinstance(self.date_header, bool):
            raise ValueError("date_header must be bool")
        object.__setattr__(
            self,
            "_default_response_headers",
            _DefaultResponseHeaders(server_header, self.date_header),
        )
        if self.port < 0 or self.port > 65535:
            raise ValueError("port must be in 0..65535")
        if self.backlog < 1:
            raise ValueError("backlog must be positive")
        for name in ("max_request_line", "max_header_count", "max_header_bytes",
                     "max_body_bytes", "read_high_water",
                     "read_high_water_messages", "response_high_water",
                     "response_high_water_segments", "max_ws_fragments"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive")
        for low, high in (
            ("response_low_water", "response_high_water"),
            ("response_low_water_segments", "response_high_water_segments"),
        ):
            if getattr(self, low) < 0:
                raise ValueError(f"{low} must be non-negative")
            if getattr(self, low) >= getattr(self, high):
                raise ValueError(f"{low} must be less than {high}")
        if self.lifespan not in ("auto", "on", "off"):
            raise ValueError("lifespan must be 'auto', 'on', or 'off'")
        self._validate_protocols()
        # All limits are positive except the compression table sizes and the
        # blocked-stream count, which may be zero.
        for name in ("max_concurrent_streams", "initial_stream_window",
                     "initial_connection_window", "max_header_list_bytes"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive")
        for name in ("hpack_table_bytes", "qpack_table_bytes",
                     "qpack_blocked_streams"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")

    def _validate_protocols(self) -> None:
        protocols = self.protocols
        if not isinstance(protocols, tuple):
            raise ValueError("protocols must be a tuple")
        if not protocols:
            raise ValueError("protocols must be non-empty")
        seen: set[str] = set()
        for name in protocols:
            if name not in _VALID_PROTOCOLS:
                raise ValueError(
                    f"unknown protocol {name!r}; expected one of "
                    "'http/1.1', 'h2', 'h3'"
                )
            if name in seen:
                raise ValueError(f"duplicate protocol {name!r}")
            seen.add(name)

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        **overrides: Any,
    ) -> ServerConfig:
        """Build a configuration from `WREATH_*` environment variables.

        Precedence is defaults < environment < explicit `overrides`. When
        `env` is omitted the process environment is read exactly once, via a
        single native `read_osenv` crossing; pass a mapping (e.g. a snapshot
        already taken for a required-variable check) to avoid crossing at all.
        An unset or empty variable leaves the field at its default. A value that
        will not coerce raises `ValueError` naming the offending variable.

        Only the fields in the `WREATH_*` registry are environment-settable;
        the HTTP/2 and HTTP/3 window sizes are deliberately code-only, being
        tuning an operator has no way to choose well from outside.

        Args:
            env: A mapping to read instead of the process environment.
            overrides: Field values that win over both the environment and the defaults.

        Returns:
            A validated configuration.

        Raises:
            ValueError: A variable would not coerce, or the resulting field is out of range.
        """
        snapshot = read_osenv() if env is None else env
        values: dict[str, Any] = {}
        for spec in _SERVER_ENV_REGISTRY:
            raw = snapshot.get(spec.var)
            if not raw:
                continue
            try:
                values[spec.field] = spec.coerce(raw)
            except (ValueError, TypeError) as exc:
                raise ValueError(f"invalid value for {spec.var}: {exc}") from exc
        values.update(overrides)
        return cls(**values)


def _warn_missing_env(missing: Iterable[str]) -> None:
    for name in missing:
        warnings.warn(
            f"required environment variable {name!r} is not set",
            EnvConfigWarning,
            stacklevel=3,
        )


def missing_required_env(
    required: Iterable[str],
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Return the names in `required` that are unset or empty in the env.

    Empty counts as missing: an exported-but-blank variable is a deployment
    mistake, not a configured empty value.

    Args:
        env: A mapping to check instead of the process environment.

    Returns:
        The missing names, in the order given. Empty when nothing is missing.
    """
    snapshot = read_osenv() if env is None else env
    return [name for name in required if not snapshot.get(name)]


def configure_from_env(
    env: Mapping[str, str] | None = None,
    *,
    required: Iterable[str] = (),
    warn: bool = True,
    **overrides: Any,
) -> tuple[ServerConfig, list[str]]:
    """Build a `ServerConfig` and report missing critical variables.

    The environment is snapshotted once and reused for both the required-variable
    check and field binding, so the whole boot path costs a single native
    crossing. Names in `required` that are unset or empty are returned and,
    when `warn` is set, emitted as `EnvConfigWarning`. `required` is
    how an app declares boot-critical keys it reads itself (a database DSN, a
    signing secret) that have no ServerConfig field.

    A missing required variable is reported, never fatal -- the caller decides
    whether to boot without it. A variable that is *present but uncoercible*
    still raises, because that is a typo rather than an absence.

    Args:
        env: A mapping to read instead of the process environment.
        required: Boot-critical names with no `ServerConfig` field of their own.
        warn: Emit an `EnvConfigWarning` per missing name.
        overrides: Passed to `ServerConfig`; they win over the environment.

    Returns:
        `(config, missing)` -- the built configuration and the missing names.

    Raises:
        ValueError: An environment value would not coerce, or a field is out of range.
    """
    snapshot = read_osenv() if env is None else env
    missing = [name for name in required if not snapshot.get(name)]
    if warn:
        _warn_missing_env(missing)
    return ServerConfig.from_env(snapshot, **overrides), missing


def _select_protocol() -> type:
    if os.environ.get("WREATH_PURE"):
        from ._pure.server import HttpProtocol

        return HttpProtocol
    try:
        # importlib keeps the compiled submodule invisible to static analysis
        # while remaining independent of the _core accelerator loader.
        server_ext = importlib.import_module("wreath._native._server")
        return cast(type, server_ext.HttpProtocol)
    except ImportError:
        from ._pure.server import HttpProtocol

        return HttpProtocol


def _native_server_module() -> Any | None:
    if os.environ.get("WREATH_PURE"):
        return None
    try:
        return importlib.import_module("wreath._native._server")
    except ImportError:
        return None


def _require_native_h2() -> Any:
    """The native server extension, or `RuntimeError` naming what is missing.

    Called from `_resolve_tls` at startup, so an `h2` listener that could never
    serve a request is refused before anything binds. Called again from
    `_select_tcp_protocol`, which is the only other way to reach a protocol
    class -- constructing a `Server` directly bypasses `serve`.
    """
    ext = _native_server_module()
    if ext is None or not hasattr(ext, "Http2Protocol"):
        raise RuntimeError(
            "HTTP/2 (h2) requires the native wreath._native._server extension; "
            "build it, or remove 'h2' from config.protocols."
        )
    return ext


def _select_tcp_protocol(config: ServerConfig) -> type:
    """Choose the TCP protocol class for the configured protocol set.

    `h2` requires the native extension, which `serve` has already insisted on;
    the check is repeated here because a `Server` built by hand never went
    through it. A combined `http/1.1`+`h2` listener negotiates via TLS ALPN
    through `NegotiatingHttpProtocol`.
    """
    protocols = config.protocols
    wants_h1 = "http/1.1" in protocols
    wants_h2 = "h2" in protocols
    if wants_h2:
        ext = _require_native_h2()
        if not wants_h1:
            return cast(type, ext.Http2Protocol)
        return NegotiatingHttpProtocol
    return _select_protocol()


class NegotiatingHttpProtocol(asyncio.Protocol):
    """Selects HTTP/1.1 or HTTP/2 from the TLS ALPN result after the handshake.

    A single TLS listener can serve both protocols: `connection_made` reads the
    negotiated ALPN protocol and delegates every subsequent `asyncio.Protocol`
    callback to the matching native protocol.

    The mapping is exact and closed. `h2` selects HTTP/2; `http/1.1` and *no
    ALPN at all* select HTTP/1.1, the latter because a client too old to offer
    ALPN, or a plaintext connection, is an HTTP/1.1 client. Anything else closes
    the transport without invoking ASGI. The first application bytes are never
    inspected to guess a protocol, so a client cannot reach a protocol it did not
    negotiate.

    Selected only for a combined `http/1.1` + `h2` listener, and only when the
    native server extension is present -- a single-protocol listener instantiates
    its protocol directly. If the extension turns out to be missing at connection
    time the transport is closed rather than downgraded.

    Args:
        registry: The server's live-protocol set; the delegate registers itself in it.
        recorder: The run's Flight Recorder, or None. Passed straight to the delegate.
    """

    def __init__(
        self,
        app: ASGIApplication,
        config: ServerConfig,
        loop: asyncio.AbstractEventLoop,
        registry: set[Any],
        recorder: Any = None,
    ) -> None:
        self._app = app
        self._config = config
        self._loop = loop
        self._registry = registry
        self._recorder = recorder
        self._delegate: Any = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Read the ALPN result, build the delegate, and hand it the transport.

        The only callback that decides anything; the rest forward. When no
        delegate is chosen the transport is closed here and every later callback
        becomes a no-op, so a rejected connection reaches neither ASGI nor a
        protocol implementation.
        """
        ssl_object = transport.get_extra_info("ssl_object")
        alpn = ssl_object.selected_alpn_protocol() if ssl_object is not None else None
        ext = _native_server_module()
        if ext is None:
            transport.close()
            return
        if alpn == "h2":
            protocol_cls = ext.Http2Protocol
        elif alpn in ("http/1.1", None):
            # No ALPN (older/plain client) falls back to HTTP/1.1.
            protocol_cls = ext.Http1Protocol
        else:
            transport.close()  # unknown ALPN: never reach ASGI
            return
        self._delegate = protocol_cls(
            self._app, self._config, self._loop, self._registry,
            recorder=self._recorder,
        )
        self._delegate.connection_made(transport)

    def data_received(self, data: bytes) -> None:
        """Forward to the delegate. Discards the bytes when ALPN chose none."""
        if self._delegate is not None:
            self._delegate.data_received(data)

    def eof_received(self) -> bool | None:
        """Forward to the delegate. Returns None with no delegate, closing the transport."""
        if self._delegate is not None:
            return self._delegate.eof_received()
        return None

    def connection_lost(self, exc: BaseException | None) -> None:
        """Forward to the delegate, so it deregisters and unwinds its requests."""
        if self._delegate is not None:
            self._delegate.connection_lost(exc)

    def pause_writing(self) -> None:
        """Forward write backpressure to the delegate."""
        if self._delegate is not None:
            self._delegate.pause_writing()

    def resume_writing(self) -> None:
        """Forward the release of write backpressure to the delegate."""
        if self._delegate is not None:
            self._delegate.resume_writing()


_http3_available_cache: bool | None = None


def _http3_available() -> bool:
    """Report whether the optional native HTTP/3 extension can be loaded.

    The extension is only configured when `WREATH_BUILD_HTTP3=1` at build time,
    so a default install returns `False`. "Available" means *loadable*, not
    merely discoverable: a partial build where the `.so` exists but a
    transitive shared library (e.g. `libngtcp2_crypto_ossl`) is missing must
    report `False` so `serve()` raises its actionable "not built" error
    rather than a raw `ImportError` from deep in the import machinery. The
    import is attempted once and the result cached.
    """
    global _http3_available_cache
    if _http3_available_cache is None:
        if importlib.util.find_spec("wreath._native._http3") is None:
            _http3_available_cache = False
        else:
            try:
                importlib.import_module("wreath._native._http3")
                _http3_available_cache = True
            except (ImportError, ValueError):
                _http3_available_cache = False
    return _http3_available_cache


def _resolve_tls(
    config: ServerConfig,
    ssl: SSLContext | None,
    tls: TLSConfig | None,
) -> SSLContext | None:
    """Validate the requested protocol/TLS combination and return the TCP context.

    Enforces the plan's startup rules: no silent downgrade for an unavailable
    `h2` or `h3` build, `h3` requires a `TLSConfig`, network `h2`/`h3`
    require TLS, and `ssl=`/`tls=` are mutually exclusive.

    Both extension checks live here rather than where the protocol object is
    built, because a missing extension is a deployment fact and not a property
    of one connection: checked per connection it starts a server that refuses
    every request it is ever offered, which is the failure a startup check
    exists to turn into a refusal to start.
    """
    if ssl is not None and tls is not None:
        raise ValueError("pass either ssl= or tls=, not both")

    protocols = config.protocols
    wants_h2 = "h2" in protocols
    wants_h3 = "h3" in protocols

    if wants_h3:
        if tls is None:
            raise ValueError("HTTP/3 (h3) requires a TLSConfig via tls=")
        if not _http3_available():
            # Never silently downgrade to a TCP-only server.
            raise RuntimeError(
                "HTTP/3 (h3) was requested but the native wreath._native._http3 "
                "extension is not built. Rebuild with WREATH_BUILD_HTTP3=1 and the "
                "ngtcp2/nghttp3 backend, or remove 'h3' from config.protocols."
            )

    if wants_h2:
        _require_native_h2()
        if ssl is None and tls is None:
            raise ValueError(
                "HTTP/2 (h2) network serving requires TLS with ALPN; pass tls= or ssl="
            )

    if tls is not None:
        return tls.build_ssl_context(protocols)
    return ssl


class Server:
    """Owns the listening socket, active protocols, lifespan, and shutdown.

    Constructed by `serve`, not directly: construction only wires the
    object up, and it is `serve` that binds and starts it. What a caller
    does with the result is drive it -- `serve_forever` to block,
    `close` to shut down, `wait_closed` to join.

    Startup is atomic. If any requested listener, the lifespan, the recorder
    pipeline, or the Inspector fails to come up -- including by cancellation --
    everything already created is torn down and the original error propagates. A
    half-bound server is never returned.

    Shutdown is graceful and ordered: the Inspector stops reading first, the
    listener stops accepting, live protocols are told to finish their current
    requests, in-flight work drains until `shutdown_timeout`, remaining
    transports are closed, the telemetry threads are joined off the loop, and
    the ASGI lifespan shutdown runs last.
    """

    def __init__(
        self,
        app: ASGIApplication,
        config: ServerConfig,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._app = app
        self._config = config
        self._loop = loop
        self._recorder = _create_recorder(config)
        self._projector: Any = None
        self._export: Any = None
        self._recording_sink: Any = None
        self._arm_registry: Any = None
        self._inspector: InspectorServer | None = None
        self._protocols: set[Any] = set()
        self._asyncio_server: asyncio.AbstractServer | None = None
        self._datagram_transport: asyncio.DatagramTransport | None = None
        self._lifespan: _LifespanManager | None = None
        self._closed = loop.create_future()
        self._closing = False
        self._date_timer: asyncio.TimerHandle | None = None

    @property
    def sockets(self) -> tuple[Any, ...]:
        """Bound TCP sockets, or empty when no TCP listener exists.

        The supported way to learn the port after binding `port=0`:
        `server.sockets[0].getsockname()[1]`. Empty for an `h3`-only server,
        which binds UDP -- see `datagram_addresses`.
        """
        server = self._asyncio_server
        sockets = getattr(server, "sockets", None)
        if not sockets:
            return ()
        return tuple(sockets)

    @property
    def datagram_addresses(self) -> tuple[Any, ...]:
        """Bound UDP addresses for HTTP/3 endpoints (empty when h3 is disabled)."""
        transport = self._datagram_transport
        if transport is None:
            return ()
        sock = transport.get_extra_info("socket")
        if sock is None:
            return ()
        return (sock.getsockname(),)

    @property
    def recorder(self) -> Any:
        """The native Flight Recorder for this run, or None when telemetry is off."""
        return self._recorder

    def _protocol_factory(self) -> Any:
        protocol_cls = _select_tcp_protocol(self._config)
        return protocol_cls(
            self._app, self._config, self._loop, self._protocols,
            recorder=self._recorder,
        )

    async def _start(
        self, ssl: SSLContext | None, tls: TLSConfig | None = None
    ) -> None:
        config = self._config
        protocols = config.protocols
        wants_tcp = "http/1.1" in protocols or "h2" in protocols
        wants_udp = "h3" in protocols
        if config.lifespan != "off":
            self._lifespan = _LifespanManager(self._app, self._loop)
            await self._lifespan.startup(required=config.lifespan == "on")

        # Startup is atomic: if any requested listener fails, tear everything
        # already created down and re-raise.
        try:
            if config.date_header:
                self._refresh_date_header()
            port = config.port
            if wants_tcp:
                self._asyncio_server = await self._loop.create_server(
                    self._protocol_factory,
                    host=config.host,
                    port=config.port,
                    backlog=config.backlog,
                    ssl=ssl,
                    reuse_address=True,
                    reuse_port=bool(getattr(self._loop, "_wreath_reuse_port", False)),
                )
                # When port==0, bind UDP to the OS-assigned TCP port.
                if wants_udp and self.sockets:
                    port = self.sockets[0].getsockname()[1]
            if wants_udp:
                assert tls is not None  # enforced by _resolve_tls
                self._datagram_transport = await self._bind_datagram(tls, port)
            if self._recorder is not None:
                self._projector, self._export = _create_projector(
                    self._recorder, config, self._app
                )
                if self._projector is not None:
                    self._projector.start()
                if self._export is not None:
                    self._export.start()
                self._recording_sink, self._arm_registry = _create_recording(
                    self._recorder, config, self._app
                )
                if self._recording_sink is not None:
                    self._recording_sink.start()
                # Install the compiled capture plan + arm registry on the app so
                # its request-path seam can capture per policy. Only a Wreath app
                # carries the seam; a bare ASGI app simply lacks the method.
                setter = getattr(self._app, "_set_flight_recording", None)
                if setter is not None and self._arm_registry is not None:
                    from .recording import compile_redaction

                    setter(compile_redaction(config.recording.redaction),
                           self._arm_registry)
            if config.inspector is not None and self._recorder is not None:
                self._inspector = await serve_inspector(
                    self._recorder, self._app, config.inspector,
                    projector=self._projector,
                    arm_registry=self._arm_registry,
                )
        except BaseException:  # re-raised; cleanup must be total
            # Broad *and* re-raised. A half-bound server -- listener up, lifespan
            # not, or an inspector serving against a recorder that never started
            # -- is worse than a failed start, and a start cancelled partway
            # leaves exactly that. `except Exception` would miss the cancellation
            # and leak the sockets. Nothing is swallowed.
            await self._abort_startup()
            raise

    async def _bind_datagram(
        self, tls: TLSConfig, port: int
    ) -> asyncio.DatagramTransport:
        ext = importlib.import_module("wreath._native._http3")
        config = self._config

        def factory() -> Any:
            return ext.DatagramEndpoint(
                self._app, config, self._loop, self._protocols,
                os.fspath(tls.certfile), os.fspath(tls.keyfile), tls.password,
                self._recorder,
            )

        transport, _protocol = await self._loop.create_datagram_endpoint(
            factory,
            local_addr=(config.host, port),
            reuse_port=bool(getattr(self._loop, "_wreath_reuse_port", False)),
        )
        return transport

    def _refresh_date_header(self) -> None:
        self._config._default_response_headers.refresh(True)
        self._date_timer = self._loop.call_later(1.0, self._refresh_date_header)

    def _cancel_date_timer(self) -> None:
        if self._date_timer is not None:
            self._date_timer.cancel()
            self._date_timer = None

    async def _stop_projection(self) -> None:
        """Stop the export pipeline then the projector, joining their threads off
        the event loop. The export pipeline goes first so its final flush can
        still pull from a projector that is about to do its own last drain."""
        export, self._export = self._export, None
        if export is not None:
            await self._loop.run_in_executor(None, export.stop)
        projector, self._projector = self._projector, None
        if projector is not None:
            await self._loop.run_in_executor(None, projector.stop)
        # Clear the app's capture seam first so no request captures into a slab
        # pool that is about to be torn down.
        clearer = getattr(self._app, "_set_flight_recording", None)
        if clearer is not None and self._arm_registry is not None:
            clearer(None, None)
        # The recording sink is an independent capture-slab consumer; stop it off
        # the loop too, so its final drain + WFR1 footer land before teardown.
        sink, self._recording_sink = self._recording_sink, None
        self._arm_registry = None
        if sink is not None:
            await self._loop.run_in_executor(None, sink.stop)

    async def _abort_startup(self) -> None:
        self._cancel_date_timer()
        if self._inspector is not None:
            await self._inspector.close()
            self._inspector = None
        await self._stop_projection()
        if self._asyncio_server is not None:
            self._asyncio_server.close()
            try:
                await self._asyncio_server.wait_closed()
            except Exception:  # noqa: BLE001 -- must not mask the abort's cause
                # This runs *while unwinding*: every caller re-raises the failure
                # that brought us here. An exception escaping this cleanup would
                # replace that cause with a shutdown detail, which is strictly
                # less useful to whoever reads the traceback. Narrowing is the
                # wrong trade here -- the broad catch is what preserves the
                # original error, not what hides one.
                pass
            self._asyncio_server = None
        if self._datagram_transport is not None:
            self._datagram_transport.close()
            self._datagram_transport = None
        if self._lifespan is not None:
            await self._lifespan.shutdown()
            self._lifespan = None

    async def close(self) -> None:
        """Shut the server down gracefully, then resolve `wait_closed`.

        Concurrency-safe and idempotent: a second call while the first is running
        does not restart the sequence, it waits for it.

        The ordering is the contract, and each step exists because the one before
        it must have finished. The Inspector closes first so nothing reads a
        recorder mid-teardown. The listener stops accepting, then live protocols
        are asked to stop accepting new *requests* on connections they already
        hold. In-flight responses then drain, polled until `shutdown_timeout`
        expires; the HTTP/3 endpoint is deliberately not counted as work, because
        it is a listener rather than a connection and waiting on it would drain
        nothing and spend the whole timeout. Remaining transports are closed,
        the UDP endpoint after them, the projector and export threads are joined
        off the loop so a final drain captures the last completions, and the ASGI
        lifespan shutdown runs last -- after every request that might have used
        what it tears down.

        Returns once that sequence completes. Requests still running at the end
        of the drain window are cut off, not waited for.
        """
        if self._closing:
            await self.wait_closed()
            return
        self._closing = True
        self._cancel_date_timer()
        config = self._config

        # 0. The Inspector goes first: no reads of a recorder mid-teardown.
        if self._inspector is not None:
            await self._inspector.close()
            self._inspector = None

        # 1. Stop accepting connections (TCP) and reject new QUIC connections.
        server = self._asyncio_server
        if server is not None:
            server.close()

        # 2. Ask active protocols to stop accepting new requests.
        for protocol in list(self._protocols):
            stop = getattr(protocol, "stop_accepting", None)
            if stop is not None:
                stop()

        # 3. Allow active responses to drain until shutdown_timeout.
        deadline = self._loop.time() + config.shutdown_timeout
        while self._has_work_to_drain() and self._loop.time() < deadline:  # noqa: ASYNC110
            await asyncio.sleep(0.02)

        # 4. Close remaining transports.
        for protocol in list(self._protocols):
            shutdown = getattr(protocol, "shutdown", None)
            if shutdown is not None:
                shutdown()

        # 5. Wait for protocol teardown.
        drain_deadline = self._loop.time() + 1.0
        while self._protocols and self._loop.time() < drain_deadline:  # noqa: ASYNC110
            await asyncio.sleep(0.01)

        if server is not None:
            await server.wait_closed()

        # 5b. Close the UDP/HTTP-3 endpoint.
        if self._datagram_transport is not None:
            self._datagram_transport.close()
            self._datagram_transport = None

        # 5c. Stop the projector/export threads. Requests have drained, so a final
        # drain here captures the last completions before their ring is dropped.
        await self._stop_projection()

        # 6. Run lifespan shutdown.
        if self._lifespan is not None:
            await self._lifespan.shutdown()

        # 7. Resolve wait_closed().
        if not self._closed.done():
            self._closed.set_result(None)

    def _has_work_to_drain(self) -> bool:
        """Whether anything in `_protocols` still owes a response.

        A TCP protocol *is* one connection, so its presence in the set is the
        work, and it leaves once the connection is done. The HTTP/3 endpoint is
        a listener rather than a connection: one object serves every QUIC
        connection on the UDP socket, and it only leaves the set when the socket
        closes -- in step 5b, after this loop. Counting it as work meant a
        graceful close of any HTTP/3 server waited out the entire
        shutdown_timeout and drained nothing, because the thing it was waiting
        for could not go away until it had finished waiting. It reports what it
        actually holds instead.
        """
        for protocol in self._protocols:
            active = getattr(protocol, "active_requests", None)
            if active is None or active:
                return True
        return False

    async def wait_closed(self) -> None:
        """Wait until `close` has finished. Does not itself start a shutdown.

        Shielded, so cancelling the waiter does not cancel the shutdown a
        different task is running. Awaitable from any number of tasks.
        """
        await asyncio.shield(self._closed)

    async def serve_forever(self) -> None:
        """Wait for shutdown, and convert a cancellation into a graceful one.

        Unlike `wait_closed`, cancelling this *does* shut the server down:
        it closes gracefully first, then re-raises `CancelledError`. That makes
        it the coroutine to put in a task group or under `asyncio.timeout`.
        """
        try:
            await self.wait_closed()
        except asyncio.CancelledError:
            await self.close()
            raise

    def _install_signal_handlers(self) -> None:
        def handler() -> None:
            self._loop.create_task(self.close())

        for signame in (signal.SIGINT, signal.SIGTERM):
            try:
                self._loop.add_signal_handler(signame, handler)
            except (NotImplementedError, RuntimeError):
                return


async def serve(
    app: ASGIApplication,
    config: ServerConfig | None = None,
    *,
    ssl: SSLContext | None = None,
    tls: TLSConfig | None = None,
) -> Server:
    """Start serving `app` and return the running `Server`.

    Returns only once every requested listener is bound and the ASGI lifespan
    startup has completed, so a caller may send a request the instant it
    returns. If anything fails to come up nothing is left running and the error
    propagates; there is no partly-started server to clean up.

    Does not install process signal handlers -- use `run` for that. The
    caller drives shutdown via `server.close()` / `server.wait_closed()`.

    `ssl` and `tls` are alternatives, not layers: `tls` builds the context
    *and* supplies the certificate paths HTTP/3 needs, so `h3` requires it.

    A missing extension for any requested protocol is refused here rather than
    silently downgrading: `h3` without `wreath._native._http3` and `h2` without
    `wreath._native._server` both raise before anything binds. Neither can start
    a server that would refuse every connection it is offered.

    Args:
        config: Defaults to a plain `ServerConfig`; the environment is not read here.
        ssl: A ready `SSLContext`. Sufficient for TCP, but not for `h3`.
        tls: Certificate and key paths. Required for `h3`.

    Returns:
        The running server.

    Raises:
        ValueError: Both `ssl` and `tls` were passed, or `h3`/`h2` lacks the TLS it needs.
        RuntimeError: `h2` or `h3` was requested and its native extension is not built.
    """
    config = config or ServerConfig()
    ssl_context = _resolve_tls(config, ssl, tls)
    loop = asyncio.get_running_loop()
    server = Server(app, config, loop)
    await server._start(ssl_context, tls)
    return server


def run(
    app: ASGIApplication,
    config: ServerConfig | None = None,
    *,
    ssl: SSLContext | None = None,
    tls: TLSConfig | None = None,
    loop_factory: Callable[[], asyncio.AbstractEventLoop] | None = None,
    required_env: Iterable[str] = (),
) -> None:
    """Serve `app` until interrupted, then gracefully shut down.

    Installs SIGINT/SIGTERM handlers when running in the main thread. Pass an
    explicit `loop_factory` to use an optional event-loop implementation;
    Wreath never selects one implicitly.

    When `config` is omitted it is built from the environment via
    `configure_from_env`, so `WREATH_*` variables take effect. `required_env`
    names boot-critical variables (a database DSN, a signing secret); any that
    are unset emit an `EnvConfigWarning` before serving. The environment
    is read once whether or not `config` is supplied.

    Returns when the graceful shutdown has finished. A `KeyboardInterrupt` that
    outruns the signal handler is caught rather than propagated, so an
    interactive Ctrl-C exits cleanly instead of printing a traceback. Off the
    main thread the platform does not permit signal handlers and they are skipped
    without complaint; `serve` is the entry point for embedding a server in
    a loop somebody else owns.

    Args:
        config: Built from the environment when omitted.
        loop_factory: Passed to `asyncio.run`, e.g. `uvloop.new_event_loop`.
        required_env: Boot-critical variable names to warn about when unset.
    """
    if config is None:
        config, _ = configure_from_env(required=required_env)
    elif required_env:
        _warn_missing_env(missing_required_env(required_env))

    async def _main() -> None:
        server = await serve(app, config, ssl=ssl, tls=tls)
        ready_fd_text = os.environ.pop("_WREATH_WORKER_READY_FD", None)
        if ready_fd_text is not None:
            ready_fd = int(ready_fd_text)
            try:
                os.write(ready_fd, b"1")
            finally:
                os.close(ready_fd)
        try:
            server._install_signal_handlers()
        except ValueError:
            # Not on the main thread; skip signal handling.
            pass
        try:
            await server.serve_forever()
        except asyncio.CancelledError:
            pass
        finally:
            await server.close()

    try:
        asyncio.run(_main(), loop_factory=loop_factory)
    except KeyboardInterrupt:
        pass


class _LifespanManager:
    """Runs the ASGI lifespan protocol in a background task."""

    def __init__(self, app: ASGIApplication, loop: asyncio.AbstractEventLoop) -> None:
        self._app = app
        self._loop = loop
        self._receive_queue: asyncio.Queue[Message] = asyncio.Queue()
        self._startup_event = loop.create_future()
        self._shutdown_event = loop.create_future()
        self._task: asyncio.Task[None] | None = None
        self._supported = True

    async def _receive(self) -> Message:
        return await self._receive_queue.get()

    async def _send(self, message: Message) -> None:
        message_type = message["type"]
        if message_type == "lifespan.startup.complete":
            if not self._startup_event.done():
                self._startup_event.set_result(None)
        elif message_type == "lifespan.startup.failed":
            if not self._startup_event.done():
                self._startup_event.set_exception(
                    RuntimeError(message.get("message", "lifespan startup failed"))
                )
        elif message_type == "lifespan.shutdown.complete":
            if not self._shutdown_event.done():
                self._shutdown_event.set_result(None)
        elif message_type == "lifespan.shutdown.failed":
            if not self._shutdown_event.done():
                self._shutdown_event.set_exception(
                    RuntimeError(message.get("message", "lifespan shutdown failed"))
                )

    async def _main(self) -> None:
        scope = {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.5"}}
        try:
            await self._app(scope, self._receive, self._send)
        except Exception as exc:  # noqa: BLE001 -- routed to the awaiting future
            # `_main` is a task nobody awaits directly, so an exception escaping
            # here would surface as an unretrieved-task warning and `startup()`
            # would hang forever on a future nothing ever resolves. The catch
            # exists to *move* the failure to the caller, not to hide it: it is
            # deliberately `Exception`, so a `CancelledError` still propagates
            # and cancels the task rather than being reported as app failure.
            self._supported = False
            if not self._startup_event.done():
                self._startup_event.set_exception(exc)
            if not self._shutdown_event.done():
                self._shutdown_event.set_result(None)

    async def startup(self, required: bool) -> None:
        self._task = self._loop.create_task(self._main())
        await self._receive_queue.put({"type": "lifespan.startup"})
        try:
            await self._startup_event
        except Exception:  # conditionally re-raised just below
            # Not a swallow: the whole body is a decision about whether to
            # re-raise, and the failing case does. Broad because `_main` puts
            # whatever the app raised onto this future, and any of it means the
            # same thing here -- lifespan did not come up.
            # In "auto" mode an app that rejects lifespan may run without it,
            # but a genuine startup *failure* must abort. We can only
            # distinguish "unsupported" (app raised before consuming) via the
            # supported flag.
            if required or self._supported:
                raise

    async def shutdown(self) -> None:
        if self._task is None:
            return
        if not self._supported:
            await self._task
            return
        await self._receive_queue.put({"type": "lifespan.shutdown"})
        try:
            await asyncio.wait_for(self._shutdown_event, timeout=10.0)
        except (TimeoutError, RuntimeError):
            # The only two outcomes this await has besides success: the app never
            # answered (TimeoutError), or it answered `lifespan.shutdown.failed`,
            # which `_receive` turns into exactly this RuntimeError. Shutdown
            # continues either way -- but naming them means a *third* outcome
            # would now surface instead of being quietly absorbed.
            pass
        if not self._task.done():
            self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            # We cancelled it on the line above; reaping our own cancellation is
            # expected. `_main` already routes an app exception into
            # `_startup_event`/`_shutdown_event`, so nothing else escapes here.
            pass


__all__ = [
    "ASGIApplication",
    "EnvConfigWarning",
    "HttpProtocolName",
    "NegotiatingHttpProtocol",
    "Server",
    "ServerConfig",
    "TLSConfig",
    "configure_from_env",
    "missing_required_env",
    "run",
    "serve",
]
