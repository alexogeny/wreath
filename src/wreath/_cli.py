"""Dependency-free implementation helpers for Wreath's command-line interface."""

from __future__ import annotations

import argparse
import importlib
import inspect
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, Literal, cast

from .server import ASGIApplication, ServerConfig, TLSConfig, run

LoopName = Literal["asyncio", "uvloop"]


class CliError(Exception):
    """An expected command-line failure with a stable process exit code."""

    def __init__(self, message: str, *, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True, slots=True)
class RunOptions:
    """Validated command-line values used to configure one server process."""

    command: Literal["run", "dev"]
    target: str
    factory: bool
    host: str
    port: int
    backlog: int
    keep_alive_timeout: float
    request_timeout: float
    shutdown_timeout: float
    server_header: str | None
    date_header: bool
    max_request_line: int
    max_header_count: int
    max_header_bytes: int
    max_body_bytes: int
    read_high_water: int
    read_high_water_messages: int
    max_ws_fragments: int
    lifespan: Literal["auto", "on", "off"]
    protocols: tuple[str, ...]
    max_concurrent_streams: int
    initial_stream_window: int
    initial_connection_window: int
    max_header_list_bytes: int
    hpack_table_bytes: int
    qpack_table_bytes: int
    qpack_blocked_streams: int
    loop: LoopName
    tls_cert: str | None
    tls_key: str | None
    tls_password_file: str | None
    reload_dirs: tuple[str, ...] = ()
    reload_includes: tuple[str, ...] = ("*.py",)
    reload_excludes: tuple[str, ...] = ()
    reload_delay: float = 0.25
    reload_debounce: float = 0.10

    def server_config(self) -> ServerConfig:
        """Build the server's authoritative validated configuration."""
        try:
            return ServerConfig(
                host=self.host,
                port=self.port,
                backlog=self.backlog,
                keep_alive_timeout=self.keep_alive_timeout,
                request_timeout=self.request_timeout,
                shutdown_timeout=self.shutdown_timeout,
                server_header=self.server_header,
                date_header=self.date_header,
                max_request_line=self.max_request_line,
                max_header_count=self.max_header_count,
                max_header_bytes=self.max_header_bytes,
                max_body_bytes=self.max_body_bytes,
                read_high_water=self.read_high_water,
                read_high_water_messages=self.read_high_water_messages,
                max_ws_fragments=self.max_ws_fragments,
                lifespan=self.lifespan,
                protocols=cast(Any, self.protocols),
                max_concurrent_streams=self.max_concurrent_streams,
                initial_stream_window=self.initial_stream_window,
                initial_connection_window=self.initial_connection_window,
                max_header_list_bytes=self.max_header_list_bytes,
                hpack_table_bytes=self.hpack_table_bytes,
                qpack_table_bytes=self.qpack_table_bytes,
                qpack_blocked_streams=self.qpack_blocked_streams,
            )
        except ValueError as error:
            raise CliError(str(error), exit_code=2) from error

    def tls_config(self) -> TLSConfig | None:
        """Build TLS configuration, reading any password outside process argv."""
        if self.tls_cert is None:
            return None
        password = None
        if self.tls_password_file is not None:
            try:
                password = Path(self.tls_password_file).read_text().rstrip("\r\n")
            except OSError as error:
                raise CliError(
                    f"could not read TLS password file {self.tls_password_file!r}: {error}",
                    exit_code=2,
                ) from error
        return TLSConfig(self.tls_cert, cast(str, self.tls_key), password)


def _version() -> str:
    try:
        value = metadata.version("wreath")
    except metadata.PackageNotFoundError:
        value = "unknown"
    return f"wreath {value}"


