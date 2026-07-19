"""Dependency-free implementation helpers for Wreath's command-line interface."""

from __future__ import annotations

import argparse
import importlib
import inspect
import os
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
    inspect_parser = commands.add_parser(
        "inspect", help="query a running server's read-only telemetry Inspector"
    )
    inspect_parser.add_argument(
        "socket", help="path to the Inspector's Unix-domain socket"
    )
    inspect_parser.add_argument(
        "topic", nargs="?", default="summary",
        choices=("summary", "active", "routes", "explain-route", "explain-plan",
                 "metadata", "timeline", "failures", "distributions"),
        help="what to show (default: summary = workers + pressure)",
    )
    inspect_parser.add_argument("--route-id", type=int, default=None)
    inspect_parser.add_argument("--method", default=None)
    inspect_parser.add_argument("--path", default=None)
    inspect_parser.add_argument("--plan-id", type=int, default=None)
    inspect_parser.add_argument(
        "--table", default=None,
        help="metadata table name for the metadata topic",
    )
    inspect_parser.add_argument("--offset", type=int, default=0)
    inspect_parser.add_argument("--limit", type=int, default=50)
    inspect_parser.add_argument(
        "--json", action="store_true", dest="as_json",
        help="print versioned JSON instead of tables",
    )
    _add_capture_parser(commands)
    _add_replay_parser(commands)
    return parser


def _add_replay_parser(commands: Any) -> None:
    """`wreath replay {transport,plan}` -- replay a recording through the owned
    pipeline. Unlike inspect/capture this loads the target application, because
    replay drives the app's own protocol and endpoint code in-process."""
    replay_parser = commands.add_parser(
        "replay", help="replay a recording through the owned protocol/endpoint pipeline"
    )
    replay_parser.add_argument(
        "--factory", action="store_true",
        help="the target is a zero-argument callable returning the application",
    )
    replay_parser.add_argument(
        "--json", action="store_true", dest="as_json",
        help="print versioned JSON instead of a human summary",
    )
    actions = replay_parser.add_subparsers(dest="replay_action", required=True)

    transport = actions.add_parser(
        "transport", help="feed a recorded connection into the owned HTTP/1 driver"
    )
    transport.add_argument("target", metavar="MODULE[:ATTRIBUTE]")
    transport.add_argument("recording", metavar="RECORDING", help="path to a .wtr1 recording")
    transport.add_argument(
        "--inject", metavar="SCHEDULE", default=None,
        help="apply a .wfs1 fault schedule before the bytes reach the parser",
    )
    transport.add_argument(
        "--record-faults", metavar="PATH", default=None,
        help="write the realized fault schedule that this run applied",
    )
    transport.add_argument("--pure", action="store_true", help="use the pure protocol driver")

    plan = actions.add_parser(
        "plan", help="replay a canonical request through routing/binding/serialization"
    )
    plan.add_argument("target", metavar="MODULE[:ATTRIBUTE]")
    plan.add_argument("--method", default="GET")
    plan.add_argument("--path", required=True)
    plan.add_argument("--query", default="", help="raw query string")
    plan.add_argument("--body", default="", help="request body (utf-8)")
    plan.add_argument(
        "--header", action="append", default=[], metavar="NAME:VALUE",
        help="a request header (repeatable)",
    )
    plan.add_argument(
        "--mode", choices=("invoke", "replace", "skip"), default="invoke",
        help="handler boundary: run it, use --replace-body, or resolve only",
    )
    plan.add_argument("--replace-body", default=None, help="REPLACE mode: recorded return string")


