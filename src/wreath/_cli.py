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

#: Controls sampled by `wreath test` when nobody asks for a number.
#:
#: **The sample count is very nearly free; the mutation phase is not.** That is
#: worth stating because the opposite was assumed once and acted on. Measured
#: here as an interleaved A/B against a pristine checkout, no DSN, three warm
#: rounds each, by subtracting the same tree's `--mutant off` time from its
#: as-typed time:
#:
#:     192 controls   101.82s +/- 0.07 total   ->  62.66s of mutation
#:      48 controls    91.44s +/- 0.15 total   ->  59.02s of mutation
#:
#: Three quarters of the controls for 3.6 seconds. The tail is roughly 55s of
#: fixed cost -- catalog build, baseline seal, the live probe window -- plus
#: about 0.02s per control, so cutting the sample trades most of the evidence
#: (123-124 gold files at 192, against 34-40 at 48) for noise.
#:
#: An older curve in `AGENTS.md` recorded 76.1s at 192 against 33.2s at 48 and
#: reads as though the count dominates. It does not reproduce on this tree; the
#: numbers above are the ones to trust, and the way to move this phase is to
#: attack the fixed cost rather than the sample.
_DEFAULT_MUTANT_SAMPLES = 192


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
    max_body_chunks: int
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
                max_body_chunks=self.max_body_chunks,
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
    parser.add_argument("--max-body-chunks", type=int, default=defaults.max_body_chunks)
    parser.add_argument("--read-high-water", type=int, default=defaults.read_high_water)
    parser.add_argument(
        "--read-high-water-messages", type=int, default=defaults.read_high_water_messages
    )
    parser.add_argument("--response-high-water", type=int, default=defaults.response_high_water)
    parser.add_argument("--response-low-water", type=int, default=defaults.response_low_water)
    parser.add_argument(
        "--response-high-water-segments",
        type=int,
        default=defaults.response_high_water_segments,
    )
    parser.add_argument(
        "--response-low-water-segments",
        type=int,
        default=defaults.response_low_water_segments,
    )
    parser.add_argument("--max-ws-fragments", type=int, default=defaults.max_ws_fragments)
    parser.add_argument("--lifespan", choices=("auto", "on", "off"), default=defaults.lifespan)
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
    parser.add_argument("--initial-stream-window", type=int, default=defaults.initial_stream_window)
    parser.add_argument(
        "--initial-connection-window", type=int, default=defaults.initial_connection_window
    )
    parser.add_argument("--max-header-list-bytes", type=int, default=defaults.max_header_list_bytes)
    parser.add_argument("--hpack-table-bytes", type=int, default=defaults.hpack_table_bytes)
    parser.add_argument("--qpack-table-bytes", type=int, default=defaults.qpack_table_bytes)
    parser.add_argument("--qpack-blocked-streams", type=int, default=defaults.qpack_blocked_streams)
    parser.add_argument("--loop", choices=("asyncio", "uvloop", "metal"), default="asyncio")
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
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
    dev_parser.add_argument("--reload-include", action="append", dest="reload_includes", default=[])
    dev_parser.add_argument("--reload-exclude", action="append", dest="reload_excludes", default=[])
    dev_parser.add_argument("--reload-delay", type=float, default=0.25)
    dev_parser.add_argument("--reload-debounce", type=float, default=0.10)
    typegen_parser = commands.add_parser(
        "typegen", help="generate consumer type contracts from typed routes"
    )
    typegen_parser.add_argument("target", help="application target as module:attribute")
    typegen_parser.add_argument(
        "--target",
        dest="typegen_target",
        default="typescript",
        choices=("typescript", "python"),
        metavar="TARGET",
        help="output target: typescript or python (default: typescript)",
    )
    typegen_parser.add_argument(
        "--class-name",
        dest="typegen_class_name",
        default="GeneratedServiceClient",
        metavar="NAME",
        help="python target: name of the generated ServiceClient subclass",
    )
    typegen_parser.add_argument("--output", required=True, metavar="PATH")
    typegen_parser.add_argument("--react-query", action="store_true")
    typegen_parser.add_argument("--base-url-env", metavar="NAME", default=None)
    typegen_parser.add_argument("--check", action="store_true")
    typegen_parser.add_argument("--title", default="Wreath")
    typegen_parser.add_argument("--api-version", default="0.1.0")
    strictness = typegen_parser.add_mutually_exclusive_group()
    strictness.add_argument("--strict", dest="allow_unknown", action="store_false", default=False)
    strictness.add_argument("--allow-unknown", dest="allow_unknown", action="store_true")
    typegen_parser.add_argument(
        "--factory",
        action="store_true",
        help="invoke the target as a zero-argument application factory",
    )
    migrations_parser = commands.add_parser(
        "migrations", help="inspect and run Wreath-metal PostgreSQL migrations"
    )
    migration_actions = migrations_parser.add_subparsers(dest="migration_action", required=True)
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
    migration_baseline = migration_actions.add_parser(
        "baseline",
        help="review and adopt a matching live schema without replaying old migrations",
    )
    migration_baseline.add_argument("target", help="application target as module:attribute")
    migration_baseline.add_argument("--database", default="main")
    migration_baseline.add_argument("--factory", action="store_true")
    migration_baseline.add_argument("--output", required=True, metavar="DIRECTORY")
    migration_baseline.add_argument("--migration-id", required=True, metavar="32_HEX")
    migration_baseline.add_argument(
        "--adopt",
        action="store_true",
        help="record the reviewed root in Wreath history after re-verifying it",
    )
    migration_baseline.add_argument("--dsn-env", default="WREATH_MIGRATION_DSN")
    migration_baseline.add_argument("--json", action="store_true")
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
        "--force",
        action="store_true",
        help="downgrade even when live ORM code still maps a dropped/retyped object",
    )
    migration_down.add_argument("--dsn-env", default="WREATH_MIGRATION_DSN")
    migration_down.add_argument("--json", action="store_true")

    from ._privacy.cli import add_privacy_parser
    from .infra.cli import add_infra_parser

    add_infra_parser(commands)
    add_privacy_parser(commands)
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
            "config",
            nargs="?",
            default="wreath_docs.py",
            help="the Python config module exposing `site = Site(...)`",
        )
        if _action == "serve":
            _sub.add_argument("--port", type=int, default=8000, help="preview port")
            _sub.add_argument(
                "--no-reload",
                action="store_true",
                help="do not watch the source tree and rebuild on change",
            )

    port_parser = commands.add_parser(
        "port", help="port an existing FastAPI app to Wreath (report or emit)"
    )
    port_parser.add_argument(
        "source",
        nargs="*",
        help="one or more app roots (omitted with --verify)",
    )
    port_parser.add_argument(
        "--report-only",
        action="store_true",
        default=True,
        help="static analysis + report (default when neither --output nor --in-place is given)",
    )
    port_parser.add_argument(
        "--json", action="store_true", dest="as_json", help="emit the machine-readable report JSON"
    )
    port_parser.add_argument(
        "--inventory",
        action="store_true",
        help="inventory routes, security, integrations, and dependencies per project",
    )
    port_parser.add_argument(
        "--target-python",
        default="3.14",
        metavar="VERSION",
        help="Python version checked against every project and lock declaration (default: 3.14)",
    )
    port_parser.add_argument(
        "--migration-strategy",
        choices=("preserve", "baseline"),
        default="preserve",
        help=(
            "preserve migration history, or report already-applied history as "
            "retired by a reviewed baseline"
        ),
    )
    inventory = port_parser.add_mutually_exclusive_group()
    inventory.add_argument(
        "--write-inventory",
        metavar="PATH",
        help="write the canonical migration inventory JSON atomically",
    )
    inventory.add_argument(
        "--check-inventory",
        metavar="PATH",
        help="fail when the canonical inventory JSON differs from PATH",
    )
    port_parser.add_argument(
        "--write-cedar",
        metavar="PATH",
        help="write a compiled, fail-closed Cedar policy/decorator module for inventory guards",
    )
    port_parser.add_argument(
        "--cedar-semantics",
        metavar="PATH",
        help=(
            "JSON policy semantics for --write-cedar: default/action/condition "
            "expressions and authentication-only dependency factories"
        ),
    )
    port_parser.add_argument(
        "--by-rule",
        action="store_true",
        help="cluster the findings needing a decision by rule, heaviest first, "
        "instead of listing them one per line in file order",
    )
    port_parser.add_argument(
        "--rule",
        action="append",
        metavar="ID",
        help="show only this rule's sites (repeatable, e.g. --rule orm.query.filter)",
    )
    port_parser.add_argument(
        "--context",
        type=int,
        default=0,
        metavar="N",
        help="show N source lines either side of each site (implies the site view)",
    )
    port_emit = port_parser.add_mutually_exclusive_group()
    port_emit.add_argument(
        "--in-place",
        action="store_true",
        help="rewrite files in place (Phase 1 declarative emit; requires --force)",
    )
    port_emit.add_argument(
        "--output",
        metavar="DIR",
        help="write ported code to a sister tree (Phase 1 declarative emit)",
    )
    port_parser.add_argument(
        "--force", action="store_true", help="allow --in-place and overwrite hand-edited outputs"
    )
    port_parser.add_argument(
        "--opinionated",
        action="store_true",
        help="make the changes that reach past one file, instead of leaving a note: "
        "give a function that runs queries the session parameter it needs "
        "(its callers then have to pass one)",
    )
    port_parser.add_argument(
        "--verify",
        nargs=2,
        metavar=("SOURCE_APP", "CANDIDATE_APP"),
        help="run source and candidate module:attribute ASGI apps against one corpus",
    )
    port_parser.add_argument(
        "--cases",
        metavar="PATH",
        help="JSON request corpus for --verify",
    )
    port_parser.add_argument(
        "--ignore-header",
        action="append",
        default=["date", "server"],
        metavar="NAME",
        help="response header excluded from --verify (repeatable; Date and Server by default)",
    )
    mutant_parser = commands.add_parser(
        "mutant",
        help="remove one declared control at a time and see whether the tests notice",
    )
    from ._mutant.cli import add_arguments as _add_mutant_arguments

    _add_mutant_arguments(mutant_parser)
    test_parser = commands.add_parser(
        "test",
        help="run pytest with an animated file heat map and duration profiling",
        description=(
            "Run a pytest-compatible suite with Wreath's activity grid and timing report. "
            "Arguments not recognized here are forwarded to pytest in their original order."
        ),
    )
    test_parser.add_argument(
        "--grid",
        choices=("auto", "always", "never"),
        default="auto",
        help="animate on a TTY, force animation, or print only the final report",
    )
    test_parser.add_argument(
        "--workers",
        default="auto",
        metavar="N",
        help="pytest worker processes: auto (capped at 8) or a positive integer",
    )
    test_parser.add_argument(
        "--slowest",
        type=int,
        default=5,
        metavar="N",
        help="number of slowest tests in the final report (default: 5)",
    )
    test_parser.add_argument(
        "--report",
        metavar="PATH",
        help="write the complete run, per-file, and per-test timings as JSON",
    )
    test_parser.add_argument(
        "--history",
        default=".wreath/test-history.json",
        metavar="PATH",
        help="bounded duration history used by future scheduling",
    )
    test_parser.add_argument(
        "--no-history",
        action="store_true",
        help="do not read or update duration history",
    )
    test_parser.add_argument(
        "--mutant",
        choices=("auto", "off", "sample", "changed", "full"),
        default="auto",
        help="after the ordinary run, measure its green tests' mutation confidence: "
        "a stable sample, "
        "controls changed from a ref, or a complete sweep (default: auto sample)",
    )
    test_parser.add_argument(
        "--mutant-samples",
        type=int,
        default=_DEFAULT_MUTANT_SAMPLES,
        metavar="N",
        help=f"number of whole-corpus controls in --mutant sample "
        f"(default: {_DEFAULT_MUTANT_SAMPLES}; 192 is the deep sweep)",
    )
    test_parser.add_argument(
        "--mutant-workers",
        default="auto",
        metavar="N|auto",
        help="mutant children to run concurrently after preparation overlaps "
        "the ordinary suite (default: auto, capped at 3 live and reclaiming "
        "up to 6 worker slots after the suite seals)",
    )
    test_parser.add_argument(
        "--mutant-path",
        action="append",
        default=[],
        metavar="PATH",
        help="source path included in mutation confidence (repeatable)",
    )
    test_parser.add_argument(
        "--mutant-tests",
        action="append",
        default=[],
        metavar="PATH",
        help="test path used by the mutation phase (repeatable; default: tests/)",
    )
    test_parser.add_argument(
        "--mutant-operator",
        action="append",
        default=[],
        metavar="PREFIX",
        help="mutation operator prefix included in confidence (repeatable)",
    )
    test_parser.add_argument(
        "--mutant-only",
        action="append",
        default=[],
        metavar="TEXT",
        help="include mutation identifiers containing TEXT (repeatable)",
    )
    test_parser.add_argument(
        "--mutant-pytest-arg",
        action="append",
        default=[],
        metavar="ARG",
        help="extra argument passed to mutation-phase pytest (repeatable)",
    )
    test_parser.add_argument(
        "--mutant-timeout",
        type=float,
        default=60.0,
        metavar="SECONDS",
        help="deadline for each selected mutant (default: 60)",
    )
    test_parser.add_argument(
        "--mutant-max-candidates",
        type=int,
        default=4000,
        metavar="N",
        help="maximum tests one mutant may select (default: 4000)",
    )
    test_parser.add_argument(
        "--mutant-maxfail",
        type=int,
        default=1,
        metavar="N",
        help="stop each mutant at N failures; 0 runs every candidate (default: 1)",
    )
    test_parser.add_argument(
        "--mutant-budget",
        type=float,
        default=50.0,
        metavar="SECONDS",
        help="post-suite execution ceiling for auto/sample mutants; live probes "
        "stop at the suite seal and do not spend it (default: 50)",
    )
    test_parser.add_argument(
        "--mutant-changed",
        default="HEAD",
        metavar="REF",
        help="git reference used by --mutant changed (default: HEAD)",
    )
    test_parser.add_argument(
        "--mutant-fail-on-survivor",
        action="store_true",
        help="make survived or unreached sampled controls fail the command",
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
        "--static",
        action="append",
        metavar="DIR",
        default=[],
        help="also audit *.html under DIR (repeatable)",
    )
    audit_static.add_argument("--title", default="Wreath", help="docs title used when rendering")
    audit_static.add_argument("--version", default="0.1.0", help="docs version used when rendering")
    audit_static.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="emit the machine-readable report JSON",
    )
    audit_static.add_argument(
        "--strict", action="store_true", help="exit non-zero on warnings as well as errors"
    )
    audit_static.add_argument(
        "--fix",
        action="store_true",
        help="apply the safe auto-fix subset to static HTML (and suggest patches for the docs)",
    )
    audit_runtime = audit_actions.add_parser(
        "runtime", help="audit a running server's live responses (headers + HTML)"
    )
    audit_runtime.add_argument("url", nargs="?", help="base URL of the running app")
    audit_runtime.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="emit the machine-readable report JSON",
    )
    audit_runtime.add_argument(
        "--strict", action="store_true", help="exit non-zero on warnings as well as errors"
    )
    audit_code = audit_actions.add_parser(
        "code", help="audit application source for security defect patterns"
    )
    audit_code.add_argument(
        "paths",
        nargs="*",
        default=["."],
        metavar="PATH",
        help="files or directories to scan (default: the working directory)",
    )
    audit_code.add_argument(
        "--tests",
        action="store_true",
        help="include test directories, which legitimately trip several rules",
    )
    audit_code.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="emit the machine-readable report JSON",
    )
    audit_code.add_argument(
        "--strict", action="store_true", help="exit non-zero on warnings as well as errors"
    )
    inspect_parser = commands.add_parser(
        "inspect", help="query a running server's read-only telemetry Inspector"
    )
    inspect_parser.add_argument("socket", help="path to the Inspector's Unix-domain socket")
    inspect_parser.add_argument(
        "topic",
        nargs="?",
        default="summary",
        choices=(
            "summary",
            "active",
            "routes",
            "explain-route",
            "explain-plan",
            "metadata",
            "timeline",
            "failures",
            "distributions",
        ),
        help="what to show (default: summary = workers + pressure)",
    )
    inspect_parser.add_argument("--route-id", type=int, default=None)
    inspect_parser.add_argument("--method", default=None)
    inspect_parser.add_argument("--path", default=None)
    inspect_parser.add_argument("--plan-id", type=int, default=None)
    inspect_parser.add_argument(
        "--table",
        default=None,
        help="metadata table name for the metadata topic",
    )
    inspect_parser.add_argument("--offset", type=int, default=0)
    inspect_parser.add_argument("--limit", type=int, default=50)
    inspect_parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="print versioned JSON instead of tables",
    )
    _add_new_parser(commands)
    _add_ci_parser(commands)
    _add_capabilities_parser(commands)
    _add_mcp_parser(commands)
    _add_doctor_parser(commands)
    _add_capture_parser(commands)
    _add_replay_parser(commands)
    _add_passes_parser(commands)
    _add_jobs_parser(commands)
    _add_schema_parser(commands)
    _add_flight_parser(commands)
    return parser


