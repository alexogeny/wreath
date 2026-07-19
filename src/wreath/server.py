"""Wreath's optional native HTTP/1.1 server facade.

Wreath remains a normal ASGI framework: it runs behind Uvicorn or any other
conforming server without importing this module. ``wreath.server`` is an
*additional* way to serve an ASGI application, moving the HTTP hot path into a
Wreath-owned protocol implementation that runs on top of an asyncio (or uvloop)
transport.

The protocol implementation is selected at import time:

1. ``WREATH_PURE`` set -> the pure-Python reference (``wreath._pure.server``).
2. otherwise the native extension (``wreath._native._server``) when built.
3. falling back to the pure reference if the extension is absent.

Server availability is independent of the framework accelerator ``_core``: a
missing ``_server`` extension never disables JSON, routing, codec, or parser
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
import socket
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
        except BaseException as error:
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

    Returns ``(projector, export)`` where either may be None. The projector is
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

    Returns ``(sink, arm_registry)``, either of which may be None. Both are
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

    ``TLSConfig`` builds the TCP :class:`ssl.SSLContext` and also supplies the
    certificate/key paths to the QUIC backend. Private-key material is never
    extracted from a Python ``SSLContext``.
    """

    certfile: str | os.PathLike[str]
    keyfile: str | os.PathLike[str]
    password: str | None = None

    def build_ssl_context(self, protocols: tuple[HttpProtocolName, ...]) -> SSLContext:
        """Build a server ``SSLContext`` advertising ALPN for the TCP protocols."""
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
    """Warns that a variable declared critical for boot is unset or empty."""


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
                     "read_high_water_messages", "max_ws_fragments"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive")
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
        """Build a configuration from ``WREATH_*`` environment variables.

        Precedence is defaults < environment < explicit ``overrides``. When
        ``env`` is omitted the process environment is read exactly once, via a
        single native ``read_osenv`` crossing; pass a mapping (e.g. a snapshot
        already taken for a required-variable check) to avoid crossing at all.
        An unset or empty variable leaves the field at its default. A value that
        will not coerce raises ``ValueError`` naming the offending variable.
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
    """Return the names in ``required`` that are unset or empty in the env."""
    snapshot = read_osenv() if env is None else env
    return [name for name in required if not snapshot.get(name)]


def configure_from_env(
    env: Mapping[str, str] | None = None,
    *,
    required: Iterable[str] = (),
    warn: bool = True,
    **overrides: Any,
) -> tuple[ServerConfig, list[str]]:
    """Build a :class:`ServerConfig` and report missing critical variables.

    The environment is snapshotted once and reused for both the required-variable
    check and field binding, so the whole boot path costs a single native
    crossing. Names in ``required`` that are unset or empty are returned and,
    when ``warn`` is set, emitted as :class:`EnvConfigWarning`. ``required`` is
    how an app declares boot-critical keys it reads itself (a database DSN, a
    signing secret) that have no ServerConfig field.
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


def _select_tcp_protocol(config: ServerConfig) -> type:
    """Choose the TCP protocol class for the configured protocol set.

    ``h2`` requires the native extension. A combined ``http/1.1``+``h2`` listener
    negotiates via TLS ALPN through ``NegotiatingHttpProtocol``.
    """
    protocols = config.protocols
    wants_h1 = "http/1.1" in protocols
    wants_h2 = "h2" in protocols
    if wants_h2:
        ext = _native_server_module()
        if ext is None or not hasattr(ext, "Http2Protocol"):
            raise RuntimeError(
                "HTTP/2 (h2) requires the native wreath._native._server extension"
            )
        if not wants_h1:
            return cast(type, ext.Http2Protocol)
        return NegotiatingHttpProtocol
    return _select_protocol()