def _add_capture_parser(commands: Any) -> None:
    """`wreath capture {arm,status,disarm}` -- the token-gated capture control."""
    capture_parser = commands.add_parser(
        "capture", help="arm/disarm forensic capture on a running server"
    )
    capture_parser.add_argument("socket", help="path to the Inspector's Unix socket")
    capture_parser.add_argument(
        "--token", default=None,
        help="capability token (or the WREATH_CAPTURE_TOKEN environment variable)",
    )
    capture_parser.add_argument(
        "--json", action="store_true", dest="as_json",
        help="print JSON instead of a human summary",
    )
    actions = capture_parser.add_subparsers(dest="capture_action", required=True)

    arm = actions.add_parser("arm", help="install a bounded, expiring capture arm")
    arm.add_argument("--allow-header", action="append", dest="allow_headers", default=[],
                     metavar="NAME", help="header captured verbatim (repeatable)")
    arm.add_argument("--hash-header", action="append", dest="hash_headers", default=[],
                     metavar="NAME", help="header captured as a keyed hash (repeatable)")
    arm.add_argument("--mask-header", action="append", dest="mask_headers", default=[],
                     metavar="NAME", help="header captured as length only (repeatable)")
    arm.add_argument("--allow-query", action="append", dest="allow_query", default=[],
                     metavar="NAME", help="query parameter captured verbatim (repeatable)")
    arm.add_argument("--hash-query", action="append", dest="hash_query", default=[],
                     metavar="NAME", help="query parameter captured as a keyed hash (repeatable)")
    arm.add_argument("--mask-query", action="append", dest="mask_query", default=[],
                     metavar="NAME", help="query parameter captured as length only (repeatable)")
    arm.add_argument("--body", default=None,
                     choices=("none", "metadata", "hashed", "structured"),
                     help="request/response body capture mode")
    arm.add_argument("--dependency", default=None,
                     choices=("none", "metadata", "hashed", "structured"),
                     help="dependency (DB params/rows, outbound bodies) capture mode")
    arm.add_argument("--max-body-bytes", type=int, default=0)
    arm.add_argument("--max-fields", type=int, default=0)
    arm.add_argument("--max-depth", type=int, default=0)
    arm.add_argument("--slabs", type=int, default=0, help="capture budget: slab count")
    arm.add_argument("--slab-bytes", type=int, default=64 * 1024)
    arm.add_argument("--expiry", type=float, required=True, metavar="SECONDS",
                     help="how long the arm stays live (required; no forever arms)")
    arm.add_argument("--max-matches", type=int, default=0,
                     help="stop after this many matches (0 = only expiry bounds it)")

    actions.add_parser("status", help="list the active capture arms and the ceiling")

    disarm = actions.add_parser("disarm", help="remove a capture arm by id")
    disarm.add_argument("--arm-id", type=int, required=True)


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


def execute_inspect(namespace: argparse.Namespace) -> int:
    # The CLI is a protocol client only: it never imports the application it
    # inspects. Everything it shows arrived over the Inspector socket.
    import asyncio
    import json as _json

    from .inspector import Command, InspectorClient, InspectorError

    async def query() -> dict:
        async with InspectorClient(namespace.socket) as client:
            topic = namespace.topic
            if topic == "summary":
                hello = await client.hello()
                workers = await client.call(Command.WORKERS)
                return {
                    "server": hello,
                    "workers": workers["workers"],
                    "generation": workers["generation"],
                }
            if topic == "active":
                return await client.call(
                    Command.ACTIVE_REQUESTS,
                    {"offset": namespace.offset, "limit": namespace.limit},
                )
            if topic == "routes":
                return await client.metadata(
                    "routes", offset=namespace.offset, limit=namespace.limit
                )
            if topic == "explain-route":
                return await client.explain_route(
                    route_id=namespace.route_id,
                    method=namespace.method,
                    path=namespace.path,
                )
            if topic == "explain-plan":
                if namespace.plan_id is None:
                    raise CliError("explain-plan needs --plan-id", exit_code=2)
                return await client.explain_plan(namespace.plan_id)
            if topic == "timeline":
                return await client.timeline(
                    offset=namespace.offset, limit=namespace.limit
                )
            if topic == "failures":
                return await client.recent_failures(
                    offset=namespace.offset, limit=namespace.limit
                )
            if topic == "distributions":
                return await client.route_distributions()
            if namespace.table is None:
                raise CliError("metadata needs --table", exit_code=2)
            return await client.metadata(
                namespace.table, offset=namespace.offset, limit=namespace.limit
            )

    try:
        body = asyncio.run(query())
    except InspectorError as error:
        print(f"wreath inspect: error: {error}", file=sys.stderr)
        return 1
    except (ConnectionError, FileNotFoundError) as error:
        print(f"wreath inspect: cannot reach inspector: {error}", file=sys.stderr)
        return 1
    if namespace.as_json:
        print(_json.dumps({"version": 1, "topic": namespace.topic, "data": body}))
        return 0
    _print_inspect(namespace.topic, body)
    return 0