def _add_new_parser(commands: Any) -> None:
    """`wreath new NAME` -- a project that runs, tests green, and is wired right."""
    from ._ci import FORGES as _FORGES

    parser = commands.add_parser(
        "new",
        help="write a new wreath project that already runs and tests green",
        description=(
            "Generate a project laid out the documented way, with the dotenv "
            "dialect, the router/config split, the app factory and a working "
            "test suite already correct. Refuses a directory that has anything "
            "in it; there is no --force."
        ),
    )
    parser.add_argument("name", help="the project and package name (an importable one)")
    parser.add_argument(
        "--directory",
        default=".",
        metavar="PATH",
        help="where to create it (default: the working directory)",
    )
    parser.add_argument(
        "--frontend",
        choices=("none", "react"),
        default="none",
        help="also write a React app wired to `wreath typegen` (default: none)",
    )
    parser.add_argument(
        "--profile",
        choices=("service", "modular-monolith"),
        default="service",
        help="project layout: a small service or bounded-context modular monolith "
        "(default: service)",
    )
    parser.add_argument(
        "--database",
        choices=("none", "postgres"),
        default="none",
        help="also declare an ORM model and register a database (default: none)",
    )
    parser.add_argument(
        "--tenancy",
        action="store_true",
        help="isolate tenants by PostgreSQL role: a directory, the resolving "
        "middleware, and tenant-bound sessions (needs --database postgres)",
    )
    parser.add_argument(
        "--forge",
        choices=("none", *_FORGES),
        default="none",
        help="also write CI for the host this will live on: lint, tests and "
        "preflight (default: none). `codeberg` and `forgejo` are the same "
        "file",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="print the written paths as JSON instead of a report",
    )