class NegotiatingHttpProtocol(asyncio.Protocol):
    """Selects HTTP/1.1 or HTTP/2 from the TLS ALPN result after the handshake.

    A single TLS listener can serve both protocols: ``connection_made`` reads the
    negotiated ALPN protocol and delegates every subsequent callback to the
    matching native protocol. Missing/unknown/unconfigured ALPN closes the
    transport without invoking ASGI; the first application bytes are never
    inspected to guess the protocol.
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
        if self._delegate is not None:
            self._delegate.data_received(data)

    def eof_received(self) -> bool | None:
        if self._delegate is not None:
            return self._delegate.eof_received()
        return None

    def connection_lost(self, exc: BaseException | None) -> None:
        if self._delegate is not None:
            self._delegate.connection_lost(exc)

    def pause_writing(self) -> None:
        if self._delegate is not None:
            self._delegate.pause_writing()

    def resume_writing(self) -> None:
        if self._delegate is not None:
            self._delegate.resume_writing()


def _http3_available() -> bool:
    """Report whether the optional native HTTP/3 extension is importable.

    The extension is only configured when ``WREATH_BUILD_HTTP3=1`` at build time,
    so a default install returns ``False``. This never imports the module (that
    would fail loudly on a partial build); it only checks discoverability.
    """
    try:
        return importlib.util.find_spec("wreath._native._http3") is not None
    except (ImportError, ValueError):
        return False


def _resolve_tls(
    config: ServerConfig,
    ssl: SSLContext | None,
    tls: TLSConfig | None,
) -> SSLContext | None:
    """Validate the requested protocol/TLS combination and return the TCP context.

    Enforces the plan's startup rules: no silent downgrade for an unavailable
    ``h3`` build, ``h3`` requires a :class:`TLSConfig`, network ``h2``/``h3``
    require TLS, and ``ssl=``/``tls=`` are mutually exclusive.
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

    if wants_h2 and ssl is None and tls is None:
        raise ValueError(
            "HTTP/2 (h2) network serving requires TLS with ALPN; pass tls= or ssl="
        )

    if tls is not None:
        return tls.build_ssl_context(protocols)
    return ssl


class Server:
    """Owns the listening socket, active protocols, lifespan, and shutdown."""

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
        self._signal_handlers_installed = False
        self._date_timer: asyncio.TimerHandle | None = None

    @property
    def sockets(self) -> tuple[Any, ...]:
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
        except BaseException:
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
            factory, local_addr=(config.host, port), reuse_port=False,
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
            except Exception:  # noqa: BLE001
                pass
            self._asyncio_server = None
        if self._datagram_transport is not None:
            self._datagram_transport.close()
            self._datagram_transport = None
        if self._lifespan is not None:
            await self._lifespan.shutdown()
            self._lifespan = None

    async def close(self) -> None:
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
        """Whether anything in ``_protocols`` still owes a response.

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
        await asyncio.shield(self._closed)

    async def serve_forever(self) -> None:
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
        self._signal_handlers_installed = True


async def serve(
    app: ASGIApplication,
    config: ServerConfig | None = None,
    *,
    ssl: SSLContext | None = None,
    tls: TLSConfig | None = None,
) -> Server:
    """Start serving ``app`` and return the running :class:`Server`.

    Does not install process signal handlers. The caller drives shutdown via
    ``server.close()`` / ``server.wait_closed()``.
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
    """Serve ``app`` until interrupted, then gracefully shut down.

    Installs SIGINT/SIGTERM handlers when running in the main thread. Pass an
    explicit ``loop_factory`` to use an optional event-loop implementation;
    Wreath never selects one implicitly.

    When ``config`` is omitted it is built from the environment via
    :func:`configure_from_env`, so ``WREATH_*`` variables take effect. ``required_env``
    names boot-critical variables (a database DSN, a signing secret); any that
    are unset emit an :class:`EnvConfigWarning` before serving. The environment
    is read once whether or not ``config`` is supplied.
    """
    if config is None:
        config, _ = configure_from_env(required=required_env)
    elif required_env:
        _warn_missing_env(missing_required_env(required_env))

    async def _main() -> None:
        server = await serve(app, config, ssl=ssl, tls=tls)
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
        except Exception as exc:
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
        except Exception:
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
        except Exception:  # noqa: BLE001 - shutdown must not raise
            pass
        if not self._task.done():
            self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass


def _open_socket(config: ServerConfig) -> socket.socket:  # pragma: no cover - helper
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((config.host, config.port))
    sock.listen(config.backlog)
    sock.setblocking(False)
    return sock


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
