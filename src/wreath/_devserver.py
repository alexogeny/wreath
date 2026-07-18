"""Development reload supervisor for Wreath's command-line server."""

from __future__ import annotations

import fnmatch
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ._cli import RunOptions

FileSignature = tuple[int, int]
Snapshot = dict[str, FileSignature]

_DEFAULT_EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        "build",
        "dist",
        "site",
        ".sanitizers",
    }
)
_DEFAULT_EXCLUDED_PREFIXES = ("benchmark-results", "benchmark-diagnosis")


class ChildProcess(Protocol):
    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


def _pattern_matches(relative: str, name: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) or fnmatch.fnmatchcase(relative, pattern)
               for pattern in patterns)


def _excluded_directory(relative: str, name: str, patterns: Sequence[str]) -> bool:
    return (
        name in _DEFAULT_EXCLUDED_DIRECTORIES
        or name.startswith(_DEFAULT_EXCLUDED_PREFIXES)
        or _pattern_matches(relative, name, patterns)
    )


def _walk_files(
    root: Path, includes: Sequence[str], excludes: Sequence[str]
) -> Iterable[tuple[str, FileSignature]]:
    # ``os.scandir`` and ``DirEntry.stat`` keep directory iteration and metadata
    # reads in the platform's C implementation. Carry relative prefixes through
    # the stack to avoid constructing Path objects or rescanning path segments
    # for every file on each reload poll.
    stack = [(os.fspath(root), "")]
    while stack:
        directory, relative_parent = stack.pop()
        try:
            entries = os.scandir(directory)
        except OSError:
            continue
        with entries:
            for entry in entries:
                relative = (
                    f"{relative_parent}/{entry.name}" if relative_parent else entry.name
                )
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if not _excluded_directory(relative, entry.name, excludes):
                            stack.append((entry.path, relative))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    if not _pattern_matches(relative, entry.name, includes):
                        continue
                    if _pattern_matches(relative, entry.name, excludes):
                        continue
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                yield entry.path, (stat.st_mtime_ns, stat.st_size)


def snapshot_files(
    roots: Sequence[Path], includes: Sequence[str], excludes: Sequence[str]
) -> Snapshot:
    """Return a deterministic metadata snapshot of matching regular files."""
    snapshot: Snapshot = {}
    seen_roots: set[str] = set()
    for requested_root in roots:
        root = requested_root.expanduser().resolve(strict=False)
        root_key = os.path.normcase(os.fspath(root))
        if root_key in seen_roots:
            continue
        seen_roots.add(root_key)
        if root.is_file():
            try:
                stat = root.stat()
            except OSError:
                continue
            if _pattern_matches(root.name, root.name, includes) and not _pattern_matches(
                root.name, root.name, excludes
            ):
                snapshot[os.fspath(root)] = (stat.st_mtime_ns, stat.st_size)
            continue
        for path, signature in _walk_files(root, includes, excludes):
            snapshot[path] = signature
    return dict(sorted(snapshot.items()))


class ChangeDetector:
    """Own the previous snapshot and report one edge per observed change."""

    def __init__(
        self, roots: Sequence[Path], includes: Sequence[str], excludes: Sequence[str]
    ) -> None:
        self._roots = tuple(roots)
        self._includes = tuple(includes)
        self._excludes = tuple(excludes)
        self._snapshot = snapshot_files(self._roots, self._includes, self._excludes)

    def poll(self) -> bool:
        current = snapshot_files(self._roots, self._includes, self._excludes)
        if current == self._snapshot:
            return False
        self._snapshot = current
        return True


def _flag(name: str) -> str:
    return "--" + name.replace("_", "-")


def worker_argv(options: RunOptions) -> tuple[str, ...]:
    """Serialize serving options into a fresh, non-reloading child command."""
    argv = [sys.executable, "-m", "wreath", "run", options.target]
    if options.factory:
        argv.append("--factory")
    scalar_options = (
        ("host", options.host),
        ("port", options.port),
        ("backlog", options.backlog),
        ("keep_alive_timeout", options.keep_alive_timeout),
        ("request_timeout", options.request_timeout),
        ("shutdown_timeout", options.shutdown_timeout),
        ("max_request_line", options.max_request_line),
        ("max_header_count", options.max_header_count),
        ("max_header_bytes", options.max_header_bytes),
        ("max_body_bytes", options.max_body_bytes),
        ("read_high_water", options.read_high_water),
        ("read_high_water_messages", options.read_high_water_messages),
        ("max_ws_fragments", options.max_ws_fragments),
        ("lifespan", options.lifespan),
        ("max_concurrent_streams", options.max_concurrent_streams),
        ("initial_stream_window", options.initial_stream_window),
        ("initial_connection_window", options.initial_connection_window),
        ("max_header_list_bytes", options.max_header_list_bytes),
        ("hpack_table_bytes", options.hpack_table_bytes),
        ("qpack_table_bytes", options.qpack_table_bytes),
        ("qpack_blocked_streams", options.qpack_blocked_streams),
        ("loop", options.loop),
    )
    for name, value in scalar_options:
        argv.extend((_flag(name), str(value)))
    for protocol in options.protocols:
        argv.extend(("--protocol", protocol))
    if options.tls_cert is not None:
        argv.extend(("--tls-cert", options.tls_cert, "--tls-key", options.tls_key or ""))
    if options.tls_password_file is not None:
        argv.extend(("--tls-password-file", options.tls_password_file))
    return tuple(argv)


def _stop_child(child: ChildProcess, timeout: float) -> None:
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=1.0)


def _watch_roots(options: RunOptions) -> tuple[Path, ...]:
    if options.reload_dirs:
        roots = tuple(Path(value) for value in options.reload_dirs)
    else:
        roots = (Path.cwd(),)
    missing = [os.fspath(root) for root in roots if not root.exists()]
    if missing:
        from ._cli import CliError

        raise CliError(f"reload path does not exist: {missing[0]}", exit_code=2)
    return roots


def _settle(detector: ChangeDetector, delay: float, debounce: float) -> None:
    if debounce == 0:
        return
    quiet_since = time.monotonic()
    interval = min(delay, max(0.01, debounce))
    while time.monotonic() - quiet_since < debounce:
        time.sleep(interval)
        if detector.poll():
            quiet_since = time.monotonic()


def supervise(options: RunOptions) -> None:
    """Run and gracefully replace child generations after source changes."""
    roots = _watch_roots(options)
    detector = ChangeDetector(roots, options.reload_includes, options.reload_excludes)
    argv = worker_argv(options)
    child: ChildProcess | None = None
    stopping = False

    def request_stop(_signum: int, _frame: FrameType | None) -> None:
        nonlocal stopping
        stopping = True

    previous_handlers: dict[
        int, Callable[[int, FrameType | None], object] | int | None
    ] = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous_handlers[signum] = signal.signal(signum, request_stop)
        except (OSError, ValueError):
            pass

    print(
        "wreath dev: watching " + ", ".join(os.fspath(root.resolve()) for root in roots),
        file=sys.stderr,
    )
    try:
        child = subprocess.Popen(argv)
        while not stopping:
            time.sleep(options.reload_delay)
            if not detector.poll():
                continue
            _settle(detector, options.reload_delay, options.reload_debounce)
            print("wreath dev: source change detected; reloading", file=sys.stderr)
            _stop_child(child, options.shutdown_timeout + 1.0)
            if stopping:
                break
            child = subprocess.Popen(argv)
    except KeyboardInterrupt:
        stopping = True
    finally:
        if child is not None:
            _stop_child(child, options.shutdown_timeout + 1.0)
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)