def execute_new(namespace: argparse.Namespace) -> int:
    """Write a new project. A refusal is exit 2, as for any other usage error."""
    import json as _json

    from ._scaffold import Options, ScaffoldError, create

    options = Options(
        name=namespace.name,
        directory=Path(namespace.directory),
        frontend=namespace.frontend,
        profile=namespace.profile,
        database=namespace.database,
        tenancy=namespace.tenancy,
        forge=namespace.forge,
    )
    try:
        written = create(options)
    except ScaffoldError as error:
        raise CliError(str(error), exit_code=2) from error
    if namespace.as_json:
        print(
            _json.dumps(
                {
                    "version": 1,
                    "project": options.name,
                    "directory": str(options.target),
                    "files": written,
                }
            )
        )
        return 0
    print(f"wrote {len(written)} file(s) to {options.target}\n")
    for relative in written:
        print(f"  {relative}")
    print(
        f"\nnext:\n  cd {options.target}\n  cp .env.example .env\n  pytest"
        f"\n  wreath dev {options.name}.app:app"
    )
    return 0


def _add_ci_parser(commands: Any) -> None:
    """`wreath ci init` -- the same CI files, for a project that already exists.

    `wreath new --forge` covers a project being started. This covers the two
    cases it cannot: a project started before the flag existed, and one that is
    mirrored to a second host -- `--forge` is repeatable for exactly that.
    """
    from ._ci import FORGES as _FORGES

    parser = commands.add_parser(
        "ci",
        help="write continuous integration for the forge this project lives on",
        description=(
            "Write the lint, test and preflight pipeline for GitHub, GitLab, "
            "Codeberg/Forgejo or Gitea. Refuses to write over a CI file that is "
            "already there; there is no --force."
        ),
    )
    actions = parser.add_subparsers(dest="ci_action", required=True)
    init = actions.add_parser("init", help="write the CI files for one or more forges")
    init.add_argument(
        "--forge",
        action="append",
        required=True,
        choices=_FORGES,
        metavar="FORGE",
        help=f"which forge to write for, repeatable; one of: {', '.join(_FORGES)}",
    )
    init.add_argument(
        "--directory",
        default=".",
        metavar="PATH",
        help="the project root (default: the working directory)",
    )
    init.add_argument(
        "--name",
        default=None,
        help="the importable package name, used to spell the preflight target "
        "(default: read from pyproject.toml)",
    )
    init.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="print the written paths as JSON instead of a report",
    )


def _project_name(directory: Path, given: str | None) -> str:
    """The package name the preflight target is spelled from.

    Read from `pyproject.toml` rather than taken from the directory name. A
    checkout is routinely called something else -- `shop-api`, or `main` under a
    CI provider -- and a preflight target built from that names a module that
    does not import, in a file nobody runs until it is in front of a pull
    request.
    """
    if given is not None:
        return given
    manifest = directory / "pyproject.toml"
    if not manifest.exists():
        raise CliError(
            f"no {manifest} to read the project name from; pass --name with the "
            "importable package name (the one `import <name>` uses)",
            exit_code=2,
        )
    import tomllib

    try:
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise CliError(f"could not read {manifest}: {error}", exit_code=2) from error
    name = data.get("project", {}).get("name")
    if not isinstance(name, str) or not name:
        raise CliError(
            f"{manifest} declares no [project] name; pass --name instead",
            exit_code=2,
        )
    # A distribution name is hyphenated where an import is not, and the
    # preflight target is an import.
    return name.replace("-", "_")


def execute_ci(namespace: argparse.Namespace) -> int:
    """Write CI for every named forge, or refuse before writing any of them."""
    import json as _json

    from ._ci import plan as ci_plan
    from ._ci import render as ci_render

    if namespace.ci_action != "init":  # pragma: no cover - argparse rejects first
        raise ValueError(f"unknown ci action {namespace.ci_action!r}")
    directory = Path(namespace.directory)
    if not directory.is_dir():
        raise CliError(f"{directory} is not a directory", exit_code=2)
    ci = ci_plan(_project_name(directory, namespace.name))

    # Every forge rendered and every collision found before one byte is written,
    # so `--forge github --forge gitlab` cannot leave one of the two behind.
    files: dict[str, str] = {}
    for forge in dict.fromkeys(namespace.forge):
        files.update(ci_render(ci, forge))
    clashes = sorted(name for name in files if (directory / name).exists())
    if clashes:
        raise CliError(
            f"{', '.join(clashes)} already exists; wreath ci never writes over a "
            "pipeline somebody is relying on. Move it aside first.",
            exit_code=2,
        )
    for relative, content in sorted(files.items()):
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    written = sorted(files)
    if namespace.as_json:
        print(
            _json.dumps(
                {
                    "version": 1,
                    "project": ci.project,
                    "directory": str(directory),
                    "files": written,
                }
            )
        )
        return 0
    print(f"wrote {len(written)} file(s) to {directory}\n")
    for relative in written:
        print(f"  {relative}")
    return 0


def _add_capabilities_parser(commands: Any) -> None:
    """`wreath capabilities [TERM]` -- what already ships that answers a word.

    Deliberately takes no application target and opens nothing. It reads an
    index compiled into the package, so it answers before a project exists,
    which is when the question is actually asked.
    """
    parser = commands.add_parser(
        "capabilities",
        help="what wreath already ships that answers a word you know",
        description=(
            "Look a capability up by the name you would otherwise install "
            "(`celery`), by subsystem or module (`jobs`, `wreath.messaging`), or "
            "by a word in its description. Every capability that answers is "
            "listed, strongest match first -- one word is often several "
            "subsystems, and stopping at the first is how the others get "
            "reimplemented. With no term, prints all of them."
        ),
    )
    parser.add_argument(
        "term",
        nargs="?",
        default=None,
        help="the package, module, subsystem or word to look up",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="print JSON instead of a report",
    )