def execute_capture(namespace: argparse.Namespace) -> int:
    # A protocol client, like `inspect`: it never imports the application. The
    # token comes from --token or the environment so it stays off the process
    # table when supplied that way.
    import asyncio
    import json as _json

    from .inspector import InspectorClient, InspectorError

    token = namespace.token or os.environ.get("WREATH_CAPTURE_TOKEN")
    if not token:
        raise CliError(
            "a capability token is required (--token or WREATH_CAPTURE_TOKEN)",
            exit_code=2,
        )

    async def run() -> dict:
        async with InspectorClient(namespace.socket) as client:
            action = namespace.capture_action
            if action == "status":
                return await client.capture_status(token=token)
            if action == "disarm":
                return await client.disarm_capture(token=token, arm_id=namespace.arm_id)
            redaction: dict[str, Any] = {}
            if namespace.allow_headers:
                redaction["header_allowlist"] = namespace.allow_headers
            if namespace.hash_headers:
                redaction["header_hash"] = namespace.hash_headers
            if namespace.mask_headers:
                redaction["header_mask"] = namespace.mask_headers
            if namespace.allow_query:
                redaction["query_allowlist"] = namespace.allow_query
            if namespace.hash_query:
                redaction["query_hash"] = namespace.hash_query
            if namespace.mask_query:
                redaction["query_mask"] = namespace.mask_query
            if namespace.body is not None:
                redaction["body"] = namespace.body
            if namespace.dependency is not None:
                redaction["dependency"] = namespace.dependency
            if namespace.max_body_bytes:
                redaction["max_body_bytes"] = namespace.max_body_bytes
            if namespace.max_fields:
                redaction["max_fields"] = namespace.max_fields
            if namespace.max_depth:
                redaction["max_depth"] = namespace.max_depth
            budget: dict[str, Any] = {"slab_bytes": namespace.slab_bytes}
            if namespace.slabs:
                budget["slabs"] = namespace.slabs
            return await client.arm_capture(
                token=token,
                redaction=redaction or None,
                budget=budget,
                expiry_seconds=namespace.expiry,
                max_matches=namespace.max_matches,
            )

    try:
        body = asyncio.run(run())
    except InspectorError as error:
        print(f"wreath capture: error: {error}", file=sys.stderr)
        return 1
    except (ConnectionError, FileNotFoundError) as error:
        print(f"wreath capture: cannot reach inspector: {error}", file=sys.stderr)
        return 1
    if namespace.as_json:
        print(_json.dumps({"version": 1, "action": namespace.capture_action, "data": body}))
        return 0
    _print_capture(namespace.capture_action, body)
    return 0


def _print_capture(action: str, body: dict) -> None:
    if action == "arm":
        print(
            f"armed capture #{body['arm_id']}: expires in {body['expires_in']}s, "
            f"{'unlimited' if body['remaining_matches'] < 0 else body['remaining_matches']}"
            " matches"
        )
        print(f"  headers: {', '.join(body['headers']) if body['headers'] else 'none'}")
        return
    if action == "disarm":
        print("disarmed" if body["disarmed"] else "no such arm")
        return
    # status
    ceiling = body["ceiling"]
    print(
        f"ceiling: {ceiling['capture_slabs']} slabs, "
        f"{ceiling['max_capture_bytes']} bytes, body={ceiling['body']}"
    )
    arms = body["arms"]
    print(f"{len(arms)} active arm(s)")
    for arm in arms:
        remaining = "unlimited" if arm["remaining_matches"] < 0 else arm["remaining_matches"]
        print(
            f"  #{arm['arm_id']}: expires in {arm['expires_in']}s, {remaining} matches, "
            f"headers {', '.join(arm['headers']) if arm['headers'] else 'none'}"
        )


def _print_inspect(topic: str, body: dict) -> None:
    if topic == "summary":
        server = body["server"]
        print(f"wreath inspector (protocol {server['protocol']}, pid {server['pid']})")
        print(f"capabilities: {', '.join(server['capabilities'])}")
        for worker in body["workers"]:
            print(
                f"worker: mode={worker['mode']} requests={worker['requests']} "
                f"completions={worker['completions']} active={worker['active_count']}"
            )
            print(
                f"  ring: {worker['ring_occupancy']} occupied "
                f"(high water {worker['ring_high_water']})"
            )
            print(
                f"  phases: {worker['phase_in_use']}/{worker['phase_capacity']} "
                f"in use (high water {worker['phase_high_water']})"
            )
            losses = {k: v for k, v in worker["losses"].items() if v}
            print(f"  losses: {losses if losses else 'none'}")
        return
    if topic == "active":
        print(f"{body['total']} active request(s)"
              + (" [truncated page]" if body.get("truncated") else ""))
        for row in body["requests"]:
            print(
                f"  #{row['request_id']}  {row['protocol']:9s} "
                f"age {row['age_us']}us  route {row['route_id']}"
            )
        return
    if topic in ("routes", "metadata"):
        print(f"{body['table']}: {body['total']} row(s)"
              + (" [truncated page]" if body.get("truncated") else ""))
        for row in body["rows"]:
            if "method" in row:
                print(f"  {row['id']:4d}  {row['method']:7s} {row['path']}")
            else:
                print(f"  {row['id']:4d}  {row.get('name', row)}")
        return
    if topic in ("timeline", "failures"):
        label = "failure" if topic == "failures" else "trace"
        print(
            f"{body['total']} {label}(s), {body['assembled']} assembled"
            + (" [truncated page]" if body.get("truncated") else "")
        )
        for row in body["traces"]:
            flag = "!" if row["is_failure"] else " "
            print(
                f" {flag}#{row['request_id']}  {row['protocol']:9s} "
                f"status {row['status']:>3}  {row['terminal']:12s} "
                f"{row['duration_us']}us  route {row['route_id']}"
                + (f"  phases={len(row['phases'])}" if row["phases"] else "")
            )
        _print_projector_loss(body.get("loss"))
        return
    if topic == "distributions":
        print(f"route distributions ({body['assembled']} assembled)")
        for row in body["routes"]:
            where = (
                f"{row['method']} {row['path']}"
                if row.get("path")
                else f"route {row['route_id']}"
            )
            avg = row["duration_us_sum"] // row["count"] if row["count"] else 0
            print(
                f"  {where}: {row['count']} req, {row['errors']} err, "
                f"avg {avg}us, max {row['duration_us_max']}us"
            )
        _print_projector_loss(body.get("loss"))
        return
    for key, value in body.items():
        print(f"{key}: {value}")