def _add_server_arguments(parser: argparse.ArgumentParser) -> None:
    defaults = ServerConfig()
    parser.add_argument("target", metavar="MODULE[:ATTRIBUTE]")
    parser.add_argument(
        "--factory",
        action="store_true",
        help="invoke the target as a zero-argument application factory",
    )
    parser.add_argument("--host", default=defaults.host)
    parser.add_argument("--port", type=int, default=defaults.port)
    parser.add_argument("--backlog", type=int, default=defaults.backlog)
    parser.add_argument("--keep-alive-timeout", type=float, default=defaults.keep_alive_timeout)
    parser.add_argument("--request-timeout", type=float, default=defaults.request_timeout)
    parser.add_argument("--shutdown-timeout", type=float, default=defaults.shutdown_timeout)
    parser.add_argument("--server-header", default=defaults.server_header)
    parser.add_argument(
        "--no-server-header", action="store_const", const=None, dest="server_header"
    )
    parser.add_argument(
        "--date-header", action=argparse.BooleanOptionalAction, default=defaults.date_header
    )
    parser.add_argument("--max-request-line", type=int, default=defaults.max_request_line)
    parser.add_argument("--max-header-count", type=int, default=defaults.max_header_count)
    parser.add_argument("--max-header-bytes", type=int, default=defaults.max_header_bytes)
    parser.add_argument("--max-body-bytes", type=int, default=defaults.max_body_bytes)
    parser.add_argument("--read-high-water", type=int, default=defaults.read_high_water)
    parser.add_argument(
        "--read-high-water-messages", type=int, default=defaults.read_high_water_messages
    )
    parser.add_argument("--max-ws-fragments", type=int, default=defaults.max_ws_fragments)
    parser.add_argument(
        "--lifespan", choices=("auto", "on", "off"), default=defaults.lifespan
    )
    parser.add_argument(
        "--protocol",
        action="append",
        choices=("http/1.1", "h2", "h3"),
        dest="protocols",
        help="enabled protocol; repeat to enable more than one",
    )
    parser.add_argument(
        "--max-concurrent-streams", type=int, default=defaults.max_concurrent_streams
    )
    parser.add_argument(
        "--initial-stream-window", type=int, default=defaults.initial_stream_window
    )
    parser.add_argument(
        "--initial-connection-window", type=int, default=defaults.initial_connection_window
    )
    parser.add_argument(
        "--max-header-list-bytes", type=int, default=defaults.max_header_list_bytes
    )
    parser.add_argument("--hpack-table-bytes", type=int, default=defaults.hpack_table_bytes)
    parser.add_argument("--qpack-table-bytes", type=int, default=defaults.qpack_table_bytes)
    parser.add_argument(
        "--qpack-blocked-streams", type=int, default=defaults.qpack_blocked_streams
    )
    parser.add_argument("--loop", choices=("asyncio", "uvloop"), default="asyncio")
    parser.add_argument("--tls-cert", metavar="PATH")
    parser.add_argument("--tls-key", metavar="PATH")
    parser.add_argument(
        "--tls-password-file",
        metavar="PATH",
        help="read the private-key password from a file instead of process argv",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wreath", description="Run ASGI applications with Wreath")
    parser.add_argument("--version", action="version", version=_version())
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser(
        "run", help="serve an ASGI application in one foreground process"
    )
    _add_server_arguments(run_parser)
    dev_parser = commands.add_parser(
        "dev", help="serve an ASGI application and reload after source changes"
    )
    _add_server_arguments(dev_parser)
    dev_parser.add_argument("--reload-dir", action="append", default=[])
    dev_parser.add_argument(
        "--reload-include", action="append", dest="reload_includes", default=[]
    )
    dev_parser.add_argument(
        "--reload-exclude", action="append", dest="reload_excludes", default=[]
    )
    dev_parser.add_argument("--reload-delay", type=float, default=0.25)
    dev_parser.add_argument("--reload-debounce", type=float, default=0.10)
    typegen_parser = commands.add_parser(
        "typegen", help="generate consumer type contracts from typed routes"
    )
    typegen_parser.add_argument("target", help="application target as module:attribute")
    typegen_parser.add_argument("--target", dest="typegen_target", default="typescript",
                                choices=("typescript",), metavar="TARGET",
                                help="output target (default: typescript)")
    typegen_parser.add_argument("--output", required=True, metavar="PATH")
    typegen_parser.add_argument("--react-query", action="store_true")
    typegen_parser.add_argument("--base-url-env", metavar="NAME", default=None)
    typegen_parser.add_argument("--check", action="store_true")
    typegen_parser.add_argument("--title", default="Wreath")
    typegen_parser.add_argument("--api-version", default="0.1.0")
    strictness = typegen_parser.add_mutually_exclusive_group()
    strictness.add_argument("--strict", dest="allow_unknown", action="store_false",
                            default=False)
    strictness.add_argument("--allow-unknown", dest="allow_unknown", action="store_true")
    typegen_parser.add_argument("--pure", action="store_true")
    typegen_parser.add_argument("--factory", action="store_true",
                                help="invoke the target as a zero-argument application factory")
    return parser


def options_from_namespace(namespace: argparse.Namespace) -> RunOptions:
    if namespace.command not in ("run", "dev"):
        raise CliError(f"unsupported command {namespace.command!r}", exit_code=2)
    if (namespace.tls_cert is None) != (namespace.tls_key is None):
        raise CliError("--tls-cert and --tls-key must be supplied together", exit_code=2)
    if namespace.tls_password_file is not None and namespace.tls_cert is None:
        raise CliError("--tls-password-file requires --tls-cert and --tls-key", exit_code=2)
    if namespace.command == "dev" and namespace.port == 0:
        raise CliError("wreath dev does not support port 0 across reloads", exit_code=2)
    reload_delay = getattr(namespace, "reload_delay", 0.25)
    reload_debounce = getattr(namespace, "reload_debounce", 0.10)
    if reload_delay <= 0 or reload_debounce < 0:
        raise CliError("reload delay must be positive and debounce non-negative", exit_code=2)
    protocols = tuple(namespace.protocols or ("http/1.1",))
    return RunOptions(
        command=namespace.command,
        target=namespace.target,
        factory=namespace.factory,
        host=namespace.host,
        port=namespace.port,
        backlog=namespace.backlog,
        keep_alive_timeout=namespace.keep_alive_timeout,
        request_timeout=namespace.request_timeout,
        shutdown_timeout=namespace.shutdown_timeout,
        server_header=namespace.server_header,
        date_header=namespace.date_header,
        max_request_line=namespace.max_request_line,
        max_header_count=namespace.max_header_count,
        max_header_bytes=namespace.max_header_bytes,
        max_body_bytes=namespace.max_body_bytes,
        read_high_water=namespace.read_high_water,
        read_high_water_messages=namespace.read_high_water_messages,
        max_ws_fragments=namespace.max_ws_fragments,
        lifespan=namespace.lifespan,
        protocols=protocols,
        max_concurrent_streams=namespace.max_concurrent_streams,
        initial_stream_window=namespace.initial_stream_window,
        initial_connection_window=namespace.initial_connection_window,
        max_header_list_bytes=namespace.max_header_list_bytes,
        hpack_table_bytes=namespace.hpack_table_bytes,
        qpack_table_bytes=namespace.qpack_table_bytes,
        qpack_blocked_streams=namespace.qpack_blocked_streams,
        loop=namespace.loop,
        tls_cert=namespace.tls_cert,
        tls_key=namespace.tls_key,
        tls_password_file=namespace.tls_password_file,
        reload_dirs=tuple(getattr(namespace, "reload_dir", ())),
        reload_includes=tuple(getattr(namespace, "reload_includes", ())) or ("*.py",),
        reload_excludes=tuple(getattr(namespace, "reload_excludes", ())),
        reload_delay=reload_delay,
        reload_debounce=reload_debounce,
    )


def _split_target(target: str) -> tuple[str, str]:
    if target.count(":") > 1:
        raise CliError("application target must use module:attribute syntax")
    module_name, separator, attribute = target.partition(":")
    if not separator:
        attribute = "app"
    if (
        not module_name
        or module_name.startswith(".")
        or any(not part.isidentifier() for part in module_name.split("."))
        or not attribute
        or not attribute.isidentifier()
    ):
        raise CliError("application target must use module:attribute syntax")
    return module_name, attribute


def load_application(target: str, *, factory: bool = False) -> ASGIApplication:
    """Import one ASGI application or explicit zero-argument factory."""
    module_name, attribute = _split_target(target)
    try:
        module = importlib.import_module(module_name)
    except Exception as error:  # noqa: BLE001 -- target imports can fail arbitrarily
        raise CliError(f"could not import application module {module_name!r}: {error}") from error
    try:
        selected = getattr(module, attribute)
    except AttributeError as error:
        raise CliError(
            f"application module {module_name!r} has no attribute {attribute!r}"
        ) from error
    if factory:
        if not callable(selected):
            raise CliError(f"application factory {target!r} is not callable")
        try:
            selected = selected()
        except Exception as error:  # noqa: BLE001 -- user factory failure
            raise CliError(f"application factory {target!r} failed: {error}") from error
        if inspect.isawaitable(selected):
            close = getattr(selected, "close", None)
            if close is not None:
                close()
            raise CliError(f"application factory {target!r} must be synchronous")
    if not callable(selected):
        raise CliError(f"application target {target!r} is not callable")
    return cast(ASGIApplication, selected)


def _loop_factory(name: LoopName) -> Callable[[], Any] | None:
    if name == "asyncio":
        return None
    try:
        uvloop = importlib.import_module("uvloop")
    except ImportError as error:
        raise CliError(
            "uvloop is not installed; install it or use --loop asyncio", exit_code=2
        ) from error
    return cast(Callable[[], Any], uvloop.new_event_loop)


def run_server(
    app: ASGIApplication,
    config: ServerConfig,
    *,
    tls: TLSConfig | None,
    loop_factory: Callable[[], Any] | None,
) -> None:
    run(app, config, tls=tls, loop_factory=loop_factory)


def execute(options: RunOptions) -> None:
    if options.command == "dev":
        from ._devserver import supervise

        supervise(options)
        return
    app = load_application(options.target, factory=options.factory)
    config = options.server_config()
    tls = options.tls_config()
    loop_factory = _loop_factory(options.loop)
    run_server(app, config, tls=tls, loop_factory=loop_factory)


def execute_typegen(namespace: argparse.Namespace) -> int:
    from .typegen.cli import TypegenCliError, TypegenOptions, run

    # Importing the application for introspection must not start ASGI lifespan;
    # load_application only imports (and optionally calls a factory).
    app = load_application(namespace.target, factory=namespace.factory)
    options = TypegenOptions(
        target=namespace.typegen_target,
        output=namespace.output,
        react_query=namespace.react_query,
        base_url_env=namespace.base_url_env,
        check=namespace.check,
        allow_unknown=namespace.allow_unknown,
        pure=namespace.pure,
        factory=namespace.factory,
        title=namespace.title,
        version=namespace.api_version,
    )
    try:
        return run(app, options)
    except TypegenCliError as error:
        print(f"wreath typegen: error: {error}", file=sys.stderr)
        return error.exit_code


def main(argv: Sequence[str] | None = None) -> int:
    namespace = build_parser().parse_args(argv)
    try:
        if namespace.command == "typegen":
            return execute_typegen(namespace)
        options = options_from_namespace(namespace)
        execute(options)
    except CliError as error:
        print(f"neo: error: {error}", file=sys.stderr)
        return error.exit_code
    return 0