def _add_flight_parser(commands: Any) -> None:
    """`wreath flight read` -- what the recorder still held when it died.

    The one command here that is used *after* something has gone wrong, and the
    only one that needs no running server: a ring file is a mapping the dead
    process left behind, and reading it is a file operation.
    """
    flight_parser = commands.add_parser(
        "flight", help="read a flight recorder file left behind by a crash"
    )
    actions = flight_parser.add_subparsers(dest="flight_action", required=True)

    read = actions.add_parser(
        "read",
        help="decode a recorder file: a WFRR ring, or a WFR1 recording and the "
        "job attempts inside it",
    )
    read.add_argument("path", help="a WFRR ring file or a WFR1 recording")
    read.add_argument(
        "--kind",
        default=None,
        choices=("completion", "correlation", "phase", "log", "client-facts"),
        help="show only records of one kind (ring files only)",
    )
    read.add_argument("--limit", type=int, default=50, help="records to print (0 = all)")
    read.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="print JSON instead of a human summary",
    )

    reproduce = actions.add_parser(
        "replay",
        help="re-drive a recorded request and check it retraces the crash",
    )
    reproduce.add_argument("path", help="the ring file the crash left behind")
    reproduce.add_argument("recording", help="a WTR1 transport recording of the request")
    reproduce.add_argument("target", help="the application, as module:attribute")
    reproduce.add_argument("--factory", default=None, help="callable that builds the application")
    reproduce.add_argument(
        "--request-id",
        type=int,
        default=None,
        help="which request from the ring to check against (default: the one that was in flight)",
    )
    reproduce.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="print JSON instead of a human summary",
    )


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
        "--component",
        default=None,
        help="one component by name (default: every registered one)",
    )
    emit.add_argument(
        "--from-version",
        type=int,
        default=0,
        dest="from_version",
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
    status = actions.add_parser("status", help="show every pass's phase, position, and pacing")
    status.add_argument("target", help="application target as module:attribute")
    status.add_argument("--factory", action="store_true")
    status.add_argument(
        "--database",
        default=None,
        help="read one database's ledger (default: every one a job runner uses)",
    )
    status.add_argument("--schema", default=None, help="ledger schema (default: the job runner's)")
    status.add_argument("--name", default=None, help="one pass by name")
    status.add_argument(
        "--holes",
        action="store_true",
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


def _add_jobs_parser(commands: Any) -> None:
    """`wreath jobs list` -- what is in the durable queue, and what caused it.

    One read of one table, like `wreath passes status`, and it defaults to the
    dead letters because that is the row somebody is looking for at three in the
    morning. Each row prints the trace id of the request that enqueued it, which
    is the whole point of the column: the request finished hours ago and is
    otherwise unrecoverable from the failure.
    """
    jobs_parser = commands.add_parser("jobs", help="report on the durable job queue")
    actions = jobs_parser.add_subparsers(dest="jobs_action", required=True)
    listing = actions.add_parser("list", help="show queue rows, dead-lettered ones by default")
    listing.add_argument("target", help="application target as module:attribute")
    listing.add_argument("--factory", action="store_true")
    listing.add_argument(
        "--database",
        default=None,
        help="read one database's queue (default: every one a job runner uses)",
    )
    listing.add_argument("--schema", default=None, help="queue schema (default: the job runner's)")
    listing.add_argument(
        "--state",
        action="append",
        default=None,
        dest="states",
        help="a state to include; repeatable (default: dead)",
    )
    listing.add_argument(
        "--all",
        action="store_true",
        dest="every_state",
        help="every state, not just the dead letters",
    )
    listing.add_argument("--queue", default=None, help="one queue by name")
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--json", action="store_true", dest="as_json")


def execute_jobs(namespace: argparse.Namespace) -> int:
    """Read the queue and print it, trace id included."""
    import asyncio
    import json as _json

    application = load_application(namespace.target, factory=namespace.factory)
    targets = _pass_ledgers(application, namespace)
    if not targets:
        raise CliError(
            "no job queue to read: the application configures no job runner, "
            "and no --database was given",
            exit_code=2,
        )
    states: tuple[str, ...]
    if namespace.every_state:
        states = ()
    elif namespace.states:
        states = tuple(namespace.states)
    else:
        states = ("dead",)
    rows = asyncio.run(
        _read_jobs(targets, states=states, queue=namespace.queue, limit=namespace.limit)
    )
    if namespace.as_json:
        print(_json.dumps({"jobs": [row.as_dict() for row in rows]}, indent=2))
        return 0
    _print_jobs(rows, states)
    return 0


async def _read_jobs(
    targets: list[tuple[Any, str]],
    *,
    states: tuple[str, ...],
    queue: str | None,
    limit: int,
) -> list[Any]:
    from .jobs import read_jobs

    rows: list[Any] = []
    for database, schema in targets:
        await database.start()
        try:
            connection = await database.acquire("write")
            try:
                rows.extend(
                    await read_jobs(
                        connection,
                        schema=schema,
                        states=states,
                        queue=queue,
                        limit=limit,
                    )
                )
            finally:
                await database.release("write", connection)
        finally:
            await database.stop()
    return rows


def _print_jobs(rows: list[Any], states: tuple[str, ...]) -> None:
    if not rows:
        wanted = ", ".join(states) if states else "any state"
        print(f"no jobs in the queue ({wanted})")
        return
    header = f"{'ID':>10}  {'TASK':<28} {'STATE':<9} {'TRIES':>6}  UPDATED"
    print(header)
    print("-" * len(header))
    for row in rows:
        label = row.task if not row.tenant else f"{row.task}@{row.tenant}"
        print(
            f"{row.id:>10}  {label[:28]:<28} {row.state:<9} "
            f"{row.attempts:>3}/{row.max_attempts:<2}  {row.updated_at}"
        )
        if row.last_error:
            print(f"{'':>12}error: {row.last_error}")
        # Printed for a failed row above all: the request that enqueued it
        # finished hours ago, and this identifier is what joins the two.
        if row.trace_id:
            print(f"{'':>12}trace: {row.trace_id}  (wreath doctor trace {row.trace_id})")
        elif row.state == "dead":
            print(
                f"{'':>12}trace: none -- enqueued outside a traced request, or "
                "this database predates the trace_context column"
            )


def _add_mcp_parser(commands: Any) -> None:
    """`wreath mcp stdio` -- the MCP endpoint you already have, behind a pipe.

    The supported deployment is the HTTP endpoint, because that is where
    authorization and the audit trail are worth anything. This exists for the
    editor on someone's laptop that speaks only stdio, and it is a byte relay
    over the application's own route rather than a second server: routing, auth,
    `MCPLimits` and the Flight Recorder marker are the ones `/mcp` has, because
    it is `/mcp`.
    """
    mcp_parser = commands.add_parser(
        "mcp", help="serve an application's MCP endpoint over a local transport"
    )
    actions = mcp_parser.add_subparsers(dest="mcp_action", required=True)
    stdio = actions.add_parser(
        "stdio", help="relay newline-delimited JSON-RPC between stdin/stdout and /mcp"
    )
    stdio.add_argument("target", metavar="MODULE[:ATTRIBUTE]")
    stdio.add_argument(
        "--factory",
        action="store_true",
        help="invoke the target as a zero-argument application factory",
    )
    stdio.add_argument(
        "--path",
        default="/mcp",
        help="the MCP endpoint's path on that application (default: /mcp)",
    )


def execute_mcp(namespace: argparse.Namespace) -> int:
    """Drive the application's MCP route over stdin/stdout."""
    import asyncio

    from ._mcp.stdio import serve

    app = load_application(namespace.target, factory=namespace.factory)
    return asyncio.run(serve(app, path=namespace.path))


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
    n_plus_one.add_argument("socket", help="path to the Inspector's Unix-domain socket")
    n_plus_one.add_argument(
        "--threshold",
        type=int,
        default=10,
        help="queries of one model within one request before it counts (default: 10)",
    )
    n_plus_one.add_argument(
        "--limit",
        type=int,
        default=256,
        help="how many recent traces to scan (default: 256)",
    )
    n_plus_one.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="print versioned JSON instead of a report",
    )
    n_plus_one.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero when anything is found, for a CI gate",
    )

    pre = actions.add_parser(
        "preflight",
        help="every refusal wreath already knows, asked at once before you deploy",
        description=(
            "Load an application and report what would stop it: the gaps "
            "`wreath infra infer` derives, the configuration defects the "
            "hardening ruleset reads off the object graph, and which routes ask "
            "nothing of the caller. Opens no socket, database or DNS resolver -- "
            "and prints what that leaves unchecked, with the command for each."
        ),
    )
    pre.add_argument("target", help="application target as module:attribute")
    pre.add_argument("--factory", action="store_true")
    pre.add_argument(
        "--settings",
        action="append",
        default=[],
        metavar="SPEC",
        help="a settings dataclass whose environment contract to check, as "
        "module:Class or module:Class=PREFIX (repeatable)",
    )
    pre.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="PATH",
        help="a dotenv file supplying keys (repeatable)",
    )
    pre.add_argument(
        "--environ",
        action="store_true",
        help="treat this process's environment as a supplier too",
    )
    pre.add_argument("--json", action="store_true", dest="as_json")

    routes = actions.add_parser(
        "routes",
        help="emit a deterministic route, wire-type and security manifest",
    )
    routes.add_argument("target", help="application target as module:attribute")
    routes.add_argument("--factory", action="store_true")
    routes.add_argument(
        "--write",
        metavar="PATH",
        help="write the canonical JSON manifest to PATH instead of stdout",
    )
    routes.add_argument(
        "--check",
        metavar="PATH",
        help="exit non-zero when PATH differs from the current manifest",
    )

    trace = actions.add_parser(
        "trace",
        help="show every job, message, workflow and pass carrying one trace id",
    )
    trace.add_argument("trace_id", help="the 32-hex W3C trace id, or a whole traceparent")
    trace.add_argument(
        "target",
        nargs="?",
        default=None,
        help="application target as module:attribute, for the durable half",
    )
    trace.add_argument("--factory", action="store_true")
    trace.add_argument(
        "--database",
        default=None,
        help="read one database (default: every one a job runner uses)",
    )
    trace.add_argument("--schema", default=None, help="wreath's schema (default: the job runner's)")
    trace.add_argument(
        "--workflow-schema",
        default="wreath_system",
        dest="workflow_schema",
        help="where workflow instances live (default: wreath_system)",
    )
    trace.add_argument(
        "--workflow-table",
        default="workflow_steps",
        dest="workflow_table",
        help="the workflow step table's name (default: workflow_steps)",
    )
    trace.add_argument(
        "--socket",
        default=None,
        help="an Inspector socket, to find the recorded request as well",
    )
    trace.add_argument(
        "--limit",
        type=int,
        default=256,
        help="how many recent traces to scan on that socket (default: 256)",
    )
    trace.add_argument("--json", action="store_true", dest="as_json")


def _add_replay_parser(commands: Any) -> None:
    """`wreath replay {transport,plan}` -- replay a recording through the owned
    pipeline. Unlike inspect/capture this loads the target application, because
    replay drives the app's own protocol and endpoint code in-process."""
    replay_parser = commands.add_parser(
        "replay", help="replay a recording through the owned protocol/endpoint pipeline"
    )
    replay_parser.add_argument(
        "--factory",
        action="store_true",
        help="the target is a zero-argument callable returning the application",
    )
    replay_parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="print versioned JSON instead of a human summary",
    )
    actions = replay_parser.add_subparsers(dest="replay_action", required=True)

    transport = actions.add_parser(
        "transport", help="feed a recorded connection into the owned HTTP/1 driver"
    )
    transport.add_argument("target", metavar="MODULE[:ATTRIBUTE]")
    transport.add_argument("recording", metavar="RECORDING", help="path to a .wtr1 recording")
    transport.add_argument(
        "--inject",
        metavar="SCHEDULE",
        default=None,
        help="apply a .wfs1 fault schedule before the bytes reach the parser",
    )
    transport.add_argument(
        "--record-faults",
        metavar="PATH",
        default=None,
        help="write the realized fault schedule that this run applied",
    )

    plan = actions.add_parser(
        "plan", help="replay a canonical request through routing/binding/serialization"
    )
    plan.add_argument("target", metavar="MODULE[:ATTRIBUTE]")
    plan.add_argument("--method", default="GET")
    plan.add_argument("--path", required=True)
    plan.add_argument("--query", default="", help="raw query string")
    plan.add_argument("--body", default="", help="request body (utf-8)")
    plan.add_argument(
        "--header",
        action="append",
        default=[],
        metavar="NAME:VALUE",
        help="a request header (repeatable)",
    )
    plan.add_argument(
        "--mode",
        choices=("invoke", "replace", "skip"),
        default="invoke",
        help="handler boundary: run it, use --replace-body, or resolve only",
    )
    plan.add_argument("--replace-body", default=None, help="REPLACE mode: recorded return string")

    to_test = actions.add_parser(
        "to-test",
        help="write a runnable pytest that re-drives a recorded request or job attempt",
    )
    to_test.add_argument(
        "target",
        metavar="MODULE[:ATTRIBUTE]",
        help="the application for a .wtr1 request, or the JobRunner for a .wfr1 job attempt",
    )
    to_test.add_argument(
        "recording",
        metavar="RECORDING",
        help="a .wtr1 transport recording or a .wfr1 job-attempt recording",
    )
    to_test.add_argument(
        "--output",
        "-o",
        metavar="PATH",
        default=None,
        help="write the test here instead of to stdout",
    )
    to_test.add_argument(
        "--name",
        default=None,
        help="the generated test function's name (derived from the request otherwise)",
    )