def _print_projector_loss(loss: dict | None) -> None:
    if not loss:
        return
    nonzero = {k: v for k, v in loss.items() if v}
    if nonzero:
        print(f"  projector loss: {nonzero}")


def execute_replay(namespace: argparse.Namespace) -> int:
    """Run a transport or endpoint-plan replay and print the owned outcome.

    Unlike inspect/capture, replay loads the target application: it drives the
    app's own protocol and endpoint code in-process over fake transports. It
    never opens a socket and cannot broaden any capture policy.
    """
    import asyncio
    import json as _json

    from . import replay as rp

    app = load_application(namespace.target, factory=namespace.factory)
    action = namespace.replay_action

    if action == "transport":
        recording = rp.open_recording(namespace.recording)
        schedule = None
        if namespace.inject:
            schedule = rp.FaultSchedule.from_bytes(_read_bytes(namespace.inject))
        protocol_cls = None
        if namespace.pure:
            from ._pure.server import Http1Protocol as protocol_cls  # noqa: N813
        result = asyncio.run(
            rp.replay_transport(app, recording, protocol_cls=protocol_cls, faults=schedule)
        )
        if namespace.record_faults:
            _write_bytes(namespace.record_faults, (schedule or rp.FaultSchedule()).to_bytes())
        if namespace.as_json:
            print(_json.dumps({
                "version": 1, "kind": "transport",
                "terminal": result.terminal, "write_count": result.write_count,
                "segments_fed": result.segments_fed,
                "status_line": result.response.split(b"\r\n", 1)[0].decode("latin-1", "replace"),
                "response_bytes": len(result.response),
            }))
        else:
            status = result.response.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            print(f"terminal={result.terminal} writes={result.write_count} "
                  f"segments_fed={result.segments_fed}")
            print(status or "(no response bytes)")
        return 0

    headers = tuple(_split_header(h) for h in namespace.header)
    canonical = rp.CanonicalRequest(
        method=namespace.method, path=namespace.path,
        headers=headers, query_string=namespace.query.encode("utf-8"),
        body=namespace.body.encode("utf-8"),
    )
    mode = rp.PlanMode(namespace.mode)
    result = asyncio.run(rp.replay_endpoint_plan(
        app, canonical, mode=mode,
        recorded_return=namespace.replace_body if mode is rp.PlanMode.REPLACE else None,
    ))
    if namespace.as_json:
        print(_json.dumps({
            "version": 1, "kind": "plan", "mode": result.mode,
            "status": result.status, "body_bytes": len(result.body),
            "best_effort": result.best_effort, "deterministic": result.deterministic,
            "note": result.note,
        }))
    else:
        print(f"mode={result.mode} status={result.status} "
              f"deterministic={result.deterministic} best_effort={result.best_effort}")
        if result.note:
            print(result.note)
        if result.body:
            print(result.body.decode("utf-8", "replace"))
    return 0


def _split_header(raw: str) -> tuple[bytes, bytes]:
    name, _, value = raw.partition(":")
    return name.strip().lower().encode("latin-1"), value.strip().encode("latin-1")


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def _write_bytes(path: str, data: bytes) -> None:
    with open(path, "wb") as handle:
        handle.write(data)


def main(argv: Sequence[str] | None = None) -> int:
    namespace = build_parser().parse_args(argv)
    try:
        if namespace.command == "typegen":
            return execute_typegen(namespace)
        if namespace.command == "inspect":
            return execute_inspect(namespace)
        if namespace.command == "capture":
            return execute_capture(namespace)
        if namespace.command == "replay":
            return execute_replay(namespace)
        options = options_from_namespace(namespace)
        execute(options)
    except CliError as error:
        print(f"wreath: error: {error}", file=sys.stderr)
        return error.exit_code
    return 0
