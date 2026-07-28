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

LoopName = Literal["asyncio", "uvloop", "metal"]


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
    response_high_water: int
    response_low_water: int
    response_high_water_segments: int
    response_low_water_segments: int
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
    workers: int
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
                response_high_water=self.response_high_water,
                response_low_water=self.response_low_water,
                response_high_water_segments=self.response_high_water_segments,
                response_low_water_segments=self.response_low_water_segments,
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
    parser.add_argument(
        "--response-high-water", type=int, default=defaults.response_high_water
    )
    parser.add_argument(
        "--response-low-water", type=int, default=defaults.response_low_water
    )
    parser.add_argument(
        "--response-high-water-segments", type=int,
        default=defaults.response_high_water_segments,
    )
    parser.add_argument(
        "--response-low-water-segments", type=int,
        default=defaults.response_low_water_segments,
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
    parser.add_argument("--loop", choices=("asyncio", "uvloop", "metal"), default="asyncio")
    parser.add_argument(
        "--workers", type=int, default=1,
        help="metal worker processes sharing an SO_REUSEPORT listener",
    )
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
    migrations_parser = commands.add_parser(
        "migrations", help="inspect and run Wreath-metal PostgreSQL migrations"
    )
    migration_actions = migrations_parser.add_subparsers(
        dest="migration_action", required=True
    )
    migration_detect = migration_actions.add_parser(
        "detect", help="compare one compiled registry with its live schema"
    )
    migration_detect.add_argument("target", help="application target as module:attribute")
    migration_detect.add_argument("--database", default="main")
    migration_detect.add_argument("--factory", action="store_true")
    migration_detect.add_argument("--json", action="store_true")
    migration_check = migration_actions.add_parser(
        "check", help="exit nonzero when one compiled registry has schema drift"
    )
    migration_check.add_argument("target", help="application target as module:attribute")
    migration_check.add_argument("--database", default="main")
    migration_check.add_argument("--factory", action="store_true")
    migration_check.add_argument("--json", action="store_true")
    migration_generate = migration_actions.add_parser(
        "generate", help="build a deterministic named migration review plan"
    )
    migration_generate.add_argument("target", help="application target as module:attribute")
    migration_generate.add_argument("--database", default="main")
    migration_generate.add_argument("--factory", action="store_true")
    migration_generate.add_argument("--json", action="store_true")
    migration_generate.add_argument("--output", metavar="DIRECTORY")
    migration_generate.add_argument("--migration-id", metavar="32_HEX")
    generation_parent = migration_generate.add_mutually_exclusive_group()
    generation_parent.add_argument("--initial", action="store_true")
    generation_parent.add_argument("--parent", metavar="64_HEX")
    migration_show = migration_actions.add_parser(
        "show", help="verify and display one immutable migration artifact"
    )
    migration_show.add_argument("artifact", metavar="PATH")
    migration_show.add_argument("--json", action="store_true")
    migration_status = migration_actions.add_parser(
        "status", help="verify an artifact chain against code and the live catalog"
    )
    migration_status.add_argument("target", help="application target as module:attribute")
    migration_status.add_argument("artifacts", nargs="+", metavar="ARTIFACT")
    migration_status.add_argument("--database", default="main")
    migration_status.add_argument("--factory", action="store_true")
    migration_status.add_argument("--dsn-env", default="WREATH_MIGRATION_DSN")
    migration_status.add_argument("--json", action="store_true")
    migration_apply = migration_actions.add_parser(
        "apply", help="lock, apply, record, and verify one authoritative artifact"
    )
    migration_apply.add_argument("target", help="application target as module:attribute")
    migration_apply.add_argument("artifact", metavar="ARTIFACT")
    migration_apply.add_argument("--database", default="main")
    migration_apply.add_argument("--factory", action="store_true")
    migration_apply.add_argument("--allow-destructive", action="store_true")
    migration_apply.add_argument("--dsn-env", default="WREATH_MIGRATION_DSN")
    migration_apply.add_argument("--json", action="store_true")
    migration_down = migration_actions.add_parser(
        "down", help="revert the most recently applied artifact, inverted in metal"
    )
    migration_down.add_argument("target", help="application target as module:attribute")
    migration_down.add_argument("artifact", metavar="ARTIFACT")
    migration_down.add_argument("--database", default="main")
    migration_down.add_argument("--factory", action="store_true")
    migration_down.add_argument("--allow-destructive", action="store_true")
    migration_down.add_argument(
        "--force", action="store_true",
        help="downgrade even when live ORM code still maps a dropped/retyped object",
    )
    migration_down.add_argument("--dsn-env", default="WREATH_MIGRATION_DSN")
    migration_down.add_argument("--json", action="store_true")

    docs_parser = commands.add_parser(
        "docs", help="build a documentation site from markdown (no third-party toolchain)"
    )
    docs_actions = docs_parser.add_subparsers(dest="docs_action", required=True)
    for _action, _help in (
        ("build", "render the site to its output directory"),
        ("check", "build strictly and report orphan pages / dead links"),
        ("serve", "build then preview the site over HTTP"),
    ):
        _sub = docs_actions.add_parser(_action, help=_help)
        _sub.add_argument(
            "config", nargs="?", default="wreath_docs.py",
            help="the Python config module exposing `site = Site(...)`",
        )
        if _action == "serve":
            _sub.add_argument("--port", type=int, default=8000, help="preview port")
            _sub.add_argument(
                "--no-reload", action="store_true",
                help="do not watch the source tree and rebuild on change",
            )

    port_parser = commands.add_parser(
        "port", help="port an existing FastAPI app to Wreath (report or emit)"
    )
    port_parser.add_argument("source", nargs="+", help="one or more app roots")
    port_parser.add_argument(
        "--report-only", action="store_true", default=True,
        help="static analysis + report (default when neither --output nor --in-place is given)",
    )
    port_parser.add_argument("--json", action="store_true", dest="as_json",
                             help="emit the machine-readable report JSON")
    port_parser.add_argument(
        "--by-rule", action="store_true",
        help="cluster the findings needing a decision by rule, heaviest first, "
             "instead of listing them one per line in file order",
    )
    port_parser.add_argument(
        "--rule", action="append", metavar="ID",
        help="show only this rule's sites (repeatable, e.g. --rule orm.query.filter)",
    )
    port_parser.add_argument(
        "--context", type=int, default=0, metavar="N",
        help="show N source lines either side of each site (implies the site view)",
    )
    port_emit = port_parser.add_mutually_exclusive_group()
    port_emit.add_argument("--in-place", action="store_true",
                           help="rewrite files in place "
                                "(Phase 1 declarative emit; requires --force)")
    port_emit.add_argument("--output", metavar="DIR",
                           help="write ported code to a sister tree (Phase 1 declarative emit)")
    port_parser.add_argument("--force", action="store_true",
                             help="allow --in-place and overwrite hand-edited outputs")
    port_parser.add_argument(
        "--opinionated", action="store_true",
        help="make the changes that reach past one file, instead of leaving a note: "
             "give a function that runs queries the session parameter it needs "
             "(its callers then have to pass one)",
    )
    audit_parser = commands.add_parser(
        "audit",
        help="audit generated HTML + responses for accessibility (WCAG 2.1) and performance",
    )
    audit_actions = audit_parser.add_subparsers(dest="audit_action", required=True)
    audit_static = audit_actions.add_parser(
        "static", help="audit the API-docs surface and static HTML for a11y + performance"
    )
    audit_static.add_argument("target", help="application import target, e.g. app.main:app")
    audit_static.add_argument(
        "--factory", action="store_true", help="treat the target as an application factory"
    )
    audit_static.add_argument(
        "--static", action="append", metavar="DIR", default=[],
        help="also audit *.html under DIR (repeatable)",
    )
    audit_static.add_argument("--title", default="Wreath", help="docs title used when rendering")
    audit_static.add_argument(
        "--version", default="0.1.0", help="docs version used when rendering"
    )
    audit_static.add_argument(
        "--json", action="store_true", dest="as_json",
        help="emit the machine-readable report JSON",
    )
    audit_static.add_argument(
        "--strict", action="store_true", help="exit non-zero on warnings as well as errors"
    )
    audit_static.add_argument(
        "--fix", action="store_true",
        help="apply the safe auto-fix subset to static HTML (and suggest patches for the docs)",
    )
    audit_runtime = audit_actions.add_parser(
        "runtime", help="audit a running server's live responses (headers + HTML)"
    )
    audit_runtime.add_argument("url", nargs="?", help="base URL of the running app")
    audit_runtime.add_argument(
        "--json", action="store_true", dest="as_json",
        help="emit the machine-readable report JSON",
    )
    audit_runtime.add_argument(
        "--strict", action="store_true", help="exit non-zero on warnings as well as errors"
    )
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
    _add_doctor_parser(commands)
    _add_capture_parser(commands)
    _add_replay_parser(commands)
    _add_passes_parser(commands)
    _add_schema_parser(commands)
    return parser


def _add_schema_parser(commands: Any) -> None:
    """`wreath schema sql` / `check` -- the DBA path, made first-class.

    A deployment whose application role cannot `CREATE SCHEMA` is common and
    supported rather than an error. `sql` emits exactly what wreath would have
    applied, for a DBA to run; `check` reads the catalog and reports what is
    missing. Both exist so "apply this by hand" is a command rather than a
    paragraph in a guide.
    """
    schema_parser = commands.add_parser(
        "schema", help="wreath's own tables: emit their DDL, or check they exist"
    )
    actions = schema_parser.add_subparsers(dest="schema_action", required=True)

    emit = actions.add_parser(
        "sql", help="print the DDL for wreath's own tables, for a DBA to apply"
    )
    emit.add_argument("target", help="application target as module:attribute")
    emit.add_argument("--factory", action="store_true")
    emit.add_argument(
        "--component", default=None,
        help="one component by name (default: every registered one)",
    )
    emit.add_argument(
        "--from-version", type=int, default=0, dest="from_version",
        help="emit only the steps a database at this version still needs",
    )

    check = actions.add_parser(
        "check", help="report each component's version and any missing relation"
    )
    check.add_argument("target", help="application target as module:attribute")
    check.add_argument("--factory", action="store_true")
    check.add_argument("--json", action="store_true", dest="as_json")


def _add_passes_parser(commands: Any) -> None:
    """`wreath passes status` -- where every chunked pass has got to.

    The ledger row is the durable status, so this is one read of one table and
    it is honest at three in the morning: a pass that has been running for two
    hours is still there, and a pass nothing is driving says so instead of
    looking idle.
    """
    passes_parser = commands.add_parser(
        "passes", help="report on chunked passes (backfills, rollups, purges)"
    )
    actions = passes_parser.add_subparsers(dest="passes_action", required=True)
    status = actions.add_parser(
        "status", help="show every pass's phase, position, and pacing"
    )
    status.add_argument("target", help="application target as module:attribute")
    status.add_argument("--factory", action="store_true")
    status.add_argument(
        "--database", default=None,
        help="read one database's ledger (default: every one a job runner uses)",
    )
    status.add_argument(
        "--schema", default=None, help="ledger schema (default: the job runner's)"
    )
    status.add_argument("--name", default=None, help="one pass by name")
    status.add_argument(
        "--holes", action="store_true",
        help="list every dead-lettered chunk, with the statement that reproduces it",
    )
    status.add_argument("--json", action="store_true", dest="as_json")

    retry = actions.add_parser(
        "retry",
        help="requeue dead-lettered chunks, which is the only way to un-bar a gate",
    )
    retry.add_argument("target", help="application target as module:attribute")
    retry.add_argument("--factory", action="store_true")
    retry.add_argument("--database", default=None)
    retry.add_argument("--schema", default=None)
    retry.add_argument("--name", default=None, help="one pass by name")
    retry.add_argument("--json", action="store_true", dest="as_json")


def _add_doctor_parser(commands: Any) -> None:
    """`wreath doctor n-plus-one` -- diagnose a running server's recorded traces.

    A protocol client like `inspect`: it never imports the application. The
    diagnosis comes entirely from what the Flight Recorder already recorded.
    """
    doctor_parser = commands.add_parser(
        "doctor", help="diagnose defects a green test suite cannot see"
    )
    actions = doctor_parser.add_subparsers(dest="action", required=True)
    n_plus_one = actions.add_parser(
        "n-plus-one",
        help="find requests that queried one model over and over",
    )
    n_plus_one.add_argument(
        "socket", help="path to the Inspector's Unix-domain socket"
    )
    n_plus_one.add_argument(
        "--threshold", type=int, default=10,
        help="queries of one model within one request before it counts (default: 10)",
    )
    n_plus_one.add_argument(
        "--limit", type=int, default=256,
        help="how many recent traces to scan (default: 256)",
    )
    n_plus_one.add_argument(
        "--json", action="store_true", dest="as_json",
        help="print versioned JSON instead of a report",
    )
    n_plus_one.add_argument(
        "--strict", action="store_true",
        help="exit non-zero when anything is found, for a CI gate",
    )


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

    to_test = actions.add_parser(
        "to-test",
        help="write a runnable pytest that re-drives a recorded request",
    )
    to_test.add_argument("target", metavar="MODULE[:ATTRIBUTE]")
    to_test.add_argument("recording", metavar="RECORDING", help="a .wtr1 recording")
    to_test.add_argument(
        "--output", "-o", metavar="PATH", default=None,
        help="write the test here instead of to stdout",
    )
    to_test.add_argument(
        "--name", default=None,
        help="the generated test function's name (derived from the request otherwise)",
    )


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
    if namespace.workers < 1:
        raise CliError("--workers must be at least 1", exit_code=2)
    if namespace.workers > 1 and namespace.loop != "metal":
        raise CliError("--workers requires --loop metal", exit_code=2)
    if namespace.workers > 1 and namespace.command == "dev":
        raise CliError("wreath dev does not support multiple workers", exit_code=2)
    if namespace.workers > 1 and namespace.port == 0:
        raise CliError("multiple workers require a fixed port", exit_code=2)
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
        response_high_water=namespace.response_high_water,
        response_low_water=namespace.response_low_water,
        response_high_water_segments=namespace.response_high_water_segments,
        response_low_water_segments=namespace.response_low_water_segments,
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
        workers=namespace.workers,
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


def _ensure_cwd_importable() -> None:
    """Put the working directory on `sys.path`, the way `python -m` does.

    A console script starts with `sys.path[0]` naming the directory the script
    itself lives in -- inside the virtualenv's `bin` -- so a project's own
    `app.py` sitting in the directory the user is standing in is not importable.
    `wreath run app:app`, the line the README and the getting-started guide both
    open with, therefore failed with `No module named 'app'` on a fresh install.
    `wreath dev` never had the bug because it re-executes `python -m wreath`, and
    `-m` prepends the working directory for us; that inconsistency is what makes
    this a defect in `run` rather than a documented requirement.

    An empty entry means the working directory too, so a caller that already
    arranged for it (`python -m`, an interactive interpreter) is left alone.
    """
    cwd = os.getcwd()
    if any(entry in ("", ".", cwd) for entry in sys.path):
        return
    sys.path.insert(0, cwd)


def load_application(target: str, *, factory: bool = False) -> ASGIApplication:
    """Import one ASGI application or explicit zero-argument factory."""
    module_name, attribute = _split_target(target)
    _ensure_cwd_importable()
    try:
        module = importlib.import_module(module_name)
    except Exception as error:  # target imports can fail arbitrarily
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
        except Exception as error:  # user factory failure
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
    if name == "metal":
        # Experimental "bare metal" tier: the native reactor with wheel-backed
        # timers and inline-driven request coroutines.
        reactor = importlib.import_module("wreath.reactor")
        return cast(Callable[[], Any], reactor.metal_event_loop)
    try:
        uvloop = importlib.import_module("uvloop")
    except ImportError as error:
        raise CliError(
            "uvloop is not installed; install it or use --loop asyncio", exit_code=2
        ) from error
    return cast(Callable[[], Any], uvloop.new_event_loop)


def _protocol_tier() -> str:
    """Whether this process will serve requests from C or from Python.

    Reads the same selector the server itself uses rather than re-deriving it,
    so the startup line cannot claim `native` for a process that silently fell
    back to the pure reference because no extension was built.
    """
    from .server import _select_protocol

    return "pure" if _select_protocol().__module__.startswith("wreath._pure") else "native"


def _listening_address(server: Any) -> str:
    """`host:port` for the first listener, asked of the bound socket.

    Never taken from the configuration: `--port 0` only learns its port from the
    kernel, and printing the requested value would announce a port nothing is
    listening on. An `h3`-only server binds no stream socket, so its address
    comes from the datagram side.
    """
    addresses = server.sockets or server.datagram_addresses
    if not addresses:
        return "an unknown address"
    first = addresses[0]
    host, port = (first.getsockname() if hasattr(first, "getsockname") else first)[:2]
    return f"[{host}]:{port}" if ":" in str(host) else f"{host}:{port}"


def _startup_line(
    target: str, address: str, *, tls: bool, protocols: Sequence[str], loop: str, workers: int
) -> str:
    details = [", ".join(protocols), _protocol_tier(), f"{loop} loop"]
    if workers > 1:
        details.append(f"{workers} workers")
    scheme = "https" if tls else "http"
    return (
        # `_version()` is argparse's version string and already says "wreath".
        f"\N{HERB} {_version()} serving {target} "
        f"on {scheme}://{address}  ({', '.join(details)})"
    )


def run_server(
    app: ASGIApplication,
    config: ServerConfig,
    *,
    tls: TLSConfig | None,
    loop_factory: Callable[[], Any] | None,
    announce: Callable[[Any], None] | None = None,
) -> None:
    run(app, config, tls=tls, loop_factory=loop_factory, ready=announce)


def _apply_metal_worker_affinity(worker_id: int) -> int | None:
    policy = os.environ.get("WREATH_METAL_AFFINITY", "auto").strip().lower()
    if policy == "off":
        return None
    if policy != "auto":
        raise ValueError("WREATH_METAL_AFFINITY must be 'auto' or 'off'")
    if not hasattr(os, "sched_getaffinity") or not hasattr(os, "sched_setaffinity"):
        raise RuntimeError("WREATH_METAL_AFFINITY=auto requires Linux CPU affinity")
    available = sorted(os.sched_getaffinity(0))
    if not available:
        raise RuntimeError("the worker affinity mask contains no CPUs")
    cpu = available[worker_id % len(available)]
    os.sched_setaffinity(0, {cpu})
    return cpu


def _spawn_metal_worker(
    app: ASGIApplication,
    config: ServerConfig,
    *,
    tls: TLSConfig | None,
    worker_id: int,
) -> tuple[int, int]:
    import functools
    import signal

    ready_read, ready_write = os.pipe()
    pid = os.fork()
    if pid != 0:
        os.close(ready_write)
        return pid, ready_read

    os.close(ready_read)
    exit_code = 0
    try:
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        os.environ["_WREATH_WORKER_READY_FD"] = str(ready_write)
        _apply_metal_worker_affinity(worker_id)
        reactor = importlib.import_module("wreath.reactor")
        loop_factory = functools.partial(
            reactor.metal_event_loop,
            worker_id=worker_id,
            reuse_port=True,
        )
        run_server(app, config, tls=tls, loop_factory=loop_factory)
    except BaseException:  # noqa: BLE001 -- see below
        # A forked worker is a process boundary: nothing above this frame can see
        # an exception, so anything that escapes here becomes a silent exit 0 and
        # the supervisor reads a healthy worker that is not serving. `BaseException`
        # rather than `Exception` because that failure mode does not care which
        # base class ended the child. It is not a swallow -- the traceback goes to
        # stderr and the non-zero code is the report.
        import traceback

        traceback.print_exc()
        exit_code = 1
    finally:
        try:
            os.close(ready_write)
        except OSError:
            pass
        os._exit(exit_code)


def _wait_for_worker_generation(
    workers: dict[int, tuple[int, int]], timeout: float
) -> bool:
    import select
    import time

    pending = {ready_fd: worker_id for worker_id, (_pid, ready_fd) in workers.items()}
    deadline = time.monotonic() + timeout
    ready_ok = True
    try:
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            readable, _, _ = select.select(tuple(pending), (), (), min(remaining, 0.1))
            for ready_fd in readable:
                message = os.read(ready_fd, 1)
                if message != b"1":
                    ready_ok = False
                os.close(ready_fd)
                pending.pop(ready_fd, None)
        return ready_ok
    finally:
        for ready_fd in pending:
            try:
                os.close(ready_fd)
            except OSError:
                pass


def _terminate_worker_pids(pids: set[int], timeout: float) -> None:
    import signal
    import time

    live = set(pids)
    for pid in live:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + timeout
    while live and time.monotonic() < deadline:
        for pid in tuple(live):
            try:
                exited, _status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                exited = pid
            if exited:
                live.discard(pid)
        if live:
            time.sleep(0.05)
    for pid in live:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    for pid in live:
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass


def _run_metal_worker_group(
    app: ASGIApplication,
    config: ServerConfig,
    *,
    tls: TLSConfig | None,
    workers: int,
    target: str,
) -> None:
    import signal
    import time

    if not hasattr(os, "fork"):
        raise CliError("metal workers require a POSIX process model", exit_code=2)
    state = {"stop": False, "restart": False}

    def stop_handler(_signum: int, _frame: Any) -> None:
        state["stop"] = True

    def restart_handler(_signum: int, _frame: Any) -> None:
        state["restart"] = True

    previous = {
        signal.SIGINT: signal.getsignal(signal.SIGINT),
        signal.SIGTERM: signal.getsignal(signal.SIGTERM),
        signal.SIGHUP: signal.getsignal(signal.SIGHUP),
    }
    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGHUP, restart_handler)

    startup_timeout = max(5.0, min(config.shutdown_timeout, 30.0))
    current: dict[int, tuple[int, int]] = {}
    draining: dict[int, float] = {}
    try:
        current = {
            worker_id: _spawn_metal_worker(
                app, config, tls=tls, worker_id=worker_id
            )
            for worker_id in range(workers)
        }
        if not _wait_for_worker_generation(current, startup_timeout):
            _terminate_worker_pids(
                {pid for pid, _ready_fd in current.values()},
                config.shutdown_timeout,
            )
            current.clear()
            raise CliError("metal worker generation failed to become ready")

        # One line for the group, printed by the supervisor once every worker
        # has signalled ready. The workers themselves pass no announcer, so a
        # `--workers 8` boot does not print the same address eight times. The
        # port is `config.port` rather than a socket's: workers share an
        # SO_REUSEPORT listener the supervisor never binds, and `--workers`
        # already refuses port 0 for exactly that reason.
        print(
            _startup_line(
                target,
                f"{config.host}:{config.port}",
                tls=tls is not None,
                protocols=config.protocols,
                loop="metal",
                workers=workers,
            ),
            flush=True,
        )

        while not state["stop"]:
            if state["restart"]:
                state["restart"] = False
                replacement = {
                    worker_id: _spawn_metal_worker(
                        app, config, tls=tls, worker_id=worker_id
                    )
                    for worker_id in range(workers)
                }
                if _wait_for_worker_generation(replacement, startup_timeout):
                    old_pids = {pid for pid, _ready_fd in current.values()}
                    drain_deadline = time.monotonic() + config.shutdown_timeout
                    draining.update({pid: drain_deadline for pid in old_pids})
                    for pid in old_pids:
                        try:
                            os.kill(pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                    current = replacement
                else:
                    _terminate_worker_pids(
                        {pid for pid, _ready_fd in replacement.values()},
                        config.shutdown_timeout,
                    )

            for worker_id, (pid, _ready_fd) in tuple(current.items()):
                try:
                    exited, _status = os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    exited = pid
                if not exited or state["stop"]:
                    continue
                replacement = _spawn_metal_worker(
                    app, config, tls=tls, worker_id=worker_id
                )
                candidate = {worker_id: replacement}
                if _wait_for_worker_generation(candidate, startup_timeout):
                    current[worker_id] = replacement
                else:
                    _terminate_worker_pids({replacement[0]}, config.shutdown_timeout)
                    state["stop"] = True
                    break

            for pid, deadline in tuple(draining.items()):
                try:
                    exited, _status = os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    exited = pid
                if exited:
                    draining.pop(pid, None)
                elif time.monotonic() >= deadline:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    try:
                        os.waitpid(pid, 0)
                    except ChildProcessError:
                        pass
                    draining.pop(pid, None)
            time.sleep(0.05)
    finally:
        _terminate_worker_pids(
            {pid for pid, _ready_fd in current.values()} | set(draining),
            config.shutdown_timeout,
        )
        for signame, handler in previous.items():
            signal.signal(signame, handler)


def execute(options: RunOptions) -> None:
    if options.command == "dev":
        from ._devserver import supervise

        supervise(options)
        return
    app = load_application(options.target, factory=options.factory)
    config = options.server_config()
    tls = options.tls_config()
    if options.workers > 1:
        _run_metal_worker_group(
            app, config, tls=tls, workers=options.workers, target=options.target
        )
        return
    loop_factory = _loop_factory(options.loop)

    def announce(server: Any) -> None:
        print(
            _startup_line(
                options.target,
                _listening_address(server),
                tls=tls is not None,
                protocols=options.protocols,
                loop=options.loop,
                workers=1,
            ),
            flush=True,
        )

    run_server(app, config, tls=tls, loop_factory=loop_factory, announce=announce)


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


def execute_schema(namespace: argparse.Namespace) -> int:
    """Emit the DDL for wreath's own tables, or check the catalog against it."""
    import asyncio
    import json as _json

    # `load_application` is typed as an ASGI callable; the schema surface lives
    # on `Wreath`, and the same widening `_pass_ledgers` uses applies here.
    application: Any = load_application(namespace.target, factory=namespace.factory)
    components = application.schema_components()
    if not components:
        raise CliError(
            "this application registers no subsystem that owns tables, so wreath "
            "has no schema to emit or check",
            exit_code=2,
        )

    if namespace.schema_action == "sql":
        from .schema import emit_sql

        if namespace.component is not None:
            components = tuple(c for c in components if c.name == namespace.component)
            if not components:
                known = ", ".join(sorted(c.name for c in application.schema_components()))
                raise CliError(
                    f"no component named {namespace.component!r}; "
                    f"this application registers: {known}",
                    exit_code=2,
                )
        print(emit_sql(components, from_version=namespace.from_version), end="")
        return 0

    from .schema import inspect_components

    databases = application._components_by_database(components)

    async def _run() -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for database, claims in databases.items():
            await database.start()
            try:
                rows += await inspect_components(database, claims)
            finally:
                await database.stop()
        return rows

    reports = asyncio.run(_run())
    if namespace.as_json:
        print(_json.dumps({"components": reports}, indent=2))
    else:
        for row in reports:
            state = "ok" if not row["missing"] else "MISSING " + ", ".join(row["missing"])
            print(
                f"{row['component']:16} recorded={row['recorded']:<4} "
                f"expected={row['expected']:<4} {state}"
            )
    # Non-zero when the catalog disagrees, so this can gate a deploy.
    return 1 if any(r["missing"] or r["recorded"] != r["expected"] for r in reports) else 0


def execute_passes(namespace: argparse.Namespace) -> int:
    """Read every driven pass's ledger row and print it, or requeue its holes."""
    import asyncio
    import json as _json

    application = load_application(namespace.target, factory=namespace.factory)
    targets = _pass_ledgers(application, namespace)
    if not targets:
        raise CliError(
            "no pass ledger to read: the application configures no job runner, "
            "and no --database was given",
            exit_code=2,
        )
    if getattr(namespace, "passes_action", "status") == "retry":
        queued = asyncio.run(_retry_pass_holes(application, targets, name=namespace.name))
        if namespace.as_json:
            print(_json.dumps({"requeued": queued}, indent=2))
            return 0
        if not queued:
            print("no dead-lettered chunks to requeue")
            return 0
        for name, count in sorted(queued.items()):
            print(f"{name}: requeued {count} chunk(s)")
        print(
            "\nA hole clears when its chunk succeeds, not when it is queued -- "
            "check `wreath passes status` again once a shift has run."
        )
        return 0

    rows = asyncio.run(_read_pass_ledgers(targets, name=namespace.name))
    holes = (
        asyncio.run(_read_pass_holes(targets, name=namespace.name))
        if getattr(namespace, "holes", False)
        else []
    )
    if namespace.as_json:
        body: dict[str, Any] = {"passes": [row.as_dict() for row in rows]}
        if getattr(namespace, "holes", False):
            body["holes"] = [hole.as_dict() for hole in holes]
        print(_json.dumps(body, indent=2))
        return 0
    _print_passes(rows)
    if getattr(namespace, "holes", False):
        _print_holes(holes)
    return 0


def _pass_ledgers(application: Any, namespace: argparse.Namespace) -> list[tuple[Any, str]]:
    """(database, schema) pairs to read, discovered from the job runners."""
    if namespace.database is not None:
        databases = getattr(application, "_databases", {})
        database = databases.get(namespace.database)
        if database is None:
            known = ", ".join(sorted(databases)) or "none"
            raise CliError(
                f"unknown database {namespace.database!r}; configured: {known}",
                exit_code=2,
            )
        return [(database, namespace.schema or "wreath")]
    seen: list[tuple[Any, str]] = []
    for runner in getattr(application, "_job_runners", {}).values():
        pair = (runner._db, namespace.schema or runner._schema)
        if pair not in seen:
            seen.append(pair)
    return seen


async def _read_pass_ledgers(
    targets: list[tuple[Any, str]], *, name: str | None
) -> list[Any]:
    from .passes import read_status

    rows: list[Any] = []
    for database, schema in targets:
        await database.start()
        try:
            rows.extend(await read_status(database, schema=schema, name=name))
        finally:
            await database.stop()
    return rows


async def _read_pass_holes(
    targets: list[tuple[Any, str]], *, name: str | None
) -> list[Any]:
    from .passes import read_holes

    holes: list[Any] = []
    for database, schema in targets:
        await database.start()
        try:
            holes.extend(await read_holes(database, schema=schema, name=name))
        finally:
            await database.stop()
    return holes


async def _retry_pass_holes(
    application: Any, targets: list[tuple[Any, str]], *, name: str | None
) -> dict[str, int]:
    """Requeue every hole, through the declarations the application holds.

    Requeuing needs the pass itself rather than just its ledger: the unit goes
    into the same ``pending`` array the walk reads, and only the declaration
    knows the key it is encoded against.
    """
    queued: dict[str, int] = {}
    databases = {id(database) for database, _schema in targets}
    for runner in getattr(application, "_job_runners", {}).values():
        if id(runner._db) not in databases:
            continue
        await runner._db.start()
        try:
            for _task, walk in getattr(runner, "_passes", ()):
                if name is not None and walk.name != name:
                    continue
                count = await walk.retry(runner._db)
                if count:
                    queued[walk.name] = queued.get(walk.name, 0) + count
        finally:
            await runner._db.stop()
    return queued


def _print_passes(rows: list[Any]) -> None:
    if not rows:
        print("no passes have run yet")
        return
    header = f"{'PASS':<28} {'STATE':<8} {'PROGRESS':<18} {'ROWS':>11}  ETA"
    print(header)
    print("-" * len(header))
    for row in rows:
        label = row.name if not row.tenant else f"{row.name}@{row.tenant}"
        print(
            f"{label[:28]:<28} {row.state:<8} {_progress_cell(row):<18} "
            f"{row.rows_done:>11}  {_eta_cell(row)}"
        )
        if row.progress.state_reason:
            print(f"{'':<28} {row.progress.state_reason}")
        if row.last_error and row.last_error not in (row.progress.state_reason or ""):
            # A chunk that failed and then recovered leaves this behind until the
            # next advance clears it. Worth showing: it is the difference between
            # a pass that is fine and one that is fine *for now*.
            print(f"{'':<28} last chunk error: {row.last_error}")
        if row.progress.eta_absent:
            print(f"{'':<28} no ETA: {row.progress.eta_absent}")
        if row.holes_open:
            print(
                f"{'':<28} {row.holes_open} dead-lettered chunk(s); the terminal "
                "gate is barred until they clear -- `wreath passes retry`"
            )
        if row.pending:
            print(f"{'':<28} {row.pending} unit(s) queued to be walked out of order")
        if row.verified_at:
            # The gate's durable output, and the thing a migration reads before
            # it agrees to narrow a column. Worth printing next to the walk it
            # came from, because "did this finish?" and "is it safe to deploy
            # the next migration?" are the same question asked twice.
            fact = row.verified_fact or "(no fact named)"
            print(f"{'':<28} verified {row.verified_at}: published {fact}")
        elif row.guards:
            print(
                f"{'':<28} guards {row.guards}, not yet published -- a migration "
                "narrowing that column is refused until it is"
            )


def _progress_cell(row: Any) -> str:
    """``64% (estimated)`` -- never a percentage without where it came from."""
    percent = row.progress.percent
    if percent is None:
        kind = row.progress.denominator_kind
        return f"? ({kind})" if kind else "?"
    return f"{percent:.1f}% ({row.progress.denominator_kind})"


def _eta_cell(row: Any) -> str:
    seconds = row.progress.eta_seconds
    if seconds is None:
        return "-"
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def _print_holes(holes: list[Any]) -> None:
    print()
    if not holes:
        print("no dead-lettered chunks")
        return
    print(f"{len(holes)} dead-lettered chunk(s):")
    for hole in holes:
        label = hole.name if not hole.tenant else f"{hole.name}@{hole.tenant}"
        print(f"\n  {label}  after {hole.attempts} attempt(s), at {hole.failed_at}")
        print(f"    error: {hole.error}")
        print(f"    reproduce: {hole.predicate}")


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


def execute_doctor(namespace: argparse.Namespace) -> int:
    # A protocol client, like `inspect`: the diagnosis is assembled entirely
    # from what the recorder already holds, so nothing has to be reproduced.
    import asyncio
    import json as _json

    from .doctor import diagnose_n_plus_one
    from .inspector import InspectorClient, InspectorError

    async def query() -> list:
        async with InspectorClient(namespace.socket) as client:
            return await diagnose_n_plus_one(
                client, threshold=namespace.threshold, limit=namespace.limit
            )

    try:
        findings = asyncio.run(query())
    except InspectorError as error:
        print(f"wreath doctor: error: {error}", file=sys.stderr)
        return 1
    except (ConnectionError, FileNotFoundError) as error:
        print(f"wreath doctor: cannot reach inspector: {error}", file=sys.stderr)
        return 1

    if namespace.as_json:
        print(_json.dumps({
            "version": 1,
            "check": "n-plus-one",
            "threshold": namespace.threshold,
            "findings": [
                {
                    "route": f.route,
                    "request_id": f.request_id,
                    "queries": f.queries,
                    "summary": f.describe(),
                    "repetitions": [
                        {"model": r.model, "count": r.count, "total_us": r.total_us}
                        for r in f.repetitions
                    ],
                }
                for f in findings
            ],
        }))
    else:
        _print_n_plus_one(findings, namespace.threshold)
    return 1 if findings and namespace.strict else 0


def _print_n_plus_one(findings: list, threshold: int) -> None:
    if not findings:
        print(
            f"no request queried one model {threshold} or more times. "
            "Note this reads sampled traces: a Detailed recorder sees more."
        )
        return
    print(f"{len(findings)} request(s) queried one model {threshold}+ times:\n")
    for finding in findings:
        print(f"  {finding.describe()}")
        for repetition in finding.repetitions:
            millis = repetition.total_us / 1000
            print(
                f"      {repetition.model:<24} {repetition.count:>5} queries "
                f"{millis:>8.1f}ms"
            )
        print(f"      replay it: wreath replay --request {finding.request_id}\n")
    print("An eager load usually collapses these into one statement.")


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

    if action == "to-test":
        source = asyncio.run(
            rp.generate_test(
                app,
                rp.open_recording(namespace.recording),
                target=namespace.target,
                name=namespace.name,
                origin=namespace.recording,
            )
        )
        if namespace.output:
            with open(namespace.output, "w", encoding="utf-8") as handle:
                handle.write(source)
            print(f"wrote {namespace.output}")
        else:
            print(source, end="")
        return 0

    if action == "transport":
        recording = rp.open_recording(namespace.recording)
        schedule = None
        if namespace.inject:
            schedule = rp.FaultSchedule.from_bytes(_read_bytes(namespace.inject))
        protocol_cls = None
        if namespace.pure:
            from ._pure.server import Http1Protocol as protocol_cls
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
        if namespace.command == "doctor":
            return execute_doctor(namespace)
        if namespace.command == "capture":
            return execute_capture(namespace)
        if namespace.command == "replay":
            return execute_replay(namespace)
        if namespace.command == "passes":
            try:
                return execute_passes(namespace)
            except (OSError, KeyError, ValueError) as error:
                raise CliError(str(error), exit_code=2) from error
        if namespace.command == "schema":
            try:
                return execute_schema(namespace)
            except (OSError, KeyError, ValueError) as error:
                raise CliError(str(error), exit_code=2) from error
        if namespace.command == "migrations":
            from ._migrations_cli import execute as execute_migrations

            try:
                return execute_migrations(namespace, load_application)
            except (OSError, RuntimeError, ValueError) as error:
                raise CliError(str(error), exit_code=2) from error
        if namespace.command == "docs":
            from ._docs_cli import execute as execute_docs

            return execute_docs(namespace)
        if namespace.command == "port":
            from ._port.cli import execute as execute_port

            try:
                return execute_port(namespace)
            except (OSError, ValueError) as error:
                raise CliError(str(error), exit_code=2) from error
        if namespace.command == "audit":
            from ._audit.cli import execute as execute_audit

            try:
                return execute_audit(namespace, load_application)
            except (OSError, ValueError) as error:
                raise CliError(str(error), exit_code=2) from error
        options = options_from_namespace(namespace)
        execute(options)
    except CliError as error:
        print(f"wreath: error: {error}", file=sys.stderr)
        return error.exit_code
    return 0


# Without this guard `python -m wreath._cli docs check` imports the module,
# runs nothing, and exits 0 in a fraction of a second -- indistinguishable from
# a gate that passed. That misread a docs build as clean during development, so
# the entry point is spelled out here rather than left to the console script.
if __name__ == "__main__":
    raise SystemExit(main())