def _add_capture_parser(commands: Any) -> None:
    """`wreath capture {arm,status,disarm}` -- the token-gated capture control."""
    capture_parser = commands.add_parser(
        "capture", help="arm/disarm forensic capture on a running server"
    )
    capture_parser.add_argument("socket", help="path to the Inspector's Unix socket")
    capture_parser.add_argument(
        "--token",
        default=None,
        help="capability token (or the WREATH_CAPTURE_TOKEN environment variable)",
    )
    capture_parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="print JSON instead of a human summary",
    )
    actions = capture_parser.add_subparsers(dest="capture_action", required=True)

    arm = actions.add_parser("arm", help="install a bounded, expiring capture arm")
    arm.add_argument(
        "--allow-header",
        action="append",
        dest="allow_headers",
        default=[],
        metavar="NAME",
        help="header captured verbatim (repeatable)",
    )
    arm.add_argument(
        "--hash-header",
        action="append",
        dest="hash_headers",
        default=[],
        metavar="NAME",
        help="header captured as a keyed hash (repeatable)",
    )
    arm.add_argument(
        "--mask-header",
        action="append",
        dest="mask_headers",
        default=[],
        metavar="NAME",
        help="header captured as length only (repeatable)",
    )
    arm.add_argument(
        "--allow-query",
        action="append",
        dest="allow_query",
        default=[],
        metavar="NAME",
        help="query parameter captured verbatim (repeatable)",
    )
    arm.add_argument(
        "--hash-query",
        action="append",
        dest="hash_query",
        default=[],
        metavar="NAME",
        help="query parameter captured as a keyed hash (repeatable)",
    )
    arm.add_argument(
        "--mask-query",
        action="append",
        dest="mask_query",
        default=[],
        metavar="NAME",
        help="query parameter captured as length only (repeatable)",
    )
    arm.add_argument(
        "--body",
        default=None,
        choices=("none", "metadata", "hashed", "structured"),
        help="request/response body capture mode",
    )
    arm.add_argument(
        "--dependency",
        default=None,
        choices=("none", "metadata", "hashed", "structured"),
        help="dependency (DB params/rows, outbound bodies) capture mode",
    )
    arm.add_argument("--max-body-bytes", type=int, default=0)
    arm.add_argument("--max-fields", type=int, default=0)
    arm.add_argument("--max-depth", type=int, default=0)
    arm.add_argument("--slabs", type=int, default=0, help="capture budget: slab count")
    arm.add_argument("--slab-bytes", type=int, default=64 * 1024)
    arm.add_argument(
        "--expiry",
        type=float,
        required=True,
        metavar="SECONDS",
        help="how long the arm stays live (required; no forever arms)",
    )
    arm.add_argument(
        "--max-matches",
        type=int,
        default=0,
        help="stop after this many matches (0 = only expiry bounds it)",
    )

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
        max_body_chunks=namespace.max_body_chunks,
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
    from ._target import parse_target

    try:
        parsed = parse_target(target, label="application", default_attribute="app")
    except ValueError as error:
        raise CliError("application target must use module:attribute syntax") from error
    return parsed.module, parsed.attribute


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
    from ._target import load_target

    _split_target(target)
    _ensure_cwd_importable()
    try:
        selected = load_target(
            target,
            label="application",
            default_attribute="app",
            catch_all_import_errors=True,
        )
    except ValueError as error:
        raise CliError(str(error)) from error
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
    """The tier the startup line reports.

    Always `native` now: `_select_protocol` refuses rather than falling back, so
    a process that gets this far is serving from C. Kept as a function because
    the banner reads it and because the metal tier is a third answer this may
    have to give.
    """
    return "native"


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
        f"\N{HERB} {_version()} serving {target} on {scheme}://{address}  ({', '.join(details)})"
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


def _wait_for_worker_generation(workers: dict[int, tuple[int, int]], timeout: float) -> bool:
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
            worker_id: _spawn_metal_worker(app, config, tls=tls, worker_id=worker_id)
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
                    worker_id: _spawn_metal_worker(app, config, tls=tls, worker_id=worker_id)
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
                replacement = _spawn_metal_worker(app, config, tls=tls, worker_id=worker_id)
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
        factory=namespace.factory,
        title=namespace.title,
        version=namespace.api_version,
        class_name=namespace.typegen_class_name,
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
    seen: set[tuple[Any, str]] = set()
    targets: list[tuple[Any, str]] = []
    for runner in getattr(application, "_job_runners", {}).values():
        pair = (runner._db, namespace.schema or runner._schema)
        if pair not in seen:
            seen.add(pair)
            targets.append(pair)
    return targets


async def _read_pass_ledgers(targets: list[tuple[Any, str]], *, name: str | None) -> list[Any]:
    from .passes import read_status

    return await _read_pass_rows(targets, name=name, read=read_status)


async def _read_pass_holes(targets: list[tuple[Any, str]], *, name: str | None) -> list[Any]:
    from .passes import read_holes

    return await _read_pass_rows(targets, name=name, read=read_holes)


async def _read_pass_rows(
    targets: list[tuple[Any, str]], *, name: str | None, read: Any
) -> list[Any]:
    """Read one pass-ledger view with the shared database lifecycle."""
    rows: list[Any] = []
    for database, schema in targets:
        await database.start()
        try:
            rows.extend(await read(database, schema=schema, name=name))
        finally:
            await database.stop()
    return rows


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
        if row.trace_id and (row.last_error or row.holes_open or row.state == "blocked"):
            # Only where something went wrong, deliberately. On a healthy pass
            # the id is noise on every line; on a failed one it is the single
            # thing that connects a chunk that broke on day three to whatever
            # started the walk.
            print(f"{'':<28} trace: {row.trace_id}  (wreath doctor trace {row.trace_id})")
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
                return await client.timeline(offset=namespace.offset, limit=namespace.limit)
            if topic == "failures":
                return await client.recent_failures(offset=namespace.offset, limit=namespace.limit)
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


def execute_capabilities(namespace: argparse.Namespace) -> int:
    """Answer `wreath capabilities [TERM]`. Exit 1 when a term reaches nothing.

    The non-zero exit is for the caller doing this in a loop over a dependency
    list. It is not a failure of the command, so the report goes to stderr and
    says which word came back empty -- a bare exit code in a build log is a
    question, not an answer.
    """
    import json as _json

    from ._capabilities import index, lookup

    term = namespace.term
    if term is None:
        rows = index()
        if namespace.as_json:
            print(
                _json.dumps(
                    {"version": 1, "term": None, "matches": [_capability_json(row) for row in rows]}
                )
            )
            return 0
        for row in rows:
            print(f"{row.name:<16} {', '.join(row.modules) or 'built in'}")
        print(
            f"\n{len(rows)} capabilities. Pass one of these names, a package you "
            "would otherwise install, or a word, for the detail."
        )
        return 0

    matches = lookup(term)
    if namespace.as_json:
        print(
            _json.dumps(
                {
                    "version": 1,
                    "term": term,
                    "matches": [_capability_json(match.capability, match) for match in matches],
                }
            )
        )
        return 0 if matches else 1
    if not matches:
        print(
            f"wreath: nothing here answers {term!r}. Try `wreath capabilities` for "
            "the whole list, or read docs/capabilities.md, which closes with what "
            "wreath deliberately does not include.",
            file=sys.stderr,
        )
        return 1
    plural = "capability" if len(matches) == 1 else "capabilities"
    print(f"{term} -- {len(matches)} {plural}\n")
    for match in matches:
        print(_render_capability(match))
    return 0


def _capability_json(capability: Any, match: Any = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": capability.name,
        "capability": capability.sentence,
        "modules": list(capability.modules),
        "guides": list(capability.guides),
        "replaces": list(capability.replaces),
    }
    if match is not None:
        payload["reason"] = match.reason
        payload["matched"] = match.matched
    return payload


def _render_capability(match: Any) -> str:
    """One capability as a paragraph: what it is, where it is, where to read."""
    import textwrap

    capability = match.capability
    lines = [f"{capability.name}  ({match.reason} {match.matched!r})"]
    lines.extend(
        textwrap.wrap(capability.sentence, width=76, initial_indent="  ", subsequent_indent="  ")
    )
    lines.append(f"  modules  {', '.join(capability.modules) or 'built in'}")
    if capability.guides:
        lines.append(f"  guides   {', '.join(capability.guides)}")
    return "\n".join(lines) + "\n"


def execute_doctor(namespace: argparse.Namespace) -> int:
    action = getattr(namespace, "action", None)
    if action == "trace":
        return execute_doctor_trace(namespace)
    if action == "preflight":
        return execute_doctor_preflight(namespace)
    if action == "routes":
        return execute_doctor_routes(namespace)
    return execute_doctor_n_plus_one(namespace)


def execute_doctor_routes(namespace: argparse.Namespace) -> int:
    """Write or compare the deterministic application route manifest."""
    from .doctor import render_route_manifest, route_manifest

    app = load_application(namespace.target, factory=namespace.factory)
    rendered = render_route_manifest(route_manifest(app, application=namespace.target))
    if namespace.check:
        path = Path(namespace.check)
        try:
            expected = path.read_text(encoding="utf-8")
        except FileNotFoundError as error:
            raise CliError(f"route manifest does not exist: {path}", exit_code=1) from error
        if expected != rendered:
            print(
                f"route manifest differs: {path}; regenerate with "
                f"`wreath doctor routes {namespace.target} --write {path}`",
                file=sys.stderr,
            )
            return 1
        return 0
    if namespace.write:
        Path(namespace.write).write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


def execute_doctor_preflight(namespace: argparse.Namespace) -> int:
    """Report every check wreath can make on a built application. 1 if any block.

    The `--settings`/`--env`/`--environ` vocabulary is `wreath infra infer`'s,
    parsed by that command's own helpers rather than a second copy: they are the
    same three questions, and two spellings would eventually answer differently.
    """
    import json as _json

    from .doctor import preflight, preflight_as_dict, render_preflight
    from .infra.cli import _settings_models, _suppliers

    models = _settings_models(namespace.settings)
    supplied, dotenv = _suppliers(namespace.env, environ=namespace.environ)
    app = load_application(namespace.target, factory=namespace.factory)
    report = preflight(
        app,
        application=namespace.target,
        settings=models,
        supplied=supplied,
        dotenv_keys=dotenv,
    )
    if namespace.as_json:
        print(_json.dumps(preflight_as_dict(report)))
    else:
        print(render_preflight(report), end="")
    return 1 if report.blocking else 0


def execute_doctor_trace(namespace: argparse.Namespace) -> int:
    """Resolve a trace id to the request and every durable unit it caused.

    Two halves with two failure modes, kept apart on purpose. The durable half
    reads the application's database and is as complete as the schema is. The
    request half reads the Flight Recorder over an Inspector socket and is as
    complete as the ring is -- it needs `--socket`, and without one the report
    says so rather than implying the request was not found.
    """
    import asyncio
    import json as _json

    from .doctor import TraceLookup, find_requests_with_trace
    from .inspector import InspectorClient, InspectorError
    from .telemetry import trace_id_of

    # A whole traceparent is accepted as well as a bare id: it is what the CLI
    # itself stores and what a `traceparent` header carries, so pasting one back
    # in should work rather than silently match nothing.
    raw = namespace.trace_id.strip()
    trace_id = trace_id_of(raw) or raw.lower()
    if len(trace_id) != 32 or any(c not in "0123456789abcdef" for c in trace_id):
        raise CliError(
            f"{namespace.trace_id!r} is not a trace id: expected 32 hex "
            "characters, or a whole traceparent to take one from",
            exit_code=2,
        )

    lookup = TraceLookup(trace_id=trace_id)
    if namespace.target is None:
        lookup = TraceLookup(
            trace_id=trace_id,
            omitted=(
                "jobs, durable messages, workflows and passes: no application "
                "target was given, so no database was read",
            ),
        )
    else:
        application = load_application(namespace.target, factory=namespace.factory)
        targets = _pass_ledgers(application, namespace)
        if not targets:
            raise CliError(
                "no database to search: the application configures no job "
                "runner, and no --database was given",
                exit_code=2,
            )
        lookup = asyncio.run(
            _read_traced_work(
                targets,
                trace_id,
                workflow_schema=namespace.workflow_schema,
                workflow_table=namespace.workflow_table,
            )
        )

    requests: tuple[Any, ...] = ()
    omitted = list(lookup.omitted)
    if namespace.socket is None:
        omitted.append(
            "the request itself: it lives in the Flight Recorder's ring, not in "
            "the database. Pass --socket to search it"
        )
    else:

        async def query() -> tuple[Any, ...]:
            async with InspectorClient(namespace.socket) as client:
                return await find_requests_with_trace(client, trace_id, limit=namespace.limit)

        try:
            requests = asyncio.run(query())
        except InspectorError as error:
            print(f"wreath doctor: error: {error}", file=sys.stderr)
            return 1
        except (ConnectionError, FileNotFoundError) as error:
            print(f"wreath doctor: cannot reach inspector: {error}", file=sys.stderr)
            return 1
        if not requests:
            omitted.append(
                f"the request itself: no recorded trace in the last "
                f"{namespace.limit} carries this id, so it has aged out of the "
                "ring or was never sampled"
            )

    lookup = TraceLookup(
        trace_id=trace_id,
        work=lookup.work,
        requests=requests,
        omitted=tuple(omitted),
    )
    if namespace.as_json:
        print(_json.dumps({"version": 1, "check": "trace", **lookup.as_dict()}, indent=2))
        return 0
    _print_trace_lookup(lookup)
    return 0


async def _read_traced_work(
    targets: list[tuple[Any, str]],
    trace_id: str,
    *,
    workflow_schema: str,
    workflow_table: str,
) -> Any:
    from .doctor import TraceLookup, find_work_with_trace

    work: list[Any] = []
    omitted: list[str] = []
    seen_omitted: set[str] = set()
    for database, schema in targets:
        await database.start()
        try:
            connection = await database.acquire("write")
            try:
                found = await find_work_with_trace(
                    connection,
                    trace_id,
                    schema=schema,
                    workflow_schema=workflow_schema,
                    workflow_table=workflow_table,
                )
            finally:
                await database.release("write", connection)
        finally:
            await database.stop()
        work.extend(found.work)
        for note in found.omitted:
            if note not in seen_omitted:
                seen_omitted.add(note)
                omitted.append(note)
    return TraceLookup(trace_id=trace_id, work=tuple(work), omitted=tuple(omitted))


def _print_trace_lookup(lookup: Any) -> None:
    print(f"trace {lookup.trace_id}")
    print()
    if lookup.requests:
        print(f"{len(lookup.requests)} recorded request(s):")
        for request in lookup.requests:
            outcome = "FAILED" if request.is_failure else "ok"
            print(
                f"  request {request.request_id}  route {request.route_id}  "
                f"status {request.status}  {request.duration_us}us  {outcome}"
            )
        print()
    if lookup.work:
        print(f"{len(lookup.work)} durable unit(s) of work:")
        for item in lookup.work:
            label = item.label if not item.tenant else f"{item.label}@{item.tenant}"
            print(f"  {item.kind:<9} {item.identifier:<24} {label}  [{item.state}]")
            if item.detail:
                print(f"{'':<12}error: {item.detail}")
    else:
        print("no durable work carries this trace")
    if lookup.omitted:
        # Printed always, and last, because it is what stops the reader
        # concluding "nothing else" from a search that did not run.
        print()
        print("not searched:")
        for note in lookup.omitted:
            print(f"  - {note}")


def execute_doctor_n_plus_one(namespace: argparse.Namespace) -> int:
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
        print(
            _json.dumps(
                {
                    "version": 1,
                    "check": "n-plus-one",
                    "threshold": namespace.threshold,
                    "findings": [
                        {
                            "route": f.route,
                            "request_id": f.request_id,
                            "queries": f.queries,
                            "summary": f.explain(),
                            "repetitions": [
                                {"model": r.model, "count": r.count, "total_us": r.total_us}
                                for r in f.repetitions
                            ],
                        }
                        for f in findings
                    ],
                }
            )
        )
    else:
        _print_n_plus_one(findings, namespace.threshold)
    return 1 if findings and namespace.strict else 0


#: How each scope is named in the report, and the order the groups print in --
#: requests first because they are what a reader came for, then the background
#: scopes in the order they are hardest to reproduce.
_SCOPE_LABELS: tuple[tuple[str, str], ...] = (
    ("request", "request(s)"),
    ("job", "job attempt(s)"),
    ("step", "workflow step(s)"),
    ("shift", "pass shift(s)"),
)


def _print_n_plus_one(findings: list, threshold: int) -> None:
    if not findings:
        print(
            f"nothing queried one model {threshold} or more times. "
            "Note this reads sampled traces: a Detailed recorder sees more."
        )
        return
    grouped: dict[str, list] = {}
    for finding in findings:
        grouped.setdefault(getattr(finding.origin, "kind", "request"), []).append(finding)
    known = dict(_SCOPE_LABELS)
    # Ordered groups first, then anything a newer producer invented, so an
    # unrecognised scope is reported rather than dropped.
    order = [kind for kind, _ in _SCOPE_LABELS if kind in grouped]
    order += sorted(kind for kind in grouped if kind not in known)
    for kind in order:
        group = grouped[kind]
        noun = known.get(kind, f"{kind}(s)")
        print(f"{len(group)} {noun} queried one model {threshold}+ times:\n")
        for finding in group:
            print(f"  {finding.explain()}")
            for repetition in finding.repetitions:
                millis = repetition.total_us / 1000
                print(
                    f"      {repetition.model:<24} {repetition.count:>5} queries {millis:>8.1f}ms"
                )
            if finding.request_id:
                print(f"      replay it: wreath replay --request {finding.request_id}")
            print()
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
        print(
            f"{body['total']} active request(s)"
            + (" [truncated page]" if body.get("truncated") else "")
        )
        for row in body["requests"]:
            print(
                f"  #{row['request_id']}  {row['protocol']:9s} "
                f"age {row['age_us']}us  route {row['route_id']}"
            )
        return
    if topic in ("routes", "metadata"):
        print(
            f"{body['table']}: {body['total']} row(s)"
            + (" [truncated page]" if body.get("truncated") else "")
        )
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
                f"{row['method']} {row['path']}" if row.get("path") else f"route {row['route_id']}"
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


def _execute_flight_replay(namespace: argparse.Namespace) -> int:
    """Re-drive a recorded request and say whether it retraced the crash.

    The ring file names the request that was in flight and the call sites it had
    reached; it does not hold the request's bytes, so the recording supplies
    those. What joins them is the sequence of sites: a replay that emits the
    same ones in the same order went where the dead process went.

    Exit code 1 when the replay diverges, so this is usable as the check in a
    loop -- "did my fix change the path?" is a question with a yes and a no.
    """
    import asyncio
    import json as _json

    from . import replay as rp
    from .recording import read_ring_file

    ring = read_ring_file(namespace.path)
    app = load_application(namespace.target, factory=namespace.factory)
    try:
        outcome = asyncio.run(
            rp.reproduce_from_ring(
                app,
                ring,
                rp.open_recording(namespace.recording),
                request_id=namespace.request_id,
            )
        )
    except rp.ReplayError as error:
        # "no request was in flight", "more than one was", "that one logged
        # nothing" -- all usage problems with a specific answer, and none of
        # them worth a traceback at someone reading a crash file.
        raise CliError(str(error), exit_code=2) from error

    if namespace.as_json:
        print(
            _json.dumps(
                {
                    "schema_version": 1,
                    "request_id": outcome.request_id,
                    "reproduced": outcome.reproduced,
                    "diverged_at": outcome.diverged_at,
                    "expected_sites": list(outcome.expected),
                    "observed_sites": list(outcome.observed),
                    "terminal": outcome.result.terminal,
                },
                indent=2,
            )
        )
        return 0 if outcome.reproduced else 1

    print(f"request {outcome.request_id} was in flight when the process died")
    print(f"  it had reached {len(outcome.expected)} log call site(s)")
    print(f"  the replay reached {len(outcome.observed)}")
    if outcome.reproduced:
        print("  the replay retraced the whole recorded path")
        if len(outcome.observed) > len(outcome.expected):
            print(
                "  and went further, which is expected: the file stops where "
                "the process stopped, not where the request would have"
            )
    else:
        print(f"  they diverge at site {outcome.diverged_at}")
        print(f"    crash file: {list(outcome.expected)}")
        print(f"    replay:     {list(outcome.observed)}")
        if outcome.diverged_at == 0:
            # Only here. A divergence further in is a real behaviour change and
            # saying "wrong build?" at it would send someone chasing the
            # environment instead of reading their own diff.
            print(
                "  diverging at the very first site usually means this is not "
                "the build that crashed -- a site id is import order, not an "
                "identity, so two builds share none of them"
            )
    print(f"  the replayed connection ended {outcome.result.terminal}")
    return 0 if outcome.reproduced else 1


#: The `WFR1` container's first four bytes. Named here rather than imported
#: from `_recording_format` so reading the magic costs no import of the decoder
#: for a file that turns out to be a ring.
_RECORDING_MAGIC = b"WFR1"

#: A `WTR1` transport recording -- a third container this command does not read,
#: named so the refusal points at the command that does.
_TRANSPORT_MAGIC = b"WTR1"


def _flight_magic(path: str) -> bytes:
    """The first four bytes, or `b""` for a file too short to have any.

    A short file is *not* refused here: the ring reader's own message about a
    file shorter than a header is better than anything this could say, and it
    is already tested.
    """
    with open(path, "rb") as handle:
        return handle.read(4)


def _execute_flight_read_recording(namespace: argparse.Namespace) -> int:
    """`wreath flight read` over a `WFR1` recording rather than a ring file.

    The library decoder shipped with the attempt record kind and this dispatch
    did not, so a `.wfr1` handed to `flight read` was answered with the ring
    reader's complaint about a `WFRR` magic -- a true statement about the wrong
    thing. Every refusal below is the decoder's own, by name: this function
    adds no error text of its own, because two spellings of "truncated" is how
    they drift apart.
    """
    import json as _json

    from ._recording_format import read_recording

    with open(namespace.path, "rb") as handle:
        data = handle.read()
    decoded = read_recording(data)

    if namespace.as_json:
        print(
            _json.dumps(
                {
                    "schema_version": 1,
                    "container": "WFR1",
                    "clean": decoded.clean,
                    "capture_slabs": len(decoded.slabs),
                    "event_cells": len(decoded.events),
                    "attempts": [
                        {
                            "job_id": record.job_id,
                            "queue": record.queue,
                            "task": record.task,
                            "attempt": record.attempt,
                            "max_attempts": record.max_attempts,
                            "tenant": record.tenant,
                            "dedup_key": record.dedup_key,
                            "fence": record.fence,
                            "trace_context": record.trace_context,
                            "outcome": record.outcome,
                            "error_type": record.error_type,
                            "error_message": record.error_message,
                            "argument_count": record.argument_count,
                            "arguments": dict(record.arguments),
                            "boundaries": [
                                {
                                    "seam": event.seam,
                                    "target": event.target,
                                    "coordinate": event.coordinate,
                                    "error_type": event.error_type,
                                }
                                for event in record.boundaries
                            ],
                        }
                        for record in decoded.attempts
                    ],
                },
                indent=2,
            )
        )
        return 0

    print(f"recording {namespace.path}")
    print(
        f"  WFR1 container: {len(decoded.slabs)} capture slab(s), "
        f"{len(decoded.events)} event cell(s), {len(decoded.attempts)} attempt(s)"
    )
    if not decoded.clean:
        # Said before the contents, for the reason the ring reader prints its
        # losses first: a torn file read as a complete one loses exactly the
        # records nearest the failure.
        print("  no footer -- the process died mid-write, so what is missing cannot be counted")
    print()
    for record in decoded.attempts:
        print(
            f"  {record.task} job {record.job_id} on queue {record.queue!r}: "
            f"attempt {record.attempt} of {record.max_attempts} -> {record.outcome}"
        )
        if record.error_type:
            print(f"      raised {record.error_type}: {record.error_message}")
        kept = (
            "none allowed by name"
            if not record.arguments
            else (f"{len(record.arguments)} captured")
        )
        print(
            f"      fence {record.fence}, tenant {record.tenant!r}, "
            f"{record.argument_count} argument(s), {kept}"
        )
        for name, captured in record.arguments:
            print(f"      arg {name} = {captured}")
        if record.trace_context:
            print(f"      enqueued under trace context {record.trace_context}")
        for event in record.boundaries:
            failed = f" -> {event.error_type}" if event.error_type else ""
            print(
                f"      boundary seam {event.seam} target {event.target!r} "
                f"at {event.coordinate}{failed}"
            )
    return 0


def execute_flight(namespace: argparse.Namespace) -> int:
    """Decode a ring file and report what it held -- and what it could not.

    The counts are printed *first*, and deliberately. A crash file read as if it
    were complete is worse than one nobody read: a ring that was full has
    dropped exactly the records nearest the failure, and a reader who does not
    see that count will conclude the last thing in the file was the last thing
    that happened.
    """
    import json as _json

    from ._flight_schema import EventKind, LossReason, SchemaError
    from .recording import read_ring_file

    if namespace.flight_action == "replay":
        return _execute_flight_replay(namespace)

    # One command, one question -- "what is in this file?" -- and the recorder
    # writes two containers that answer it. `WFRR` is the ring the process
    # mapped and died holding; `WFR1` is a recording it wrote deliberately, and
    # a job attempt is a record kind *inside* that container rather than a
    # second format. Dispatching on the magic is what stops the ring reader
    # from telling somebody holding an attempt recording that they should have
    # brought a `WFRR` file, which names the wrong half of the mistake.
    magic = _flight_magic(namespace.path)
    if magic == _RECORDING_MAGIC:
        return _execute_flight_read_recording(namespace)
    if magic == _TRANSPORT_MAGIC:
        # The third container, and the one this command does not read. Named
        # rather than left to the ring reader's "not a wreath ring file",
        # which is true and sends the reader looking for a different file
        # instead of for a different command.
        raise SchemaError(
            f"{namespace.path} is a WTR1 transport recording of a connection's "
            "bytes, not a flight recorder file. `wreath replay transport` "
            "replays it, and `wreath replay to-test` turns it into a test"
        )

    ring = read_ring_file(namespace.path)
    header = ring.header
    kinds = {
        "completion": EventKind.COMPLETION,
        "correlation": EventKind.CORRELATION,
        "phase": EventKind.PHASE,
        "log": EventKind.LOG,
        "client-facts": EventKind.CLIENT_FACTS,
    }
    records = ring.records if namespace.kind is None else ring.of_kind(kinds[namespace.kind])
    shown = records if namespace.limit == 0 else records[: namespace.limit]
    losses = {
        reason.name.lower(): header.loss(reason) for reason in LossReason if header.loss(reason)
    }

    if namespace.as_json:
        print(
            _json.dumps(
                {
                    "schema_version": 1,
                    "pid": header.pid,
                    "worker_id": header.worker_id,
                    "ring_records": header.ring_records,
                    "head": header.head,
                    "tail": header.tail,
                    "live": ring.live,
                    "drained": ring.drained,
                    "undecodable": ring.undecodable,
                    "cursors_inconsistent": ring.cursors_inconsistent,
                    "created_unix_nano": header.created_unix_nano,
                    "losses": losses,
                    "records": [
                        {
                            "sequence": record.sequence,
                            "kind": EventKind(record.kind).name,
                            "record": repr(record.decode()),
                        }
                        for record in shown
                    ],
                },
                indent=2,
            )
        )
        return 0

    print(f"ring file {namespace.path}")
    print(f"  written by pid {header.pid}, worker {header.worker_id}")
    print(f"  ring of {header.ring_records} records; head {header.head}, tail {header.tail}")
    print(
        f"  {ring.live} recovered, {ring.drained} already drained "
        f"(look for those in the recording's EVNT stream)"
    )
    if ring.undecodable:
        print(
            f"  {ring.undecodable} slot(s) did not decode -- a cell half-written "
            "as the process died"
        )
    if ring.cursors_inconsistent:
        print("  the head/tail pair was torn mid-update; the window was clamped")
    if losses:
        print("  the worker dropped:")
        for name, count in losses.items():
            print(f"    {name:24s} {count}")
        if ring.ring_full_drops:
            print(
                "    ^ a full ring refuses rather than overwrites, so the "
                "records nearest the crash may be the missing ones"
            )
    else:
        print("  the worker dropped nothing")
    print()
    for record in shown:
        print(f"  {record.sequence:>8}  {EventKind(record.kind).name:<12} {record.decode()}")
    if len(records) > len(shown):
        print(f"  ... {len(records) - len(shown)} more (--limit 0 for all)")
    return 0


def _execute_to_test(namespace: argparse.Namespace) -> int:
    """`wreath replay to-test` -- one command, two record kinds.

    `open_recording` dispatches on the container magic, so a `.wtr1` request
    recording and a `.wfr1` job attempt reach the same subcommand rather than a
    second one nobody finds. They differ in what the target names: a request
    replays against the **application**, an attempt against the **job runner**
    its task is registered on, which is not callable and so is not loaded by
    `load_application`.
    """
    import asyncio

    from . import replay as rp
    from ._target import load_target

    if rp.recording_kind(namespace.recording) == rp.KIND_ATTEMPT:
        _ensure_cwd_importable()
        try:
            runner = load_target(namespace.target, label="job runner")
        except ValueError as error:
            raise CliError(
                f"could not load the job runner {namespace.target!r}: {error}. An "
                "attempt recording replays against the runner its task is "
                "registered on, spelled module:attribute"
            ) from error
        generate = rp.generate_attempt_test(
            runner,
            rp.open_attempt_recording(namespace.recording),
            target=namespace.target,
            name=namespace.name,
            origin=namespace.recording,
        )
    else:
        generate = rp.generate_test(
            load_application(namespace.target, factory=namespace.factory),
            rp.open_recording(namespace.recording),
            target=namespace.target,
            name=namespace.name,
            origin=namespace.recording,
        )
    source = asyncio.run(generate)
    if namespace.output:
        with open(namespace.output, "w", encoding="utf-8") as handle:
            handle.write(source)
        print(f"wrote {namespace.output}")
    else:
        print(source, end="")
    return 0


def execute_replay(namespace: argparse.Namespace) -> int:
    """Run a transport or endpoint-plan replay and print the owned outcome.

    Unlike inspect/capture, replay loads the target application: it drives the
    app's own protocol and endpoint code in-process over fake transports. It
    never opens a socket and cannot broaden any capture policy.
    """
    import asyncio
    import json as _json

    from . import replay as rp

    action = namespace.replay_action
    if action == "to-test":
        return _execute_to_test(namespace)

    app = load_application(namespace.target, factory=namespace.factory)

    if action == "transport":
        recording = rp.open_recording(namespace.recording)
        schedule = None
        if namespace.inject:
            schedule = rp.FaultSchedule.from_bytes(_read_bytes(namespace.inject))
        protocol_cls = None
        result = asyncio.run(
            rp.replay_transport(app, recording, protocol_cls=protocol_cls, faults=schedule)
        )
        if namespace.record_faults:
            _write_bytes(namespace.record_faults, (schedule or rp.FaultSchedule()).to_bytes())
        if namespace.as_json:
            print(
                _json.dumps(
                    {
                        "version": 1,
                        "kind": "transport",
                        "terminal": result.terminal,
                        "write_count": result.write_count,
                        "segments_fed": result.segments_fed,
                        "status_line": result.response.split(b"\r\n", 1)[0].decode(
                            "latin-1", "replace"
                        ),
                        "response_bytes": len(result.response),
                    }
                )
            )
        else:
            status = result.response.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            print(
                f"terminal={result.terminal} writes={result.write_count} "
                f"segments_fed={result.segments_fed}"
            )
            print(status or "(no response bytes)")
        return 0

    headers = tuple(_split_header(h) for h in namespace.header)
    canonical = rp.CanonicalRequest(
        method=namespace.method,
        path=namespace.path,
        headers=headers,
        query_string=namespace.query.encode("utf-8"),
        body=namespace.body.encode("utf-8"),
    )
    mode = rp.PlanMode(namespace.mode)
    result = asyncio.run(
        rp.replay_endpoint_plan(
            app,
            canonical,
            mode=mode,
            recorded_return=namespace.replace_body if mode is rp.PlanMode.REPLACE else None,
        )
    )
    if namespace.as_json:
        print(
            _json.dumps(
                {
                    "version": 1,
                    "kind": "plan",
                    "mode": result.mode,
                    "status": result.status,
                    "body_bytes": len(result.body),
                    "best_effort": result.best_effort,
                    "deterministic": result.deterministic,
                    "note": result.note,
                }
            )
        )
    else:
        print(
            f"mode={result.mode} status={result.status} "
            f"deterministic={result.deterministic} best_effort={result.best_effort}"
        )
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
    arguments = list(argv) if argv is not None else sys.argv[1:]
    parser = build_parser()
    if arguments[:1] == ["test"]:
        # pytest owns a large and extensible option vocabulary.  Parsing only
        # Wreath's few options preserves every unknown token and its ordering,
        # so ``wreath test -k auth tests/ --maxfail=1`` needs no separator and
        # behaves exactly like the pytest spelling.
        namespace, pytest_args = parser.parse_known_args(arguments)
        namespace.pytest_args = pytest_args
    else:
        namespace = parser.parse_args(arguments)
    try:
        if namespace.command == "typegen":
            return execute_typegen(namespace)
        if namespace.command == "inspect":
            return execute_inspect(namespace)
        if namespace.command == "capabilities":
            return execute_capabilities(namespace)
        if namespace.command == "new":
            return execute_new(namespace)
        if namespace.command == "ci":
            try:
                return execute_ci(namespace)
            except (OSError, ValueError) as error:
                raise CliError(str(error), exit_code=2) from error
        if namespace.command == "doctor":
            # `preflight` resolves `--settings`/`--env` through `wreath infra
            # infer`'s own helpers, which report a typo as a ValueError. That is
            # a usage error and has to read like one here too, or the same
            # mistake gives a clean message from one command and a traceback
            # from the other.
            try:
                return execute_doctor(namespace)
            except (OSError, TypeError, ValueError) as error:
                raise CliError(str(error), exit_code=2) from error
        if namespace.command == "mcp":
            return execute_mcp(namespace)
        if namespace.command == "capture":
            return execute_capture(namespace)
        if namespace.command == "replay":
            return execute_replay(namespace)
        if namespace.command == "flight":
            try:
                return execute_flight(namespace)
            except (OSError, ValueError) as error:
                # SchemaError is a ValueError: an unreadable ring file is a
                # usage-level failure, not a traceback at someone whose process
                # has already crashed once today.
                raise CliError(str(error), exit_code=2) from error
        if namespace.command == "passes":
            try:
                return execute_passes(namespace)
            except (OSError, KeyError, ValueError) as error:
                raise CliError(str(error), exit_code=2) from error
        if namespace.command == "jobs":
            try:
                return execute_jobs(namespace)
            except (OSError, KeyError, ValueError) as error:
                raise CliError(str(error), exit_code=2) from error
        if namespace.command == "schema":
            try:
                return execute_schema(namespace)
            except (OSError, KeyError, ValueError) as error:
                raise CliError(str(error), exit_code=2) from error
        if namespace.command == "migrations":
            from ._migrations.cli import execute as execute_migrations

            try:
                return execute_migrations(namespace, load_application)
            except (OSError, RuntimeError, ValueError) as error:
                raise CliError(str(error), exit_code=2) from error
        if namespace.command == "infra":
            from .infra.cli import execute as execute_infra

            try:
                return execute_infra(namespace, load_application)
            except (OSError, TypeError, ValueError) as error:
                raise CliError(str(error), exit_code=2) from error
        if namespace.command == "privacy":
            from ._privacy.cli import execute as execute_privacy

            try:
                return execute_privacy(namespace)
            except (OSError, TypeError, ValueError) as error:
                raise CliError(str(error), exit_code=2) from error
        if namespace.command == "docs":
            from ._docs.cli import execute as execute_docs

            return execute_docs(namespace)
        if namespace.command == "port":
            from ._port.cli import execute as execute_port

            try:
                return execute_port(namespace)
            except (OSError, ValueError) as error:
                raise CliError(str(error), exit_code=2) from error
        if namespace.command == "mutant":
            from ._mutant.cli import execute_mutant

            try:
                return execute_mutant(namespace)
            except (OSError, ValueError) as error:
                raise CliError(str(error), exit_code=2) from error
        if namespace.command == "test":
            from ._test_runner import execute as execute_tests

            try:
                return execute_tests(namespace)
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
